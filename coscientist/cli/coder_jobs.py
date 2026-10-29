"""Coder job declarations: parse ``--coder-jobs``, bind, dep transfer, journal bridge.

Extracted from ``coscientist.cli.run`` so the study loop stays readable. Public
symbols are re-exported from ``coscientist.cli.run`` for existing importers.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from coscientist.agents.derive import CODER_HINT
from coscientist.agents.schemas import PlanOut, TaskSpecDraft
from coscientist.api.runtime import StudyRuntime
from coscientist.checks.sandbox import SandboxLimits, limits_from_check
from coscientist.cli.errors import CliRefuse
from coscientist.cli.study_helpers import _require_attempt
from coscientist.core.events.clock import Clock
from coscientist.core.events.errors import CatalogError
from coscientist.core.events.events import UnstampedEvent
from coscientist.core.events.writer import StudyWriter
from coscientist.core.execution.call_ctx import CallCtx
from coscientist.core.execution.domain import AgentTaskOutput, ArtifactHandle, HitlPolicy
from coscientist.tools.coder import CoderOutputSpec, CoderRequest, invoke_coder_for_study
from coscientist.tools.coder.local import CoderBackend
from coscientist.tools.coder.run import CoderOutcome
from coscientist.tools.contract import ToolError
from coscientist.tools.jobs import InMemoryJobStore


@dataclass(frozen=True)
class _CoderJobDecl:
    """One declared coder job (``--coder-jobs`` JSON row; nothing invented)."""

    argv: tuple[str, ...]
    inputs: dict[str, bytes]
    env: dict[str, str]
    outputs: tuple[CoderOutputSpec, ...]
    limits: SandboxLimits


def _parse_coder_job_decl(task_key: str, raw: object) -> _CoderJobDecl:
    """Fail-closed parse of one ``--coder-jobs`` row (exact keys, typed values)."""
    if not isinstance(raw, Mapping):
        raise CliRefuse(f"--coder-jobs[{task_key!r}] must be a JSON object", code=2)
    for key in ("argv", "outputs", "limits"):
        if key not in raw:
            raise CliRefuse(f"--coder-jobs[{task_key!r}] missing required key {key!r}", code=2)
    argv_raw = raw["argv"]
    if not isinstance(argv_raw, list) or not argv_raw:
        raise CliRefuse(f"--coder-jobs[{task_key!r}].argv must be a non-empty list", code=2)
    argv: list[str] = []
    for token in argv_raw:
        if type(token) is not str or not token:
            raise CliRefuse(f"--coder-jobs[{task_key!r}].argv entries must be non-empty str", code=2)
        argv.append(token)
    inputs_raw = raw.get("inputs")
    if inputs_raw is None:
        inputs_raw = {}
    env_raw = raw.get("env")
    if env_raw is None:
        env_raw = {}
    if not isinstance(inputs_raw, Mapping):
        raise CliRefuse(f"--coder-jobs[{task_key!r}].inputs must be a JSON object", code=2)
    if not isinstance(env_raw, Mapping):
        raise CliRefuse(f"--coder-jobs[{task_key!r}].env must be a JSON object", code=2)
    inputs: dict[str, bytes] = {}
    for name, content in inputs_raw.items():
        if type(name) is not str or not name:
            raise CliRefuse(f"--coder-jobs[{task_key!r}].inputs keys must be non-empty str", code=2)
        if type(content) is not str:
            raise CliRefuse(
                f"--coder-jobs[{task_key!r}].inputs[{name!r}] must be a str "
                f"(UTF-8 text; binary inputs are not declared this way)",
                code=2,
            )
        inputs[name] = content.encode("utf-8")
    env: dict[str, str] = {}
    for name, value in env_raw.items():
        if type(name) is not str or not name or type(value) is not str:
            raise CliRefuse(f"--coder-jobs[{task_key!r}].env entries must be str → str", code=2)
        env[name] = value
    outputs_raw = raw["outputs"]
    if not isinstance(outputs_raw, list) or not outputs_raw:
        raise CliRefuse(f"--coder-jobs[{task_key!r}].outputs must be a non-empty list", code=2)
    outputs: list[CoderOutputSpec] = []
    for row in outputs_raw:
        if not isinstance(row, Mapping):
            raise CliRefuse(f"--coder-jobs[{task_key!r}].outputs entries must be JSON objects", code=2)
        source = row.get("source")
        if source not in ("stdout", "stderr"):
            raise CliRefuse(
                f"--coder-jobs[{task_key!r}].outputs[].source must be 'stdout'|'stderr'",
                code=2,
            )
        name = row.get("name")
        media_type = row.get("media_type")
        if type(name) is not str or type(media_type) is not str:
            raise CliRefuse(
                f"--coder-jobs[{task_key!r}].outputs[] 'name'/'media_type' must be str",
                code=2,
            )
        try:
            outputs.append(CoderOutputSpec(name=name, source=source, media_type=media_type))
        except ToolError as exc:
            raise CliRefuse(f"--coder-jobs[{task_key!r}].outputs[] refused: {exc.detail}", code=2) from exc
    limits_raw = raw["limits"]
    if not isinstance(limits_raw, Mapping):
        raise CliRefuse(f"--coder-jobs[{task_key!r}].limits must be a JSON object", code=2)
    try:
        limits = limits_from_check(**dict(limits_raw))
    except TypeError as exc:
        raise CliRefuse(
            f"--coder-jobs[{task_key!r}].limits keys refused (exactly the 9 budget keys; no extras): {exc}",
            code=2,
        ) from exc
    except CatalogError as exc:
        raise CliRefuse(f"--coder-jobs[{task_key!r}].limits refused: {exc}", code=2) from exc
    return _CoderJobDecl(
        argv=tuple(argv),
        inputs=inputs,
        env=env,
        outputs=tuple(outputs),
        limits=limits,
    )


def _parse_coder_jobs(raw: object) -> dict[str, _CoderJobDecl]:
    """Parse the ``--coder-jobs`` JSON object (task_key → declared job)."""
    if not isinstance(raw, Mapping):
        raise CliRefuse("--coder-jobs must be a JSON object (task_key → job)", code=2)
    decls: dict[str, _CoderJobDecl] = {}
    for task_key, row in raw.items():
        if type(task_key) is not str or not task_key.strip():
            raise CliRefuse("--coder-jobs keys must be non-empty str", code=2)
        decls[task_key] = _parse_coder_job_decl(task_key, row)
    return decls


class _WriterJobJournal:
    """Sync ``JobJournal`` → async ``StudyWriter.append`` (drained by the dispatcher).

    ``InMemoryJobStore`` emits sync; the study journal is an asyncio queue. Each
    event becomes a writer task; ``drain`` must complete before the dispatch
    path returns so Job* events are durably journaled in order with the effect
    events ``invoke_tool`` journals through the same writer.
    """

    def __init__(self, writer: StudyWriter) -> None:
        self._writer = writer
        self._pending: set[asyncio.Task[object]] = set()

    def record(self, event: UnstampedEvent) -> None:
        task = asyncio.get_running_loop().create_task(self._writer.append(event))
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)

    async def drain(self, *, strict: bool) -> None:
        while self._pending:
            pending = tuple(self._pending)
            if strict:
                await asyncio.gather(*pending)
            else:
                await asyncio.gather(*pending, return_exceptions=True)


def _coder_hitl_policy(policy: HitlPolicy) -> HitlPolicy:
    """Coder dispatch policy: the operator's explicit ``--coder-backend`` allow.

    The write-effect ``side_effect`` gate (DESIGN_M1 §8) needs an explicit allow;
    passing ``--coder-backend`` IS that operator decision for exactly the coder
    tool — scoped to this ctx, never written back into the study policy.
    """
    return HitlPolicy(
        mode=policy.mode,
        defaults={**policy.defaults, "side_effect": "allow"},
    )


def _bind_coder_jobs(
    plan_out: PlanOut,
    jobs_decl: Mapping[str, _CoderJobDecl],
) -> dict[str, _CoderJobDecl]:
    """Bind planner coder tasks to declared jobs (computed ONCE per plan).

    Exact ``--coder-jobs`` keys win; remaining tasks/jobs with EQUAL counts
    bind by plan order — the operator declares jobs in the order the task
    describes them, and planner keys are unknowable in advance. A crossed
    positional assignment fails honestly later (declared-output coverage,
    job semantics — e.g. hash comparisons), never silently. Counts differing
    without full key coverage refuse before any dispatch.
    """
    order = [d.task_key for d in plan_out.tasks if CODER_HINT in d.tools_hint]
    bound: dict[str, _CoderJobDecl] = {}
    unmatched_keys = [k for k in order if k not in jobs_decl]
    matched = {k: v for k, v in jobs_decl.items() if k in set(order)}
    used_ids = {id(v) for v in matched.values()}
    remaining = [v for k, v in jobs_decl.items() if k not in set(order) and id(v) not in used_ids]
    if len(unmatched_keys) == len(remaining):
        for key, value in zip(unmatched_keys, remaining, strict=False):
            matched[key] = value
    for key in order:
        if key in matched:
            bound[key] = matched[key]
    missing = [k for k in order if k not in bound]
    if missing:
        raise CliRefuse(
            f"coder tasks {missing!r} cannot be bound to declared jobs "
            f"(--coder-jobs keys={sorted(jobs_decl)!r}); equal counts bind by "
            f"plan order, otherwise keys must match",
            code=3,
        )
    return bound


def _dep_transfer_inputs(rt: StudyRuntime, draft: TaskSpecDraft) -> dict[str, bytes]:
    """Recorded outputs of the task's ``depends_on`` tasks as job inputs.

    Task-to-task file transfer (file_handoff semantics): a coder job receives
    the producer tasks' recorded output artifacts in ``depends_on`` order,
    named ``dep{N}_{output}`` (N is 1-based, output = expected-output name).
    Bytes come from the CAS via the journal fold — never model claims; a dep
    without a recorded output contributes nothing (the job's own semantics
    fail honestly on the missing file).
    """
    transfer: dict[str, bytes] = {}
    if rt.executor is None:
        return transfer
    for n, dep_key in enumerate(draft.depends_on, start=1):
        outputs = rt.dep_outputs.get(dep_key)
        if not outputs:
            continue
        for output_name, artifact_id in outputs.items():
            art = rt.executor.artifacts.get_meta(str(artifact_id))
            if art is None:
                continue
            transfer[f"dep{n}_{output_name}"] = rt.executor.artifacts.read_cas(art.sha256)
    return transfer

# Route prefixes that map to non-experiment task kinds (core ``_prefix_for_route``);
# code execution is experiment delivery only.
_NON_EXPERIMENT_ROUTE_PREFIXES = (
    "literature",
    "lit",
    "hypotheses",
    "hypothesis",
    "hyp",
    "planner",
    "plan",
)


async def _dispatch_coder_task(
    rt: StudyRuntime,
    *,
    draft: TaskSpecDraft,
    study_id: str,
    backend: CoderBackend,
    decl: _CoderJobDecl,
    writer_journal: _WriterJobJournal,
    clock: Clock,
    hitl_policy: HitlPolicy,
    deadline: datetime,
    usages: list[dict[str, object]],
) -> None:
    """Dispatch one code-execution task via ``invoke_coder_for_study`` (honest path).

    Typed refuses (fail-closed, no stub science): mixed hints, non-experiment
    route, missing declared job, expected outputs not covered by the declared
    job outputs, non-succeeded job outcome. Job artifacts are registered by the
    coder backend itself (sandbox producer) — model-invented values never enter.
    """
    if len(draft.tools_hint) != 1:
        raise CliRefuse(
            f"task {draft.task_key!r} tools_hint {list(draft.tools_hint)!r}: "
            f"code-execution delivery ({CODER_HINT!r}) must be the only hint",
            code=3,
        )
    route = draft.route.strip().lower()
    if route in _NON_EXPERIMENT_ROUTE_PREFIXES:
        raise CliRefuse(
            f"task {draft.task_key!r} route {draft.route!r} is a non-experiment "
            f"kind; code execution is experiment delivery only",
            code=3,
        )
    # Binding was computed once per plan (see _bind_coder_jobs); the bound
    # declaration arrives as a parameter. Nothing to resolve here anymore.
    missing_outputs = [o.name for o in draft.expected_outputs if o.name not in {s.name for s in decl.outputs}]
    if missing_outputs:
        raise CliRefuse(
            f"task {draft.task_key!r} expected outputs {missing_outputs!r} not declared "
            f"in --coder-jobs[{draft.task_key!r}].outputs "
            f"(declared={[s.name for s in decl.outputs]!r})",
            code=3,
        )
    if rt.executor is None:
        raise CliRefuse("runtime has no executor", code=4)
    aid = await _require_attempt(rt, draft.task_key)

    def _attempt_is_closed() -> bool:
        attempt = rt.attempts.get(aid)
        if isinstance(attempt, Mapping):
            closed = attempt.get("closed")
            if isinstance(closed, bool):
                return closed
        return False

    transfer = _dep_transfer_inputs(rt, draft)
    request = CoderRequest(
        attempt_id=aid,
        call_id=f"{aid}:coder",
        argv=decl.argv,
        inputs={**transfer, **decl.inputs},
        env=decl.env,
        limits=decl.limits,
        deadline=deadline,
        outputs=decl.outputs,
        attempt_is_closed=_attempt_is_closed,
    )
    jobs = InMemoryJobStore(
        study_id=study_id,
        clock=clock,
        journal=writer_journal,
        on_clock_change=None,
    )
    ctx = CallCtx(
        study_id=study_id,
        task_key=draft.task_key,
        attempt_id=aid,
        hitl_policy=_coder_hitl_policy(hitl_policy),
    )
    try:
        outcome, tool_out = await invoke_coder_for_study(
            request,
            backend=backend,
            jobs=jobs,
            writer=rt.writer,
            artifacts=rt.executor.artifacts,
            ctx=ctx,
        )
    except ToolError as exc:
        await writer_journal.drain(strict=False)
        raise CliRefuse(
            f"coder invoke for task {draft.task_key!r} refused (backend={backend!r}): {exc.detail}",
            code=4,
        ) from exc
    except BaseException:  # audit-ok[broad_except]: best-effort drain before re-raise, exception propagates
        await writer_journal.drain(strict=False)
        raise
    await _finish_coder_dispatch(rt, draft=draft, aid=aid, outcome=outcome, tool_out=tool_out,
                                 usages=usages, writer_journal=writer_journal)


async def _finish_coder_dispatch(
    rt: StudyRuntime,
    *,
    draft: TaskSpecDraft,
    aid: str,
    outcome: CoderOutcome,
    tool_out: Any,
    usages: list[dict[str, object]],
    writer_journal: _WriterJobJournal,
) -> None:
    """Shared coder-dispatch tail: drain, honest refuse, record outputs."""
    assert rt.executor is not None  # both dispatchers check before invoking
    await writer_journal.drain(strict=True)
    if outcome.status != "succeeded":
        raise CliRefuse(
            f"coder job for task {draft.task_key!r} {outcome.status} "
            f"(retryable={outcome.retryable}, detail={outcome.detail!r}) — refuse",
            code=4,
        )
    outputs: dict[str, ArtifactHandle] = {}
    for art in tool_out.artifacts:
        outputs[art.name] = ArtifactHandle(artifact_id=art.artifact_id, name=art.name)
    await rt.executor.record(
        aid,
        AgentTaskOutput(
            summary=f"coder job {outcome.job_id}: {outcome.detail}",
            outputs=outputs,
            values=outcome.model_dump(mode="json"),
            claims=[],
        ),
    )
    usages.append(
        {
            "role": "coder",
            "task_key": draft.task_key,
            "job_id": outcome.job_id,
            "status": outcome.status,
            "artifact_ids": list(outcome.artifact_ids),
        }
    )
