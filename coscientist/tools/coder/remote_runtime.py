"""Remote coder runtime: JobStore-authorized execution over the slice-1 primitives.

JobStore remains the lifecycle authority (the job exists and is running before
any reservation or POST). ``RemoteSubmissionStore`` owns submission identity
(reserve before POST; same identity returns the bound task; a reserved-but-
unbound identity is indeterminate and never re-POSTs). ``RemoteWire`` bounds
every HTTP exchange. A terminal remote claim is execution evidence only — it
never establishes scientific validity.

Receipt policy: submit / status / stop protocol responses are stored as
bounded receipts. Download bodies are the actual output files — they live in
the study CAS (``put_sandbox``, producer=sandbox, provenance="tool") and are
not duplicated into the receipt budget.

Every failure path completes the JobStore job (``failed``) and returns a typed
``CoderOutcome`` — the job is never left live, and a transport-uncertain
exchange is a retryable FAIL, never a success
(docs/remote-coder-runtime.md §16).
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Protocol
from urllib.parse import quote

from pydantic import ValidationError

from coscientist.core.events.clock import Clock
from coscientist.core.events.digest import canonical_json, sha256_hex
from coscientist.core.events.job_events import job_is_live
from coscientist.core.execution.artifacts import LocalArtifactStore, make_tool_ref

# Typing-only protocol edge (same shape as slice-1 remote_wire): the concrete
# HttpTransport is injected; the CLI composes it via mcp_live, never here.
from coscientist.mcp.transport import HttpTransport
from coscientist.tools.coder.remote_contract import (
    RemoteCoderError,
    RemoteCoderTaskRequest,
    SubmitIdentity,
    TaskIdentity,
    digest,
    endpoint,
)
from coscientist.tools.coder.remote_store import RemoteSubmissionStore
from coscientist.tools.coder.remote_wire import RemoteLimits, RemoteWire, parse_object
from coscientist.tools.coder.run import CoderOutcome
from coscientist.tools.jobs import InMemoryJobStore

# Legacy terminal set (openhands_sandbox TERMINAL_STATUSES): completed/cooldown
# claim a finished run (files downloadable); error/cancelled are definitive
# failures. queued/running/soft_stopping keep polling.
_TERMINAL_STATUSES = frozenset({"completed", "cooldown", "error", "cancelled"})
_CLAIM_COMPLETE_STATUSES = frozenset({"completed", "cooldown"})
# Bounded cleanup window for the best-effort stop (the job deadline has already
# elapsed when the stop is issued).
_STOP_GRACE_S = 10.0
# Error reasons whose exchange outcome is unknown — the honest retryable class.
_RETRYABLE_REASONS = frozenset({"remote_transport_uncertain"})
# Definitive HTTP 5xx answers from the service: the server said "not now", not
# "your task failed" — retryable; 4xx is a protocol/task-level refusal.
_RETRYABLE_HTTP = re.compile(r"^remote_(submit|status|download)_http_5\d\d$")


def remote_reason_retryable(reason: str) -> bool:
    """True for transport-uncertain and 5xx service answers (pure; tested)."""
    return reason in _RETRYABLE_REASONS or _RETRYABLE_HTTP.match(reason) is not None


def output_names_for(request: RemoteCoderTaskRequest, names: Mapping[str, str]) -> dict[str, str]:
    """Validate the path → expected-output-name mapping (pure; tested).

    Keys must be exactly the request's output paths; values non-empty bare
    strings, unique (two expected outputs cannot claim one file).
    """
    paths = {output.path for output in request.outputs}
    if set(names) != paths or len(names) != len(set(names.values())):
        raise RemoteCoderError("remote_output_names_invalid")
    for name in names.values():
        if not isinstance(name, str) or not name or name.strip() != name:
            raise RemoteCoderError("remote_output_names_invalid")
    return dict(names)


class RemoteCoderPolicy(Protocol):
    """Injected submit gate: runs before the reservation and before the POST."""

    def check(self, identity: Mapping[str, object]) -> None: ...


@dataclass(frozen=True)
class RemoteCoderSettings:
    """Explicit remote backend config. No endpoint, header or limit is invented."""

    base_url: str
    headers: Mapping[str, str]
    limits: RemoteLimits
    max_receipts: int
    max_receipt_bytes: int
    poll_interval_s: float
    http_factory: Callable[[], HttpTransport]

    def __post_init__(self) -> None:
        endpoint(self.base_url)
        if (
            type(self.poll_interval_s) is bool
            or not isinstance(self.poll_interval_s, (int, float))
            or not 0.0 < float(self.poll_interval_s) <= 60.0
        ):
            raise RemoteCoderError("remote_poll_interval_invalid")

    @property
    def policy_digest(self) -> str:
        """Binds auth configuration without storing secret values (slice-1 §35)."""
        header_fingerprints = sorted(
            f"{name}:{sha256_hex(value.encode('utf-8'))}" for name, value in self.headers.items()
        )
        return sha256_hex(canonical_json([endpoint(self.base_url), header_fingerprints]))

    def open_store(self, root: Path) -> RemoteSubmissionStore:
        return RemoteSubmissionStore(
            root,
            max_receipts=self.max_receipts,
            max_receipt_bytes=self.max_receipt_bytes,
        )

    def open_wire(self, *, clock: Clock) -> RemoteWire:
        return RemoteWire(
            base_url=self.base_url,
            http=self.http_factory(),
            clock=clock,
            headers=dict(self.headers),
            limits=self.limits,
        )


@dataclass(frozen=True)
class RemoteCoderRuntime:
    """Composed runtime: wire + store + submit policy + poll policy. No other I/O."""

    wire: RemoteWire
    store: RemoteSubmissionStore
    policy: RemoteCoderPolicy
    policy_digest: str
    poll_interval_s: float

    def __post_init__(self) -> None:
        digest(self.policy_digest)
        try:
            check_fn = self.policy.check
        except AttributeError as exc:
            # slice-1 §24/25: no (or unusable) policy means typed refusal with
            # zero task requests — fail closed at composition, not at submit.
            raise RemoteCoderError("remote_policy_invalid") from exc
        if not callable(check_fn):
            raise RemoteCoderError("remote_policy_invalid")
        if (
            type(self.poll_interval_s) is bool
            or not isinstance(self.poll_interval_s, (int, float))
            or not 0.0 < float(self.poll_interval_s) <= 60.0
        ):
            raise RemoteCoderError("remote_poll_interval_invalid")

    async def execute(
        self,
        request: RemoteCoderTaskRequest,
        *,
        jobs: InMemoryJobStore,
        artifacts: LocalArtifactStore,
        attempt_is_closed: Callable[[], bool],
        output_names: Mapping[str, str],
    ) -> CoderOutcome:
        """Run one remote coder task under JobStore authority; typed outcome."""
        names = output_names_for(request, output_names)
        identity = request.identity(self.wire.base_url, self.policy_digest)
        spec = {
            "call_id": request.call_id,
            "endpoint": identity["endpoint"],
            "task": request.task,
            "inputs": identity["inputs"],
            "outputs": identity["outputs"],
            "wait": request.wait_in_queue,
        }
        jid = jobs.submit("coder", spec, request.attempt_id, request.deadline)
        jobs.mark_running(jid)

        scope = self.store.scope(request.study_id, request.attempt_id, request.call_id)
        fail_reason: str | None
        fail_reason = None
        task_id = ""
        try:
            self.policy.check(identity)
            binding, created = self.store.reserve(scope, identity)
            if binding.task_id is not None:
                task_id = binding.task_id
            elif not created:
                # Reserved before, never bound: the POST outcome is unknown.
                # Never automatically POST again (slice-1 §32).
                fail_reason = str("remote_submission_indeterminate")
            else:
                task_id = await self._submit(request, scope, binding.request_digest)
        except RemoteCoderError as exc:
            # The typed failure IS the result: the job completes failed, reason carried.
            fail_reason = str(exc.reason)
        if fail_reason is not None:
            return self._fail(jobs, jid, request, fail_reason)

        try:
            outcome = await self._drive(
                request, jid=jid, task_id=task_id, scope=scope, jobs=jobs,
                artifacts=artifacts, attempt_is_closed=attempt_is_closed, names=names,
            )
        except RemoteCoderError as exc:
            # Same contract: the job never stays live; the reason names the exchange.
            fail_reason = str(exc.reason)
        else:
            return outcome
        return self._fail(jobs, jid, request, fail_reason)

    # --- protocol operations (single bounded exchange each) ---

    async def _submit(self, request: RemoteCoderTaskRequest, scope: str, request_digest: str) -> str:
        """POST /api/v1/run; bind only the actual server task id."""
        receipt = await self.wire.exchange(
            "POST",
            "run",
            body=request.wire(),
            deadline=request.deadline,
            operation="submit",
            max_bytes=self.wire.limits.response_bytes,
        )
        self.store.receipt(scope, receipt)
        if receipt.status_code != 200:
            raise RemoteCoderError(f"remote_submit_http_{receipt.status_code}")
        payload = parse_object(receipt.body, max_depth=self.wire.limits.json_depth)
        try:
            submitted = SubmitIdentity.model_validate(payload)
        except ValidationError as exc:
            # A 200 answer outside the identity schema is a protocol refusal,
            # never an inferred success (slice-1 §16) — and never an uncaught
            # error that would leave the job live.
            raise RemoteCoderError("remote_submit_envelope_invalid") from exc
        self.store.bind(scope, request_digest, submitted.task_id)
        return submitted.task_id

    async def _status_once(self, task_id: str, scope: str, deadline: datetime) -> tuple[TaskIdentity, bool]:
        """One bounded status exchange → (identity, execution_error)."""
        receipt = await self.wire.exchange(
            "GET",
            f"status?task_id={quote(task_id, safe='')}",
            body=None,
            deadline=deadline,
            operation="status",
            max_bytes=self.wire.limits.response_bytes,
        )
        self.store.receipt(scope, receipt)
        if receipt.status_code != 200:
            raise RemoteCoderError(f"remote_status_http_{receipt.status_code}")
        payload = parse_object(receipt.body, max_depth=self.wire.limits.json_depth)
        entry = payload.get("current_task")
        if not isinstance(entry, dict):
            raise RemoteCoderError("remote_status_envelope_invalid")
        # Legacy servers carry identity at envelope level (metrics parity): fold
        # the top-level task_id in when the entry omits it — never invent one.
        folded = dict(entry)
        if "task_id" not in folded and isinstance(payload.get("task_id"), str):
            folded["task_id"] = payload["task_id"]
        try:
            identity = TaskIdentity.model_validate(folded)
        except ValidationError as exc:
            # Unknown status values (legacy 'failed'/'stopping') or missing
            # identity are typed protocol errors (slice-1 §16).
            raise RemoteCoderError("remote_status_envelope_invalid") from exc
        if identity.task_id != task_id:
            raise RemoteCoderError("remote_status_identity_mismatch")
        turn_error = entry.get("turn_error")
        execution_error = bool(entry.get("error")) or (isinstance(turn_error, dict) and bool(turn_error))
        return identity, execution_error

    async def _download(
        self,
        task_id: str,
        *,
        path: str,
        deadline: datetime,
        expected_sha256: str | None,
        expected_size: int | None,
    ) -> bytes:
        receipt = await self.wire.exchange(
            "GET",
            f"files/download?task_id={quote(task_id, safe='')}&path={quote(path, safe='')}",
            body=None,
            deadline=deadline,
            operation="download",
            max_bytes=self.wire.limits.download_bytes,
        )
        if receipt.status_code != 200:
            raise RemoteCoderError(f"remote_download_http_{receipt.status_code}")
        if expected_size is not None and len(receipt.body) != expected_size:
            raise RemoteCoderError("remote_download_size_mismatch")
        actual = sha256_hex(receipt.body)
        if expected_sha256 is not None and actual != expected_sha256:
            raise RemoteCoderError("remote_download_digest_mismatch")
        return receipt.body

    async def _stop(self, task_id: str, scope: str) -> None:
        """Best-effort stop. The ack is not termination proof (slice-1 §13/§45);
        failures never delete the binding and never mask the outcome. The stop
        gets its own bounded grace window: the job deadline has already elapsed
        when a stop is issued (cleanup budget, like the sandbox reap window)."""
        stop_deadline = self.wire.clock.now() + timedelta(seconds=_STOP_GRACE_S)
        try:
            receipt = await self.wire.exchange(
                "POST",
                f"stop?task_id={quote(task_id, safe='')}",
                body=None,
                deadline=stop_deadline,
                operation="stop",
                max_bytes=self.wire.limits.response_bytes,
            )
            # Storing the receipt is part of the best-effort path: a receipt
            # budget/store failure must not mask the outcome (and must never
            # hit a job that is already terminal through _fail → complete).
            self.store.receipt(scope, receipt)
        except RemoteCoderError as exc:
            # Best-effort cleanup: observe the typed reason then fall through —
            # never mask the already-decided timed_out/failed outcome.
            type(exc.reason)

    # --- drive phases ---

    async def _drive(
        self,
        request: RemoteCoderTaskRequest,
        *,
        jid: str,
        task_id: str,
        scope: str,
        jobs: InMemoryJobStore,
        artifacts: LocalArtifactStore,
        attempt_is_closed: Callable[[], bool],
        names: Mapping[str, str],
    ) -> CoderOutcome:
        wait_task = asyncio.create_task(jobs.wait(jid, until=request.deadline))
        poll_task = asyncio.create_task(self._poll_terminal(task_id, scope, request.deadline))
        done, pending = await asyncio.wait({wait_task, poll_task}, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

        if wait_task in done and not wait_task.cancelled():
            job = wait_task.result()
            if job.status == "timed_out":
                return await self._timed_out(jobs, jid, task_id, scope, request)
            if job.status == "cancelled":
                # An operator cancellation is NOT a retryable timeout: report it
                # honestly so a retry cannot override a deliberate stop. The job
                # is already terminal (cancelled) — keep that status.
                return self._fail(
                    jobs, jid, request, "remote_job_cancelled", job_may_be_terminal=True
                )

        if poll_task not in done or poll_task.cancelled():
            raise RemoteCoderError("remote_poll_cancelled")
        poll_result: tuple[TaskIdentity, bool] | None
        poll_result = None
        poll_budget: str | None
        poll_budget = None
        try:
            poll_result = poll_task.result()
        except RemoteCoderError as exc:
            if exc.reason != "remote_deadline_elapsed":
                raise
            # The poll raced the JobStore deadline: a status exchange refused
            # to start because the budget elapsed. That IS the deadline path.
            poll_budget = str(exc.reason)
        if poll_budget is not None:
            return await self._timed_out(jobs, jid, task_id, scope, request)
        if poll_result is None:
            raise RemoteCoderError("remote_poll_cancelled")
        identity, execution_error = poll_result
        if identity.status not in _CLAIM_COMPLETE_STATUSES:
            return self._fail(jobs, jid, request, f"remote_status_{identity.status}")
        if execution_error:
            # completed/cooldown carrying error/turn_error is a failed turn
            # (legacy _terminal_result parity) — never a success.
            return self._fail(jobs, jid, request, "remote_turn_error")

        collect_fail: str | None
        collect_fail = None
        ids: list[str] = []
        try:
            ids = await self._collect_outputs(
                request, task_id=task_id, artifacts=artifacts,
                attempt_is_closed=attempt_is_closed, names=names,
            )
        except RemoteCoderError as exc:
            collect_fail = str(exc.reason)
        except Exception as exc:  # audit-ok[broad_except]: CAS/ingest → typed fail; job never stays live
            # Registration into the study CAS is part of the run: a rejected or
            # unreadable artifact is an honest FAILED run — the job completes,
            # no partial success escapes, and the reason names the failure class.
            # Broad on purpose: KeyError/ValueError/OSError/ArtifactRejected/…
            # must all terminalize the job (docs/remote-coder-runtime.md §16).
            collect_fail = str(f"remote_artifact_register_failed:{type(exc).__name__}")
        if collect_fail is not None:
            if collect_fail == "remote_deadline_elapsed":
                # The budget elapsed between the terminal claim and the downloads:
                # the same honest timed_out budget outcome, never a plain failure.
                return await self._timed_out(jobs, jid, task_id, scope, request)
            return self._fail(jobs, jid, request, collect_fail)
        jobs.complete(jid, status="succeeded", artifacts=ids)
        return CoderOutcome(
            job_id=jid,
            attempt_id=request.attempt_id,
            status="succeeded",
            retryable=False,
            artifact_ids=ids,
            detail=f"remote_completed:{task_id}",
        )

    async def _timed_out(
        self,
        jobs: InMemoryJobStore,
        jid: str,
        task_id: str,
        scope: str,
        request: RemoteCoderTaskRequest,
    ) -> CoderOutcome:
        """The single deadline path: apply the store's own timeout (lifecycle
        authority — the racing wait task may have been cancelled mid-race),
        stop best-effort, and return the honest retryable budget outcome."""
        await jobs.wait(jid, until=request.deadline)
        await self._stop(task_id, scope)
        return CoderOutcome(
            job_id=jid,
            attempt_id=request.attempt_id,
            status="timed_out",
            retryable=True,
            artifact_ids=[],
            detail="remote_job_deadline (stop ack is not termination proof)",
        )

    async def _collect_outputs(
        self,
        request: RemoteCoderTaskRequest,
        *,
        task_id: str,
        artifacts: LocalArtifactStore,
        attempt_is_closed: Callable[[], bool],
        names: Mapping[str, str],
    ) -> list[str]:
        ids: list[str] = []
        for output in request.outputs:
            data = await self._download(
                task_id,
                path=output.path,
                deadline=request.deadline,
                expected_sha256=output.sha256,
                expected_size=output.size,
            )
            art = artifacts.put_sandbox(
                data,
                make_tool_ref(request.call_id, tool="coder"),
                name=names[output.path],
                media_type=output.media_type,
                attempt_closed=attempt_is_closed(),
            )
            if not art.sha256 or len(art.sha256) != 64:
                raise RemoteCoderError("remote_artifact_digest_invalid")
            ids.append(art.artifact_id)
        return ids

    async def _poll_terminal(self, task_id: str, scope: str, deadline: datetime) -> tuple[TaskIdentity, bool]:
        while True:
            identity, execution_error = await self._status_once(task_id, scope, deadline)
            if identity.status in _TERMINAL_STATUSES:
                return identity, execution_error
            await asyncio.sleep(self.poll_interval_s)

    def _fail(
        self,
        jobs: InMemoryJobStore,
        jid: str,
        request: RemoteCoderTaskRequest,
        reason: str,
        *,
        job_may_be_terminal: bool = False,
    ) -> CoderOutcome:
        """Complete the job failed and return the typed honest outcome.

        ``job_may_be_terminal=True`` (operator cancellation): the store may
        already carry a terminal status — keep it instead of failing the
        transition; the typed outcome still reports the cancellation."""
        if not job_may_be_terminal or job_is_live(jobs.get(jid).status):
            jobs.complete(jid, status="failed", artifacts=[])
        return CoderOutcome(
            job_id=jid,
            attempt_id=request.attempt_id,
            status="failed",
            retryable=remote_reason_retryable(reason),
            artifact_ids=[],
            detail=reason,
        )
