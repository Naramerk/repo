"""CLI ``continue``: reopen a runtime on an EXISTING journal and resume the study.

Checkpoint-equals (event-sourced recovery design):

- A *checkpoint* is the journal itself (``events.db``, SQLite WAL +
  ``synchronous=FULL``) plus the lease row (owner/epoch/nonce/expires_at).
  Every state transition is committed as an event before its effects are
  relied on; CAS artifacts live under ``<data-root>/<study>/arts``. There is
  deliberately NO parallel snapshot subsystem — a snapshot would be a second
  source of truth next to the journal.
- *Restore* = a NEW process on the same ``data_root`` + ``study_id``:
  ``open_runtime`` replays every event (``_project_local`` rebuilds tasks,
  attempts, graph, verdicts) and takes the lease. A killed predecessor's lease
  is waited out (bounded ``--lease-wait-s``); takeover bumps the epoch and
  rotates the nonce, so a late/zombie old writer is fenced on its next append
  (``SqliteEventStore._fence`` → ``LeaseLost``).
- *Continue* = ``recover()`` (close crashed open attempts retryable, finish
  record-path attempts from CAS, resume pending expansions/foreach/loops) and
  re-drive unfinished work through the SAME dispatch shape as ``run``
  (``_require_attempt`` → executor-route → ``record_executor_route``; coder
  jobs via ``_dispatch_coder_task``).
- Must never happen: double execution of a closed attempt. ``Executor.record``
  writes require the attempt open (``AttemptClosedError``), the store rejects
  appends after ``StudyFinalized`` (``StudyClosedError``), and stale epochs
  cannot write at all.

This module never re-plans: tasks come from the journal only (unique EXP-n
keys by construction), so a resumed study cannot grow duplicate tasks.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path
from typing import IO, Any

from coscientist.agents.adapter import LlmAgentAdapter
from coscientist.agents.derive import CODER_HINT
from coscientist.agents.schemas import (
    OutputSpecDraft,
    RubricDraft,
    RubricItemDraft,
    TaskSpecDraft,
)
from coscientist.api.runtime import StudyRuntime
from coscientist.cli.errors import CliRefuse
from coscientist.cli.run import (
    CliToolSurface,
    _assert_profile_cli_routes,
    _assert_profile_tools_on_surface,
    _CoderJobDecl,
    _dispatch_coder_task,
    _execute_executor_attempt,
    _optional_report_stage,
    _registered_user_inputs,
    _require_attempt,
    _resolve_planned_calls,
    _to_task_spec,
    _WriterJobJournal,
)
from coscientist.cli.tool_surface import LiveCliToolSurface
from coscientist.core.events.attempt_fields import attempt_row_is_open
from coscientist.core.events.clock import Clock, SystemClock
from coscientist.core.events.errors import (
    JournalFormatError,
    JournalPayloadError,
    LeaseLost,
)
from coscientist.core.events.store import SqliteEventStore
from coscientist.core.execution.domain import (
    Budget,
    HitlPolicy,
    Task,
    TaskSpec,
    TaskState,
)
from coscientist.core.execution.recover import recover
from coscientist.core.execution.runtime_factory import close_runtime, open_runtime
from coscientist.profiles.models import Profile
from coscientist.tools.coder.local import resolve_coder_backend
from coscientist.tools.contract import ToolError

# Stage tasks are driven by their run-stage roles (tz_spec / planner /
# optional HYP/LIT) — never by the continue executor-route driver. REPORT is
# handled separately above the drive loop via ``_optional_report_stage``.
_STAGE_TASK_KEYS: frozenset[str] = frozenset({"SPEC", "PLAN", "HYP", "LIT"})

_LEASE_POLL_S = 0.1
_RETRY_POLL_S = 0.01


def _core_retry_pending(rt: StudyRuntime) -> bool:
    """Any task PENDING with reason retry_scheduled and no open attempt?

    Such a task is inside the core's retry backoff: the tick's
    ``_fire_due_retries`` arms the next attempt when ``retry_at`` is due —
    the driver only avoids spinning idle ticks inside that window.
    """
    for task in rt.tasks.values():
        if task.state != TaskState.PENDING:
            continue
        reason = getattr(task.reason, "value", task.reason)
        if reason != "retry_scheduled":
            continue
        if not any(attempt_row_is_open(rt.attempts, a) for a in task.attempts):
            return True
    return False


def _rubric_draft_from_spec(rubric: Any) -> RubricDraft:
    return RubricDraft(
        items=tuple(
            RubricItemDraft(
                id=it.id,
                text=it.text,
                required=it.required,
                check=it.check,
                code_kind=it.code_kind,
            )
            for it in rubric.items
        ),
        aggregate=rubric.aggregate,
        k=rubric.k,
    )


def draft_from_task_spec(spec: TaskSpec) -> TaskSpecDraft:
    """Inverse of ``run._to_task_spec`` for the CLI-representable spec subset.

    Callers MUST verify with :func:`spec_redrivable` — a spec with core-owned
    fields the draft cannot carry (foreach/loop, budget shares, frozen kinds,
    …) fails the roundtrip and continue typed-refuses to re-drive it.
    """
    return TaskSpecDraft(
        task_key=spec.task_key,
        title=spec.title,
        goal=spec.goal,
        route=spec.route,
        depends_on=tuple(spec.depends_on),
        addresses=tuple(spec.addresses),
        tools_hint=tuple(spec.tools_hint),
        expected_outputs=tuple(
            OutputSpecDraft(
                name=o.name,
                media_type=o.media_type,
                schema_ref=o.schema_ref,
                required=o.required,
                rubric=_rubric_draft_from_spec(o.rubric) if o.rubric is not None else None,
            )
            for o in spec.expected_outputs
        ),
        criteria=tuple(c.template_id for c in spec.criteria),
    )


# Spec fields the planner draft carries (the executor-route prompt semantics).
# Everything else on a journaled TaskSpec is core-owned; most of it is
# deterministic enrichment applied at TaskCreated (e.g. ``sandbox_check_limits``
# frozen from the catalog) and is IGNORED by the redrive check, while fields
# that change dispatch/dependency semantics refuse the re-drive outright.
_DRAFT_CARRIED_FIELDS: tuple[str, ...] = (
    "task_key",
    "title",
    "goal",
    "route",
    "depends_on",
    "addresses",
    "tools_hint",
    "expected_outputs",
    "criteria",
)


def spec_redrivable(spec: TaskSpec) -> bool:
    """Journaled spec faithfully re-drivable through the CLI executor route?

    True iff (a) no core-owned behavioral field is set (foreach/loop material,
    budget/deadline shares, experiment method, tests intent, tolerated failed
    deps, InputSpec bindings — the executor-route prompt cannot carry them, so
    a re-drive would silently drop semantics), and (b) the draft-carried fields
    survive the ``draft → _to_task_spec`` roundtrip unchanged.
    """
    if (
        spec.foreach is not None
        or spec.loop_id is not None
        or spec.loop_iter is not None
        or spec.loop_template_key is not None
        or spec.budget_share is not None
        or spec.deadline_share is not None
        or spec.method is not None
        or spec.tests_intent is not None
        or spec.tolerate_failed_dep
        or spec.inputs
    ):
        return False
    back = _to_task_spec(draft_from_task_spec(spec))
    return all(getattr(spec, f) == getattr(back, f) for f in _DRAFT_CARRIED_FIELDS)


async def _wait_for_lease_free(
    db: Path,
    study_id: str,
    clock: Clock,
    *,
    wait_s: float,
) -> None:
    """Bounded wait until the predecessor's lease is free/expired (fail-closed).

    A live lease means the study may still be running elsewhere; fencing makes
    a takeover *safe*, but continue refuses instead of spinning past the
    operator's window (no silent racing on a live study).
    """
    if not (isinstance(wait_s, (int, float)) and wait_s >= 0):
        raise CliRefuse("lease_wait_s must be a number >= 0", code=2)
    probe = SqliteEventStore(study_id, db, clock=clock)
    try:
        deadline = clock.now() + timedelta(seconds=float(wait_s))
        while True:
            owner, _epoch, expires = await probe.current_lease()
            now = clock.now()
            if expires is None or expires <= now:
                return
            if now >= deadline:
                raise CliRefuse(
                    f"lease held by {owner!r} until {expires.isoformat()} — the study "
                    f"may still be running elsewhere; increase --lease-wait-s or stop "
                    f"the other writer",
                    code=5,
                )
            await asyncio.sleep(min(_LEASE_POLL_S, max((expires - now).total_seconds(), 0.01)))
    finally:
        await probe.close()


async def _probe_journal(data_root: Path, study_id: str, clock: Clock) -> None:
    """Pre-lease typed refuses: journal missing, unreadable, or foreign study id."""
    db = data_root / study_id / "events.db"
    if not db.is_file():
        raise CliRefuse(
            f"no journal at {db} (continue requires an existing run under --data-root)",
            code=2,
        )
    try:
        probe = SqliteEventStore(study_id, db, clock=clock)
        try:
            events = await probe.read_all()
        finally:
            await probe.close()
    except (JournalFormatError, JournalPayloadError) as exc:
        raise CliRefuse(f"journal {db} unreadable (corrupted): {exc}", code=2) from exc
    except sqlite3.DatabaseError as exc:
        raise CliRefuse(f"journal {db} unreadable (not a valid event store): {exc}", code=2) from exc
    if not events:
        raise CliRefuse(
            f"journal {db} has no events for study-id {study_id!r} (different study id or empty journal)",
            code=2,
        )
    if not any(e.type == "StudyCreated" for e in events):
        raise CliRefuse(f"journal {db} has no StudyCreated event for study {study_id!r}", code=2)


def _coder_task_keys(tasks: dict[str, Task]) -> frozenset[str]:
    return frozenset(t.spec.task_key for t in tasks.values() if CODER_HINT in t.spec.tools_hint)


def _refuse_undrivable_coder_tasks(rt: StudyRuntime) -> None:
    """Typed refuse for coder tasks continue can never drive to DONE.

    Coder delivery is dispatched at most once per task (run's guard: zero
    attempts only — run never CLI-executes a coder attempt), and the job
    lifecycle is process-local (``InMemoryJobStore``): a coder task left with
    attempts and no result by a kill/failure mid-job has no honest resume —
    re-invoking the declared argv on a fresh attempt would duplicate job side
    effects. Without this refuse the study is unclosable (the retry re-arms
    uncharged infra attempts forever), so refuse up front instead.
    """
    for task in rt.tasks.values():
        if CODER_HINT not in task.spec.tools_hint:
            continue
        if task.state == TaskState.DONE or not task.attempts:
            continue
        raise CliRefuse(
            f"task {task.spec.task_key!r} has a crashed/failed coder-job attempt "
            f"(attempts={len(task.attempts)}, no recorded result); coder jobs are "
            f"dispatched at most once per task (process-local job lifecycle) — "
            f"continue cannot re-drive it and the study cannot reach phase=closed",
            code=3,
        )


async def continue_study(
    *,
    data_root: Path,
    study_id: str,
    catalog: Any,
    loaded_profile: Profile,
    owner: str,
    adapter_factory: Callable[[HitlPolicy], LlmAgentAdapter],
    out: IO[str],
    catalog_version: int,
    tools: CliToolSurface,
    lease_ttl_s: float,
    lease_wait_s: float,
    max_llm_calls: int,
    max_tokens: int,
    max_tool_calls: int,
    max_ticks: int,
    coder_backend: str | None = None,
    coder_jobs: dict[str, _CoderJobDecl] | None = None,
    study_clock: Clock | None = None,
) -> int:
    """Reopen the study journal and drive it to closure (see module docstring).

    Refuse codes mirror ``run_study``: 2 usage/journal, 3 study-state, 4 agent,
    5 safety (live lease / missing hard dep), 6 max_ticks, 7 verdict ≠ fulfilled.
    """
    if not isinstance(loaded_profile, Profile):
        raise CliRefuse(
            f"loaded_profile must be Profile, got {type(loaded_profile).__name__}",
            code=2,
        )
    _assert_profile_cli_routes(loaded_profile)
    _assert_profile_tools_on_surface(loaded_profile, tools)
    if type(study_id) is not str or not study_id.strip():
        raise CliRefuse("study_id must be a non-empty str", code=2)
    if type(catalog_version) is not int:
        raise CliRefuse("catalog_version must be int", code=2)
    if not (isinstance(lease_ttl_s, (int, float)) and lease_ttl_s > 0):
        raise CliRefuse("lease_ttl_s must be > 0", code=2)
    if type(max_ticks) is not int or max_ticks < 1:
        raise CliRefuse("max_ticks must be int >= 1", code=2)
    if type(max_llm_calls) is not int or max_llm_calls < 1:
        raise CliRefuse("max_llm_calls must be int >= 1", code=2)
    if type(max_tokens) is not int or max_tokens < 1:
        raise CliRefuse("max_tokens must be int >= 1", code=2)
    if type(max_tool_calls) is not int or max_tool_calls < 0:
        raise CliRefuse("max_tool_calls must be int >= 0", code=2)
    if coder_backend is not None:
        try:
            resolve_coder_backend(coder_backend)
        except ToolError as exc:
            raise CliRefuse(f"coder backend refused: {exc.detail}", code=2) from exc
        if coder_backend == "remote-openhands":
            # Slice-2 dispatch (model-generated content + composed remote
            # runtime) is run-only; a resumed study has no runtime to compose.
            raise CliRefuse(
                "remote-openhands coder backend is not supported on continue "
                "(slice-2 dispatch is run-only; docs/remote-coder-runtime.md §21)",
                code=2,
            )
    jobs: dict[str, _CoderJobDecl] = dict(coder_jobs) if coder_jobs is not None else {}
    if jobs and coder_backend is None:
        raise CliRefuse("coder_jobs requires coder_backend (explicit backend)", code=2)

    if study_clock is not None:
        wall = study_clock
    else:
        wall = SystemClock()
    db = data_root / study_id / "events.db"
    await _probe_journal(data_root, study_id, wall)
    await _wait_for_lease_free(db, study_id, wall, wait_s=lease_wait_s)

    try:
        opened = await open_runtime(
            data_root,
            study_id,
            catalog=catalog,
            clock=wall,
            owner=owner,
            runtime_cls=StudyRuntime,
        )
    except LeaseLost as exc:
        raise CliRefuse(
            f"another writer acquired the lease for study {study_id!r} first (epoch fencing held): {exc}",
            code=5,
        ) from exc
    if not isinstance(opened, StudyRuntime):
        raise CliRefuse(
            f"open_runtime returned {type(opened).__name__}, expected StudyRuntime",
            code=4,
        )
    rt = opened
    usages: list[dict[str, object]] = []
    try:
        # Authoritative post-lease refuses (the replayed journal is the truth).
        if rt.study is None:
            raise CliRefuse(
                f"journal for study {study_id!r} has no replayed StudyCreated (foreign or corrupted journal)",
                code=3,
            )
        view = rt.view()
        if view.phase == "closed":
            raise CliRefuse(
                f"study {study_id!r} is already finalized (verdict={view.verdict!r}); "
                f"continue typed-refuses finalized studies",
                code=3,
            )
        # Catalog pin must match the journaled study (a stale --catalog would
        # otherwise silently re-drive checks against a different catalog).
        if catalog_version != rt.study.catalog_version:
            raise CliRefuse(
                f"--catalog-version {catalog_version} != journaled study "
                f"catalog_version {rt.study.catalog_version} for {study_id!r}; "
                f"refuse (no silent catalog drift)",
                code=3,
            )

        # The journaled HitlPolicy is authoritative on continue (no CLI override
        # flag: policy drift between run and continue must not be expressible).
        adapter = adapter_factory(rt.study.hitl_policy)

        await rt.ensure_lease(ttl_s=float(lease_ttl_s))
        recovery = await recover(rt)

        if rt.view().phase != "closed" and not rt.plans:
            raise CliRefuse(
                f"study {study_id!r} has no accepted plan (killed before PLAN "
                f"completed); continue resumes plan execution only — re-run "
                f"from ``coscientist run`` with the same --study-id instead",
                code=3,
            )
        _refuse_undrivable_coder_tasks(rt)

        role_budget = Budget(
            max_llm_calls=max_llm_calls,
            max_tokens=max_tokens,
            max_cost_rub=None,
            max_tool_calls=max_tool_calls,
            # Study deadline is journaled and immutable (change only via fork);
            # continue never extends it.
            deadline=rt.study.deadline,
        )
        writer_job_journal = _WriterJobJournal(rt.writer)
        registered_inputs = await _registered_user_inputs(rt)
        coder_keys = _coder_task_keys(rt.tasks)

        # A kill before the REPORT stage leaves it PENDING; continue mirrors
        # run's default (unwired) stage semantics: honest cancel, no stub run.
        report_pending = any(t.spec.task_key == "REPORT" and t.state != TaskState.DONE for t in rt.tasks.values())
        if report_pending and rt.view().phase != "closed":
            # The unwired placeholder close needs no context (report generation
            # runs post-finalize in the report lane; see run.py).
            await _optional_report_stage(rt, wired_roles=frozenset())

        ticks = 0
        executed_aids: set[str] = set()
        while rt.view().phase != "closed":
            if ticks >= max_ticks:
                raise CliRefuse(
                    f"max_ticks={max_ticks} exhausted before phase=closed "
                    f"(phase={rt.view().phase!r}, verdict={rt.view().verdict!r})",
                    code=6,
                )
            drove = await _drive_round(
                rt,
                adapter=adapter,
                study_id=study_id,
                tools=tools,
                role_budget=role_budget,
                registered_inputs=registered_inputs,
                writer_job_journal=writer_job_journal,
                coder_backend=coder_backend,
                coder_jobs=jobs,
                coder_task_keys=coder_keys,
                usages=usages,
                executed_aids=executed_aids,
            )
            ticks += 1
            if drove == 0:
                if _core_retry_pending(rt):
                    # The tick's ``_fire_due_retries`` owns arming the retry
                    # (backoff RETRY_BACKOFF_S); idle DB ticks are faster than
                    # that backoff, so wait instead of burning max_ticks.
                    await asyncio.sleep(_RETRY_POLL_S)
                await rt.ensure_lease(ttl_s=float(lease_ttl_s))
                await rt.tick()

        final_view = rt.view()
        out.write(
            json.dumps(
                {
                    "study_id": study_id,
                    "phase": final_view.phase,
                    "verdict": final_view.verdict,
                    "usage": usages,
                    "recovery": recovery,
                },
                ensure_ascii=False,
            )
            + "\n"
        )
        if final_view.verdict != "fulfilled":
            raise CliRefuse(
                f"study closed with verdict={final_view.verdict!r} (want fulfilled)",
                code=7,
            )
        return 0
    finally:
        await close_runtime(rt)


async def _drive_round(
    rt: StudyRuntime,
    *,
    adapter: LlmAgentAdapter,
    study_id: str,
    tools: CliToolSurface,
    role_budget: Budget,
    registered_inputs: tuple[Any, ...],
    writer_job_journal: _WriterJobJournal,
    coder_backend: str | None,
    coder_jobs: dict[str, _CoderJobDecl],
    coder_task_keys: frozenset[str],
    usages: list[dict[str, object]],
    executed_aids: set[str],
) -> int:
    """One re-drive pass — the resume mirror of run's ``_drive_plan_attempts``.

    Returns the number of attempts driven. Dispatch shape is IDENTICAL to run:
    - CLI-started FIRST attempts only for dispatchable tasks with **no
      attempts at all** (retries are always core-fired by the tick's
      ``_fire_due_retries`` — tasks in retry backoff are left to the tick);
    - open attempts not yet executed by this continue session (core-fired
      retries) run exactly once via ``executed_aids``.

    ``rt`` holds the fenced lease, so an open unexecuted attempt can only be
    a retry nobody is executing — driving it IS the resume (no double
    execution: a closed attempt refuses at ``Executor.record``).
    """
    allow_tools = list(tools.named_tools())
    drove = 0
    for task in rt.iter_dispatchable_tasks():
        if task.attempts:
            # Closed attempts exist: the next attempt is core-fired (retry
            # backoff / budget bounds) — never CLI-started (same as run).
            continue
        _refuse_stage_task(task)
        if CODER_HINT in task.spec.tools_hint:
            # Coder dispatch starts its ONE attempt internally
            # (_dispatch_coder_task → _require_attempt) — pre-starting here
            # would raise "already has open attempt" (N1 root: run only ever
            # dispatches coder for zero-attempt tasks for this same reason).
            await _drive_attempt(
                rt,
                task=task,
                aid=None,
                adapter=adapter,
                study_id=study_id,
                tools=tools,
                allow_tools=allow_tools,
                role_budget=role_budget,
                registered_inputs=registered_inputs,
                writer_job_journal=writer_job_journal,
                coder_backend=coder_backend,
                coder_jobs=coder_jobs,
                coder_task_keys=coder_task_keys,
                usages=usages,
                executed_aids=executed_aids,
            )
            drove += 1
            continue
        await _drive_attempt(
            rt,
            task=task,
            aid=await _require_attempt(rt, task.spec.task_key),
            adapter=adapter,
            study_id=study_id,
            tools=tools,
            allow_tools=allow_tools,
            role_budget=role_budget,
            registered_inputs=registered_inputs,
            writer_job_journal=writer_job_journal,
            coder_backend=coder_backend,
            coder_jobs=coder_jobs,
            coder_task_keys=coder_task_keys,
            usages=usages,
            executed_aids=executed_aids,
        )
        drove += 1
    for task in list(rt.tasks.values()):
        if task.state == TaskState.DONE:
            continue
        if CODER_HINT in task.spec.tools_hint:
            # Defensive (N1): coder attempts are never CLI-executed (run
            # parity — dispatching here would start_attempt over an already
            # open retry attempt → raw RuntimeError). Undrivable coder states
            # refuse up front in continue_study.
            continue
        fresh = [a for a in task.attempts if a not in executed_aids and attempt_row_is_open(rt.attempts, a)]
        if not fresh:
            continue
        _refuse_stage_task(task)
        for aid in fresh:
            await _drive_attempt(
                rt,
                task=task,
                aid=aid,
                adapter=adapter,
                study_id=study_id,
                tools=tools,
                allow_tools=allow_tools,
                role_budget=role_budget,
                registered_inputs=registered_inputs,
                writer_job_journal=writer_job_journal,
                coder_backend=coder_backend,
                coder_jobs=coder_jobs,
                coder_task_keys=coder_task_keys,
                usages=usages,
                executed_aids=executed_aids,
            )
            drove += 1
    return drove


def _refuse_stage_task(task: Task) -> None:
    if task.spec.task_key in _STAGE_TASK_KEYS:
        raise CliRefuse(
            f"stage task {task.spec.task_key!r} is unfinished; continue does not "
            f"re-run stage roles (tz_spec / planner / optional HYP/LIT/REPORT) — "
            f"re-run from ``coscientist run`` with the same --study-id",
            code=3,
        )


async def _drive_attempt(
    rt: StudyRuntime,
    *,
    task: Task,
    aid: str | None,
    adapter: LlmAgentAdapter,
    study_id: str,
    tools: CliToolSurface,
    allow_tools: list[Any],
    role_budget: Budget,
    registered_inputs: tuple[Any, ...],
    writer_job_journal: _WriterJobJournal,
    coder_backend: str | None,
    coder_jobs: dict[str, _CoderJobDecl],
    coder_task_keys: frozenset[str],
    usages: list[dict[str, object]],
    executed_aids: set[str],
) -> None:
    spec = task.spec
    if not spec_redrivable(spec):
        raise CliRefuse(
            f"task {spec.task_key!r} spec is not CLI-re-drivable (core-owned fields "
            f"present; digest roundtrip failed) — refuse (no silent spec rewrite)",
            code=3,
        )
    draft = draft_from_task_spec(spec)
    if CODER_HINT in spec.tools_hint:
        if coder_backend is None:
            raise CliRefuse(
                f"task {spec.task_key!r} needs code execution but --coder-backend is not set; refuse (no stub science)",
                code=5,
            )
        assert rt.study is not None
        del aid  # coder dispatch starts its single attempt internally
        decl = coder_jobs.get(spec.task_key)
        if decl is None and len(coder_jobs) == 1:
            # Single declared job + single resumable coder task: positional
            # (planner keys are unknowable; same contract as run's binder).
            decl = next(iter(coder_jobs.values()))
        if decl is None:
            raise CliRefuse(
                f"task {spec.task_key!r} cannot be bound to declared coder jobs "
                f"(--coder-jobs keys={sorted(coder_jobs)!r})",
                code=3,
            )
        await _dispatch_coder_task(
            rt,
            draft=draft,
            study_id=study_id,
            backend=resolve_coder_backend(coder_backend),
            decl=decl,
            writer_journal=writer_job_journal,
            clock=rt.clock,
            hitl_policy=rt.study.hitl_policy,
            deadline=rt.study.deadline,
            usages=usages,
        )
        return
    # Same pre-checks + typed failure mapping as run (#219): drift refusals
    # rc=4 before any adapter call; AgentError/ToolError/LlmError close the
    # attempt retryable via the core (budget AgentError stays terminal rc=4).
    planned = (
        None if isinstance(tools, LiveCliToolSurface) and tools.automatic else _resolve_planned_calls(tools, draft)
    )
    assert aid is not None, "executor re-drive requires a started attempt id"
    executed_aids.add(aid)
    await _execute_executor_attempt(
        rt,
        adapter=adapter,
        draft=draft,
        aid=aid,
        planned=planned,
        study_id=study_id,
        registered_inputs=registered_inputs,
        allow_tools=allow_tools,
        role_budget=role_budget,
        usages=usages,
    )


__all__ = [
    "continue_study",
    "draft_from_task_spec",
    "spec_redrivable",
]
