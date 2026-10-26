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
    AgentRun,
    ExecutorRouteIn,
    OutputSpecDraft,
    RubricDraft,
    RubricItemDraft,
    TaskSpecDraft,
)
from coscientist.api.runtime import StudyRuntime
from coscientist.cli.errors import CliRefuse
from coscientist.cli.journal import record_executor_route
from coscientist.cli.run import (
    CliToolSurface,
    _assert_profile_cli_routes,
    _assert_profile_tools_on_surface,
    _CoderJobDecl,
    _dispatch_coder_task,
    _optional_report_stage,
    _registered_user_inputs,
    _require_attempt,
    _to_task_spec,
    _usage_row,
    _WriterJobJournal,
)
from coscientist.core.events.attempt_fields import attempt_closed
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
            f"journal {db} has no events for study-id {study_id!r} "
            f"(different study id or empty journal)",
            code=2,
        )
    if not any(e.type == "StudyCreated" for e in events):
        raise CliRefuse(f"journal {db} has no StudyCreated event for study {study_id!r}", code=2)


def _coder_task_keys(tasks: dict[str, Task]) -> frozenset[str]:
    return frozenset(
        t.spec.task_key for t in tasks.values() if CODER_HINT in t.spec.tools_hint
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
            f"another writer acquired the lease for study {study_id!r} first "
            f"(epoch fencing held): {exc}",
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
                f"journal for study {study_id!r} has no replayed StudyCreated "
                f"(foreign or corrupted journal)",
                code=3,
            )
        view = rt.view()
        if view.phase == "closed":
            raise CliRefuse(
                f"study {study_id!r} is already finalized (verdict={view.verdict!r}); "
                f"continue typed-refuses finalized studies",
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
        report_pending = any(
            t.spec.task_key == "REPORT" and t.state != TaskState.DONE
            for t in rt.tasks.values()
        )
        if report_pending and rt.view().phase != "closed":
            from coscientist.agents.schemas import StudyContext

            ctx = StudyContext(
                study_id=study_id,
                request=rt.study.request,
                profile=rt.study.profile,
                criterion_template_ids=tuple(
                    sorted(t.id for t in catalog.templates)
                ),
                tool_ids=tuple(sorted(tools.tool_names)),
                inputs=registered_inputs,
            )
            await _optional_report_stage(
                rt,
                adapter=adapter,
                ctx=ctx,
                role_budget=role_budget,
                usages=usages,
                wired_roles=frozenset(),
            )

        ticks = 0
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
            )
            ticks += 1
            if drove == 0:
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
) -> int:
    """One re-drive pass: dispatchable tasks (start+drive), then open attempts.

    Returns the number of tasks driven. ``rt`` holds the fenced lease, so an
    open attempt on an unfinished task can only be a tick-fired retry nobody
    is executing yet — driving it IS the resume (no double execution: a closed
    attempt refuses at ``Executor.record``).
    """
    allowed_tool_names = tools.tool_names
    allow_tools = list(tools.named_tools())
    drove = 0
    for task in rt.iter_dispatchable_tasks():
        _refuse_stage_task(task)
        aid = await _require_attempt(rt, task.spec.task_key)
        await _drive_attempt(
            rt,
            task=task,
            aid=aid,
            adapter=adapter,
            study_id=study_id,
            tools=tools,
            allowed_tool_names=allowed_tool_names,
            allow_tools=allow_tools,
            role_budget=role_budget,
            registered_inputs=registered_inputs,
            writer_job_journal=writer_job_journal,
            coder_backend=coder_backend,
            coder_jobs=coder_jobs,
            coder_task_keys=coder_task_keys,
            usages=usages,
        )
        drove += 1
    if drove:
        return drove
    for task in list(rt.tasks.values()):
        if task.state == TaskState.DONE:
            continue
        open_aids = [
            a
            for a in task.attempts
            if a in rt.attempts and not attempt_closed(rt.attempts[a])
        ]
        if not open_aids:
            continue
        _refuse_stage_task(task)
        await _drive_attempt(
            rt,
            task=task,
            aid=open_aids[0],
            adapter=adapter,
            study_id=study_id,
            tools=tools,
            allowed_tool_names=allowed_tool_names,
            allow_tools=allow_tools,
            role_budget=role_budget,
            registered_inputs=registered_inputs,
            writer_job_journal=writer_job_journal,
            coder_backend=coder_backend,
            coder_jobs=coder_jobs,
            coder_task_keys=coder_task_keys,
            usages=usages,
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
    aid: str,
    adapter: LlmAgentAdapter,
    study_id: str,
    tools: CliToolSurface,
    allowed_tool_names: frozenset[str],
    allow_tools: list[Any],
    role_budget: Budget,
    registered_inputs: tuple[Any, ...],
    writer_job_journal: _WriterJobJournal,
    coder_backend: str | None,
    coder_jobs: dict[str, _CoderJobDecl],
    coder_task_keys: frozenset[str],
    usages: list[dict[str, object]],
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
                f"task {spec.task_key!r} needs code execution but --coder-backend "
                f"is not set; refuse (no stub science)",
                code=5,
            )
        assert rt.study is not None
        await _dispatch_coder_task(
            rt,
            draft=draft,
            study_id=study_id,
            backend=resolve_coder_backend(coder_backend),
            jobs_decl=coder_jobs,
            coder_task_keys=coder_task_keys,
            writer_journal=writer_job_journal,
            clock=rt.clock,
            hitl_policy=rt.study.hitl_policy,
            deadline=rt.study.deadline,
            usages=usages,
        )
        return
    unknown = [h for h in spec.tools_hint if h not in allowed_tool_names]
    if unknown:
        raise CliRefuse(
            f"task {spec.task_key!r} tools_hint {unknown!r} not in router allow-list",
            code=3,
        )
    try:
        planned = tools.planned_calls(spec.tools_hint)
    except KeyError as exc:
        missing = exc.args[0]
        raise CliRefuse(
            f"task {spec.task_key!r} tools_hint missing declared call args for "
            f"{missing!r} (fail-closed before record)",
            code=3,
        ) from exc
    exec_run: AgentRun = await adapter.run(
        "executor-route",
        ExecutorRouteIn(
            task=draft,
            study_id=study_id,
            attempt_id=aid,
            tool_args=planned,
            inputs=registered_inputs,
        ),
        allow_tools,
        role_budget,
    )
    usages.append(_usage_row("executor-route", exec_run, task_key=spec.task_key))
    await record_executor_route(rt, aid, exec_run)


__all__ = [
    "continue_study",
    "draft_from_task_spec",
    "spec_redrivable",
]
