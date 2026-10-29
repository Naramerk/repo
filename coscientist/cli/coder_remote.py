"""Remote-openhands coder path: settings, policy, generation, dispatch.

Extracted from ``coscientist.cli.run``. Callers import from this module directly
(no shim re-exports).
"""

from __future__ import annotations

import argparse
from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path

from coscientist.agents.adapter import LlmAgentAdapter
from coscientist.agents.derive import CODER_HINT
from coscientist.agents.prompts.roles import ROLE_REMOTE_CODER_REF
from coscientist.agents.schemas import (
    AgentError,
    RegisteredInput,
    RemoteCoderGenIn,
    RemoteCoderTaskSpec,
    TaskSpecDraft,
)
from coscientist.api.runtime import StudyRuntime
from coscientist.cli.coder_jobs import (
    _NON_EXPERIMENT_ROUTE_PREFIXES,
    _coder_hitl_policy,
    _finish_coder_dispatch,
    _WriterJobJournal,
)
from coscientist.cli.errors import CliRefuse
from coscientist.cli.study_helpers import _require_attempt
from coscientist.core.events.clock import Clock
from coscientist.core.execution.call_ctx import CallCtx
from coscientist.core.execution.domain import Budget, HitlPolicy
from coscientist.llm.errors import LlmError
from coscientist.tools.coder import invoke_coder_for_study
from coscientist.tools.coder.remote_contract import (
    RemoteCoderError,
    RemoteCoderTaskRequest,
    RemoteOutput,
    endpoint,
)
from coscientist.tools.coder.remote_runtime import (
    RemoteCoderRuntime,
    RemoteCoderSettings,
)
from coscientist.tools.coder.remote_wire import RemoteLimits
from coscientist.tools.contract import ToolError
from coscientist.tools.jobs import InMemoryJobStore
from coscientist.tools.mcp_live import open_cli_http_transport

_REMOTE_CODER_LIMITS = RemoteLimits(
    # One exchange each: POST /run may queue server-side (wait=true), so its
    # budget is generous; status/download exchanges are short. Explicit here,
    # never invented per call (docs/remote-coder-runtime.md §20).
    request_bytes=262_144,
    response_bytes=1_048_576,
    download_bytes=16_777_216,
    read_chunk_bytes=65_536,
    json_depth=32,
    timeout_s=600.0,
    close_timeout_s=10.0,
)


def _remote_coder_settings_from_args(args: argparse.Namespace) -> RemoteCoderSettings | None:
    """Explicit remote-coder config from CLI flags; typed refusals on drift.

    No endpoint, header or limit is invented. Header values are carried only in
    memory (the settings digest stores value fingerprints, never values).
    """
    remote_headers: dict[str, str] = {}
    raw_headers = getattr(args, "coder_remote_header", None) or ()  # audit-ok[or_dict]: tests hand-build Namespace
    for item in raw_headers:
        name, sep, value = item.partition(":")  # audit-ok[str_partition]: first colon; values may contain colons
        name = name.strip()
        value = value.strip()
        if not sep or not name or not value or name.strip() != name or any(ord(c) < 32 for c in name + value):
            raise CliRefuse(
                f"--coder-remote-header must be 'NAME:VALUE' bare, got {item!r}",
                code=2,
            )
        if name in remote_headers:
            raise CliRefuse(f"duplicate --coder-remote-header name {name!r}", code=2)
        remote_headers[name] = value
    remote_backend = args.coder_backend == "remote-openhands"
    remote_url = getattr(args, "coder_remote_url", None)
    if not remote_backend:
        if remote_url is not None or remote_headers:
            raise CliRefuse(
                "--coder-remote-url/--coder-remote-header are only valid with "
                "--coder-backend remote-openhands",
                code=2,
            )
        return None
    if remote_url is None:
        raise CliRefuse(
            "--coder-backend remote-openhands requires --coder-remote-url "
            "(no endpoint is ever invented)",
            code=2,
        )
    return RemoteCoderSettings(
        base_url=remote_url,
        headers=remote_headers,
        limits=_REMOTE_CODER_LIMITS,
        max_receipts=256,
        # Report-assembly jobs register many artifact receipts; a 1 MiB cap
        # tripped on wave-9 (st_idem_2129: 19 receipts crossed 1 MiB on the
        # final report) — the store already caps runaway jobs via count.
        max_receipt_bytes=16 * 1024 * 1024,
        poll_interval_s=2.0,
        http_factory=open_cli_http_transport,
    )


class _StudyRemoteCoderPolicy:
    """Injected submit gate: endpoint/policy binding + study-budget deadline."""

    def __init__(self, *, expected_endpoint: str, expected_policy_digest: str, study_deadline: datetime) -> None:
        self._endpoint = expected_endpoint
        self._policy_digest = expected_policy_digest
        self._study_deadline = study_deadline

    def check(self, identity: Mapping[str, object]) -> None:
        if identity.get("endpoint") != self._endpoint or identity.get("policy_digest") != self._policy_digest:
            raise RemoteCoderError("remote_policy_identity_mismatch")
        deadline = identity.get("deadline")
        if not isinstance(deadline, str):
            raise RemoteCoderError("remote_policy_deadline_invalid")
        try:
            parsed = datetime.fromisoformat(deadline)
        except ValueError as exc:
            raise RemoteCoderError("remote_policy_deadline_invalid") from exc
        if parsed.tzinfo is None or parsed > self._study_deadline:
            raise RemoteCoderError("remote_policy_deadline_exceeded")


def compose_remote_coder_runtime(
    settings: RemoteCoderSettings,
    *,
    store_root: Path,
    study_deadline: datetime,
    clock: Clock,
) -> RemoteCoderRuntime:
    """Compose the runtime from explicit settings (transport via tools.mcp_live)."""
    return RemoteCoderRuntime(
        wire=settings.open_wire(clock=clock),
        store=settings.open_store(store_root),
        policy=_StudyRemoteCoderPolicy(
            expected_endpoint=endpoint(settings.base_url),
            expected_policy_digest=settings.policy_digest,
            study_deadline=study_deadline,
        ),
        policy_digest=settings.policy_digest,
        poll_interval_s=settings.poll_interval_s,
    )


def _validate_generated_coverage(draft: TaskSpecDraft, spec: RemoteCoderTaskSpec) -> None:
    """Generated task coverage vs the frozen expected outputs (fail-closed backstop).

    The generation response schema pins ``outputs[].name`` to the task's
    declared expected-output names (``remote_coder_task_model_for``), so name
    invention and required-output omission cannot normally reach this check —
    it stays as the honest typed refusal for direct-spec paths and as the
    load-bearing coverage gate for schema-permitted SUBSETS (the enum pins
    membership, not coverage: a model may still omit a required output and
    must die here).
    """
    expected_media = {o.name: o.media_type for o in draft.expected_outputs}
    required = {o.name for o in draft.expected_outputs if o.required}
    generated = {o.name: o for o in spec.outputs}
    missing = sorted(required - set(generated))
    if missing:
        raise CliRefuse(
            f"remote coder generation for task {draft.task_key!r} does not cover "
            f"required outputs {missing!r} (generated={sorted(generated)!r})",
            code=3,
        )
    unknown = sorted(set(generated) - set(expected_media))
    if unknown:
        raise CliRefuse(
            f"remote coder generation for task {draft.task_key!r} invented outputs "
            f"{unknown!r} (expected={sorted(expected_media)!r})",
            code=3,
        )
    mismatched = sorted(name for name, o in generated.items() if o.media_type != expected_media[name])
    if mismatched:
        raise CliRefuse(
            f"remote coder generation for task {draft.task_key!r} media_type mismatch on "
            f"{mismatched!r}",
            code=3,
        )


async def _dispatch_remote_coder_task(
    rt: StudyRuntime,
    *,
    draft: TaskSpecDraft,
    study_id: str,
    adapter: LlmAgentAdapter,
    runtime: RemoteCoderRuntime,
    writer_journal: _WriterJobJournal,
    clock: Clock,
    hitl_policy: HitlPolicy,
    deadline: datetime,
    usages: list[dict[str, object]],
    role_budget: Budget,
    registered_inputs: Sequence[RegisteredInput],
) -> None:
    """Dispatch one coder task with MODEL-AUTHORED content on the remote backend.

    The model generates the task text and maps every required expected output
    to a sandbox workspace path (one bounded structured call, executor-route
    role model). Dispatch validates coverage against the frozen expected
    outputs, then submits through ``invoke_tool(coder)`` with the composed
    remote runtime (docs/remote-coder-runtime.md §2/§5). No operator
    ``coder_jobs`` is consulted on this path.
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

    exec_study_request = ""
    if rt.study is not None:
        exec_study_request = rt.study.request
    if not exec_study_request:
        raise CliRefuse("runtime has no study request", code=4)
    payload = RemoteCoderGenIn(
        study_request=exec_study_request,
        task=draft,
        input_names=tuple(sorted({inp.name for inp in registered_inputs})),
    )
    try:
        spec, usage = await adapter.generate_remote_coder_task(
            payload, prompt_ref=ROLE_REMOTE_CODER_REF, budget=role_budget
        )
    except AgentError as exc:
        await writer_journal.drain(strict=False)
        raise CliRefuse(
            f"remote coder generation for task {draft.task_key!r} failed: {exc.detail}",
            code=4,
        ) from exc
    except LlmError as exc:
        await writer_journal.drain(strict=False)
        raise CliRefuse(
            f"remote coder generation for task {draft.task_key!r} failed: llm error: {exc}",
            code=4,
        ) from exc
    usages.append(
        {
            "role": "remote-coder-gen",
            "task_key": draft.task_key,
            "prompt_tokens": usage.prompt_tokens,
            "completion_tokens": usage.completion_tokens,
            "latency_ms": usage.latency_ms,
        }
    )
    _validate_generated_coverage(draft, spec)
    try:
        remote_request = RemoteCoderTaskRequest(
            study_id=study_id,
            attempt_id=aid,
            call_id=f"{aid}:coder",
            task=spec.task,
            inputs=(),
            outputs=tuple(
                RemoteOutput(path=o.path, media_type=o.media_type, sha256=None, size=None)
                for o in spec.outputs
            ),
            deadline=deadline,
            wait_in_queue=True,
        )
    except RemoteCoderError as exc:
        raise CliRefuse(
            f"remote coder generation for task {draft.task_key!r} invalid: {exc.reason}",
            code=3,
        ) from exc
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
            remote_request,
            backend="remote-openhands",
            jobs=jobs,
            writer=rt.writer,
            artifacts=rt.executor.artifacts,
            ctx=ctx,
            remote_runtime=runtime,
            remote_output_names={o.path: o.name for o in spec.outputs},
            attempt_is_closed=_attempt_is_closed,
        )
    except ToolError as exc:
        await writer_journal.drain(strict=False)
        raise CliRefuse(
            f"remote coder invoke for task {draft.task_key!r} refused: {exc.detail}",
            code=4,
        ) from exc
    except BaseException:  # audit-ok[broad_except]: best-effort drain before re-raise, exception propagates
        await writer_journal.drain(strict=False)
        raise
    await _finish_coder_dispatch(rt, draft=draft, aid=aid, outcome=outcome, tool_out=tool_out,
                                 usages=usages, writer_journal=writer_journal)
