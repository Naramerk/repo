"""Remote coder runtime (slice-2): JobStore-authorized execute with fake transport.

The design forbids any real submit in tests (docs/remote-coder-runtime.md §22);
every scenario runs against a scriptable in-process transport.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from coscientist.core.events.clock import Clock, FakeClock, SystemClock
from coscientist.core.events.events import UnstampedEvent
from coscientist.core.events.job_events import JobKind
from coscientist.core.execution.artifacts import LocalArtifactStore
from coscientist.mcp.errors import McpError
from coscientist.tools.coder.remote_contract import (
    HttpReceipt,
    RemoteCoderError,
    RemoteCoderTaskRequest,
    RemoteOutput,
    digest,
)
from coscientist.tools.coder.remote_runtime import (
    RemoteCoderPolicy,
    RemoteCoderRuntime,
    RemoteCoderSettings,
    output_names_for,
    remote_reason_retryable,
)
from coscientist.tools.coder.remote_store import RemoteSubmissionStore
from coscientist.tools.coder.remote_wire import RemoteLimits
from coscientist.tools.jobs import InMemoryJobStore

DEADLINE = datetime.now(UTC) + timedelta(seconds=60)
LIMITS = RemoteLimits(
    request_bytes=4096,
    response_bytes=4096,
    download_bytes=4096,
    read_chunk_bytes=1024,
    json_depth=16,
    timeout_s=5.0,
    close_timeout_s=2.0,
)


def _request(**overrides: Any) -> RemoteCoderTaskRequest:
    fields: dict[str, Any] = {
        "study_id": "st_" + "a" * 27,
        "attempt_id": "st_" + "a" * 27 + "/EXP-0#1",
        "call_id": "st_" + "a" * 27 + "/EXP-0#1:coder",
        "task": "напиши и запусти скрипт, который пишет outputs/result.json",
        "inputs": (),
        "outputs": (
            RemoteOutput(path="outputs/result.json", media_type="application/json", sha256=None, size=None),
        ),
        "deadline": DEADLINE,
        "wait_in_queue": True,
    }
    fields.update(overrides)
    return RemoteCoderTaskRequest(**fields)


class _Stream:
    def __init__(self, *, status_code: int, body: bytes) -> None:
        self.status_code = status_code
        self.headers: dict[str, str] = {}
        self._body = body

    async def read(self, n: int) -> bytes:
        chunk, self._body = self._body[:n], self._body[n:]
        return chunk

    async def aclose(self) -> None:
        return None


class _FakeTransport:
    """Scriptable transport: (method, path-prefix) → (status, body) or exception."""

    def __init__(self, *, download_delay_s: float = 0.0) -> None:
        self.opened: list[tuple[str, str]] = []
        self.script: dict[str, tuple[int, bytes] | Exception] = {}
        self._download_delay_s = download_delay_s

    def script_default(self, *, result_body: bytes = b'{"result":"ok"}') -> None:
        self.script.update(
            {
                "POST:/api/v1/run": (200, b'{"task_id":"t-1"}'),
                "GET:/api/v1/status": (200, b'{"current_task":{"task_id":"t-1","status":"completed"}}'),
                "GET:/api/v1/files/download": (200, result_body),
            }
        )

    async def open(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        body: bytes | None,
        timeout_s: float,
        connect_ip: str | None,
        server_hostname: str | None = None,
    ) -> _Stream:
        self.opened.append((method, url))
        for prefix, answer in self.script.items():
            want_method, want_path = prefix.split(":", 1)
            if method == want_method and want_path in url:
                if isinstance(answer, Exception):
                    raise answer
                if want_path == "/api/v1/files/download" and self._download_delay_s > 0:
                    await asyncio.sleep(self._download_delay_s)
                status, payload = answer
                return _Stream(status_code=status, body=payload)
        raise AssertionError(f"unexpected exchange: {method} {url}")


class _AllowPolicy:
    def __init__(self) -> None:
        self.seen: list[dict[str, object]] = []

    def check(self, identity: dict[str, object]) -> None:
        self.seen.append(dict(identity))


class _Journal:
    def __init__(self) -> None:
        self.events: list[UnstampedEvent] = []

    def record(self, event: UnstampedEvent) -> None:
        self.events.append(event)


def _runtime(
    tmp_path: Path,
    transport: _FakeTransport,
    *,
    policy: RemoteCoderPolicy | None = None,
    poll_interval_s: float = 0.01,
    clock: Clock | None = None,
) -> RemoteCoderRuntime:
    wire = _WireHost(transport, clock)
    return RemoteCoderRuntime(
        wire=wire.wire,
        store=RemoteSubmissionStore(tmp_path, max_receipts=64, max_receipt_bytes=65536),
        policy=policy if policy is not None else _AllowPolicy(),
        policy_digest="ab" * 32,
        poll_interval_s=poll_interval_s,
    )


class _WireHost:
    def __init__(self, transport: _FakeTransport, clock: Clock | None) -> None:
        from coscientist.tools.coder.remote_wire import RemoteWire

        self.wire = RemoteWire(
            base_url="http://10.32.2.2:8884",
            http=transport,
            clock=clock if clock is not None else FakeClock(),
            headers={},
            limits=LIMITS,
        )


def _jobs(study_id: str, journal: _Journal, clock: Clock) -> InMemoryJobStore:
    return InMemoryJobStore(study_id=study_id, clock=clock, journal=journal, on_clock_change=None)


def _default_names() -> dict[str, str]:
    return {"outputs/result.json": "coder"}


class TestHappyPath:
    @pytest.mark.asyncio
    async def test_submit_poll_download_registers_cas_artifacts(self, tmp_path: Path) -> None:
        transport = _FakeTransport()
        transport.script_default(result_body=b'{"answer": 42}')
        runtime = _runtime(tmp_path, transport)
        journal = _Journal()
        jobs = _jobs("st_" + "a" * 27, journal, FakeClock())
        artifacts = LocalArtifactStore(tmp_path / "arts")
        request = _request()
        outcome = await runtime.execute(
            request,
            jobs=jobs,
            artifacts=artifacts,
            attempt_is_closed=lambda: False,
            output_names=_default_names(),
        )
        assert outcome.status == "succeeded" and outcome.retryable is False
        assert outcome.detail == "remote_completed:t-1"
        assert [m for m, _ in transport.opened] == ["POST", "GET", "GET"]
        assert "/api/v1/run" in transport.opened[0][1]
        assert "task_id=t-1" in transport.opened[1][1]
        assert "path=outputs%2Fresult.json" in transport.opened[2][1]
        job = jobs.get(outcome.job_id)
        assert job.status == "succeeded" and job.artifacts == outcome.artifact_ids
        meta = artifacts.get_meta(outcome.artifact_ids[0])
        assert meta is not None
        assert meta.name == "coder"  # expected-output name, not the sandbox path
        assert meta.producer == "sandbox" and meta.provenance == "tool"
        assert artifacts.read(outcome.artifact_ids[0]) == b'{"answer": 42}'
        # receipts: submit + status (download bytes live in the CAS only)
        scope = runtime.store.scope(request.study_id, request.attempt_id, request.call_id)
        operations = {r.operation for r in runtime.store.receipts(scope)}
        assert operations == {"submit", "status"}
        binding = runtime.store.get(scope)
        assert binding.task_id == "t-1"

    @pytest.mark.asyncio
    async def test_job_running_before_any_post(self, tmp_path: Path) -> None:
        """JobStore reserve happens before the reservation and the POST."""
        transport = _FakeTransport()
        transport.script_default()
        runtime = _runtime(tmp_path, transport)
        journal = _Journal()
        jobs = _jobs("st_" + "a" * 27, journal, FakeClock())
        artifacts = LocalArtifactStore(tmp_path / "arts")
        await runtime.execute(
            _request(),
            jobs=jobs,
            artifacts=artifacts,
            attempt_is_closed=lambda: False,
            output_names=_default_names(),
        )
        submitted = [e for e in journal.events if e.type == "JobSubmitted"]
        assert len(submitted) == 1


class TestFailureSemantics:
    @pytest.mark.asyncio
    async def test_status_error_is_failed_non_retryable(self, tmp_path: Path) -> None:
        transport = _FakeTransport()
        transport.script_default()
        transport.script["GET:/api/v1/status"] = (
            200,
            b'{"current_task":{"task_id":"t-1","status":"error","error":"boom"}}',
        )
        runtime = _runtime(tmp_path, transport)
        jobs = _jobs("st_" + "a" * 27, _Journal(), FakeClock())
        outcome = await runtime.execute(
            _request(), jobs=jobs, artifacts=LocalArtifactStore(tmp_path / "a"),
            attempt_is_closed=lambda: False, output_names=_default_names(),
        )
        assert outcome.status == "failed" and outcome.retryable is False
        assert outcome.detail == "remote_status_error"
        assert jobs.get(outcome.job_id).status == "failed"

    @pytest.mark.asyncio
    async def test_completed_with_turn_error_is_failed(self, tmp_path: Path) -> None:
        transport = _FakeTransport()
        transport.script_default()
        transport.script["GET:/api/v1/status"] = (
            200,
            b'{"current_task":{"task_id":"t-1","status":"completed",'
            b'"turn_error":{"kind":"execution","detail":"agent broke"}}}',
        )
        runtime = _runtime(tmp_path, transport)
        jobs = _jobs("st_" + "a" * 27, _Journal(), FakeClock())
        outcome = await runtime.execute(
            _request(), jobs=jobs, artifacts=LocalArtifactStore(tmp_path / "a"),
            attempt_is_closed=lambda: False, output_names=_default_names(),
        )
        assert outcome.status == "failed" and outcome.retryable is False
        assert outcome.detail == "remote_turn_error"
        assert transport.opened[-1][0] == "GET"  # no download after a failed turn

    @pytest.mark.asyncio
    async def test_transport_uncertain_is_retryable_fail(self, tmp_path: Path) -> None:
        transport = _FakeTransport()
        transport.script["POST:/api/v1/run"] = McpError("connection reset")
        runtime = _runtime(tmp_path, transport)
        jobs = _jobs("st_" + "a" * 27, _Journal(), FakeClock())
        outcome = await runtime.execute(
            _request(), jobs=jobs, artifacts=LocalArtifactStore(tmp_path / "a"),
            attempt_is_closed=lambda: False, output_names=_default_names(),
        )
        assert outcome.status == "failed" and outcome.retryable is True
        assert outcome.detail == "remote_transport_uncertain"
        assert jobs.get(outcome.job_id).status == "failed"

    @pytest.mark.asyncio
    async def test_status_http_5xx_retryable_4xx_not(self, tmp_path: Path) -> None:
        transport = _FakeTransport()
        transport.script_default()
        transport.script["GET:/api/v1/status"] = (503, b"overloaded")
        runtime = _runtime(tmp_path, transport)
        jobs = _jobs("st_" + "a" * 27, _Journal(), FakeClock())
        outcome = await runtime.execute(
            _request(), jobs=jobs, artifacts=LocalArtifactStore(tmp_path / "a"),
            attempt_is_closed=lambda: False, output_names=_default_names(),
        )
        assert outcome.retryable is True and outcome.detail == "remote_status_http_503"

        transport2 = _FakeTransport()
        transport2.script_default()
        transport2.script["GET:/api/v1/status"] = (404, b"gone")
        runtime2 = _runtime(tmp_path / "b", transport2)
        jobs2 = _jobs("st_" + "a" * 27, _Journal(), FakeClock())
        outcome2 = await runtime2.execute(
            _request(), jobs=jobs2, artifacts=LocalArtifactStore(tmp_path / "b" / "a"),
            attempt_is_closed=lambda: False, output_names=_default_names(),
        )
        assert outcome2.retryable is False and outcome2.detail == "remote_status_http_404"

    @pytest.mark.asyncio
    async def test_download_digest_mismatch_is_failed(self, tmp_path: Path) -> None:
        transport = _FakeTransport()
        transport.script_default(result_body=b'{"answer": 42}')
        runtime = _runtime(tmp_path, transport)
        jobs = _jobs("st_" + "a" * 27, _Journal(), FakeClock())
        wrong = digest("cd" * 32)
        request = _request(
            outputs=(RemoteOutput(path="outputs/result.json", media_type="application/json", sha256=wrong, size=None),)
        )
        outcome = await runtime.execute(
            request, jobs=jobs, artifacts=LocalArtifactStore(tmp_path / "a"),
            attempt_is_closed=lambda: False, output_names={"outputs/result.json": "coder"},
        )
        assert outcome.status == "failed" and outcome.retryable is False
        assert outcome.detail == "remote_download_digest_mismatch"

    @pytest.mark.asyncio
    async def test_attempt_closed_is_failed_run_not_escape(self, tmp_path: Path) -> None:
        """A CAS/ingest refusal (attempt closed) completes the job FAILED —
        no exception escapes ``execute`` and the job never stays live."""
        transport = _FakeTransport()
        transport.script_default()
        runtime = _runtime(tmp_path, transport)
        jobs = _jobs("st_" + "a" * 27, _Journal(), FakeClock())
        outcome = await runtime.execute(
            _request(), jobs=jobs, artifacts=LocalArtifactStore(tmp_path / "a"),
            attempt_is_closed=lambda: True, output_names=_default_names(),
        )
        assert outcome.status == "failed" and outcome.retryable is False
        assert outcome.detail == "remote_artifact_register_failed:ArtifactRejected"
        assert jobs.get(outcome.job_id).status == "failed"
        # the download happened, but nothing was registered as success
        assert [m for m, _ in transport.opened] == ["POST", "GET", "GET"]


class TestDownloadUnavailableSkip:
    """Download HTTP 4xx: deterministic sandbox refusal for one file — typed
    skip, never a run failure and never a retry (issue #332: the same 400
    cannot be fixed by re-generating the code)."""

    @pytest.mark.asyncio
    async def test_download_http_400_is_typed_skip_not_failure(self, tmp_path: Path) -> None:
        transport = _FakeTransport()
        transport.script_default()
        transport.script["GET:/api/v1/files/download"] = (400, b'{"error":"bad path"}')
        runtime = _runtime(tmp_path, transport)
        journal = _Journal()
        jobs = _jobs("st_" + "a" * 27, journal, FakeClock())
        outcome = await runtime.execute(
            _request(), jobs=jobs, artifacts=LocalArtifactStore(tmp_path / "a"),
            attempt_is_closed=lambda: False, output_names=_default_names(),
        )
        # The attempt does NOT fail and does NOT re-arm a retry: the job
        # completed executed; the unmaterializable output is typed.
        assert outcome.status == "succeeded" and outcome.retryable is False
        assert outcome.artifact_ids == []
        assert outcome.outputs_unavailable == {
            "outputs/result.json": "remote_download_http_400"
        }
        assert outcome.detail == (
            "remote_completed:t-1;outputs_unavailable:outputs/result.json=remote_download_http_400"
        )
        assert jobs.get(outcome.job_id).status == "succeeded"
        # Exactly one submit → no retry loop regenerated the task.
        assert [m for m, _ in transport.opened] == ["POST", "GET", "GET"]

    @pytest.mark.asyncio
    async def test_download_http_400_skips_only_that_file(self, tmp_path: Path) -> None:
        transport = _FakeTransport()
        transport.script = {
            "POST:/api/v1/run": (200, b'{"task_id":"t-1"}'),
            "GET:/api/v1/status": (200, b'{"current_task":{"task_id":"t-1","status":"completed"}}'),
            # Path-specific 400 must win over the generic download answer.
            "GET:path=outputs%2Fa.json": (400, b'{"error":"bad path"}'),
            "GET:/api/v1/files/download": (200, b'{"b": 1}'),
        }
        runtime = _runtime(tmp_path, transport)
        jobs = _jobs("st_" + "a" * 27, _Journal(), FakeClock())
        request = _request(
            outputs=(
                RemoteOutput(path="outputs/a.json", media_type="application/json", sha256=None, size=None),
                RemoteOutput(path="outputs/b.json", media_type="application/json", sha256=None, size=None),
            )
        )
        outcome = await runtime.execute(
            request, jobs=jobs, artifacts=LocalArtifactStore(tmp_path / "a"),
            attempt_is_closed=lambda: False,
            output_names={"outputs/a.json": "fig_a", "outputs/b.json": "fig_b"},
        )
        assert outcome.status == "succeeded" and outcome.retryable is False
        assert len(outcome.artifact_ids) == 1
        meta = LocalArtifactStore(tmp_path / "a").get_meta(outcome.artifact_ids[0])
        assert meta is not None and meta.name == "fig_b"
        assert outcome.outputs_unavailable == {"outputs/a.json": "remote_download_http_400"}
        # The 400 did not stop the collection: the remaining file was still fetched.
        download_urls = [u for m, u in transport.opened if m == "GET" and "files/download" in u]
        assert len(download_urls) == 2

    @pytest.mark.asyncio
    async def test_download_http_5xx_still_failed_retryable(self, tmp_path: Path) -> None:
        transport = _FakeTransport()
        transport.script_default()
        transport.script["GET:/api/v1/files/download"] = (503, b"overloaded")
        runtime = _runtime(tmp_path, transport)
        jobs = _jobs("st_" + "a" * 27, _Journal(), FakeClock())
        outcome = await runtime.execute(
            _request(), jobs=jobs, artifacts=LocalArtifactStore(tmp_path / "a"),
            attempt_is_closed=lambda: False, output_names=_default_names(),
        )
        assert outcome.status == "failed" and outcome.retryable is True
        assert outcome.detail == "remote_download_http_503"
        assert outcome.outputs_unavailable == {}


class TestIdentityAndRecovery:
    @pytest.mark.asyncio
    async def test_bound_identity_recovers_without_resubmit(self, tmp_path: Path) -> None:
        """Crash between POST and status: same identity + bound task → status only."""
        transport = _FakeTransport()
        transport.script_default()
        runtime = _runtime(tmp_path, transport)
        request = _request()
        scope = runtime.store.scope(request.study_id, request.attempt_id, request.call_id)
        identity = request.identity(runtime.wire.base_url, "ab" * 32)
        binding, created = runtime.store.reserve(scope, identity)
        assert created is True
        runtime.store.bind(scope, binding.request_digest, "t-9")
        transport.script["POST:/api/v1/run"] = AssertionError("must not resubmit")
        transport.script["GET:/api/v1/status"] = (
            200,
            b'{"current_task":{"task_id":"t-9","status":"completed"}}',
        )
        transport.script["GET:/api/v1/files/download"] = (200, b'{"done": true}')
        jobs = _jobs("st_" + "a" * 27, _Journal(), FakeClock())
        outcome = await runtime.execute(
            request, jobs=jobs, artifacts=LocalArtifactStore(tmp_path / "a"),
            attempt_is_closed=lambda: False, output_names=_default_names(),
        )
        assert outcome.status == "succeeded" and outcome.detail == "remote_completed:t-9"
        assert [m for m, _ in transport.opened] == ["GET", "GET"]

    @pytest.mark.asyncio
    async def test_reserved_unbound_identity_is_indeterminate(self, tmp_path: Path) -> None:
        transport = _FakeTransport()
        runtime = _runtime(tmp_path, transport)
        request = _request()
        scope = runtime.store.scope(request.study_id, request.attempt_id, request.call_id)
        runtime.store.reserve(scope, request.identity(runtime.wire.base_url, "ab" * 32))
        jobs = _jobs("st_" + "a" * 27, _Journal(), FakeClock())
        outcome = await runtime.execute(
            request, jobs=jobs, artifacts=LocalArtifactStore(tmp_path / "a"),
            attempt_is_closed=lambda: False, output_names=_default_names(),
        )
        assert outcome.status == "failed" and outcome.retryable is False
        assert outcome.detail == "remote_submission_indeterminate"
        assert transport.opened == []  # never a second automatic POST
        assert jobs.get(outcome.job_id).status == "failed"

    @pytest.mark.asyncio
    async def test_changed_identity_under_same_call_conflicts(self, tmp_path: Path) -> None:
        transport = _FakeTransport()
        runtime = _runtime(tmp_path, transport)
        request = _request()
        scope = runtime.store.scope(request.study_id, request.attempt_id, request.call_id)
        runtime.store.reserve(scope, request.identity(runtime.wire.base_url, "ab" * 32))
        changed = _request(task="другая задача под тем же call_id")
        jobs = _jobs("st_" + "a" * 27, _Journal(), FakeClock())
        outcome = await runtime.execute(
            changed, jobs=jobs, artifacts=LocalArtifactStore(tmp_path / "a"),
            attempt_is_closed=lambda: False, output_names=_default_names(),
        )
        assert outcome.status == "failed" and outcome.retryable is False
        assert outcome.detail == "remote_request_identity_conflict"
        assert transport.opened == []

    @pytest.mark.asyncio
    async def test_policy_refusal_never_posts(self, tmp_path: Path) -> None:
        class _Deny:
            def check(self, identity: dict[str, object]) -> None:
                raise RemoteCoderError("remote_policy_deadline_exceeded")

        transport = _FakeTransport()
        runtime = _runtime(tmp_path, transport, policy=_Deny())
        jobs = _jobs("st_" + "a" * 27, _Journal(), FakeClock())
        outcome = await runtime.execute(
            _request(), jobs=jobs, artifacts=LocalArtifactStore(tmp_path / "a"),
            attempt_is_closed=lambda: False, output_names=_default_names(),
        )
        assert outcome.status == "failed" and outcome.detail == "remote_policy_deadline_exceeded"
        assert transport.opened == []
        scope = runtime.store.scope(_request().study_id, _request().attempt_id, _request().call_id)
        # refusal happens BEFORE the reservation: zero task requests, no binding
        with pytest.raises(RemoteCoderError, match="remote_binding_not_found"):
            runtime.store.get(scope)

    @pytest.mark.asyncio
    async def test_restart_new_store_same_study_reposts_new_scope_only_for_new_call(
        self, tmp_path: Path
    ) -> None:
        """A fresh store (new attempt/call scope) submits again; the old binding
        is never consulted or deleted."""
        first = _FakeTransport()
        first.script_default()
        runtime_a = _runtime(tmp_path / "s1", first)
        request = _request(attempt_id="st_" + "a" * 27 + "/EXP-0#1")
        await runtime_a.execute(
            request, jobs=_jobs("st_" + "a" * 27, _Journal(), FakeClock()),
            artifacts=LocalArtifactStore(tmp_path / "a1"),
            attempt_is_closed=lambda: False, output_names=_default_names(),
        )
        second = _FakeTransport()
        second.script_default()
        runtime_b = _runtime(tmp_path / "s2", second)
        other = _request(attempt_id="st_" + "a" * 27 + "/EXP-0#2")
        outcome = await runtime_b.execute(
            other, jobs=_jobs("st_" + "a" * 27, _Journal(), FakeClock()),
            artifacts=LocalArtifactStore(tmp_path / "a2"),
            attempt_is_closed=lambda: False, output_names=_default_names(),
        )
        assert outcome.status == "succeeded"
        assert any(m == "POST" for m, _ in second.opened)


class TestProtocolRefusals:
    @pytest.mark.asyncio
    async def test_submit_envelope_invalid_is_typed_and_job_fails(self, tmp_path: Path) -> None:
        """A 200 answer outside the identity schema is a typed protocol refusal
        that completes the job — never an uncaught ValidationError."""
        transport = _FakeTransport()
        transport.script_default()
        transport.script["POST:/api/v1/run"] = (200, b'{"status": "submitted"}')
        runtime = _runtime(tmp_path, transport)
        jobs = _jobs("st_" + "a" * 27, _Journal(), FakeClock())
        outcome = await runtime.execute(
            _request(), jobs=jobs, artifacts=LocalArtifactStore(tmp_path / "a"),
            attempt_is_closed=lambda: False, output_names=_default_names(),
        )
        assert outcome.status == "failed" and outcome.retryable is False
        assert outcome.detail == "remote_submit_envelope_invalid"
        assert jobs.get(outcome.job_id).status == "failed"

    @pytest.mark.asyncio
    async def test_legacy_status_outside_literal_is_typed_and_job_fails(self, tmp_path: Any) -> None:
        """Legacy status values ('failed'/'stopping') outside the RemoteStatus
        set are typed protocol refusals, not crashes; the job completes."""
        transport = _FakeTransport()
        transport.script_default()
        transport.script["GET:/api/v1/status"] = (
            200,
            b'{"current_task":{"task_id":"t-1","status":"failed"}}',
        )
        runtime = _runtime(tmp_path, transport)
        jobs = _jobs("st_" + "a" * 27, _Journal(), FakeClock())
        outcome = await runtime.execute(
            _request(), jobs=jobs, artifacts=LocalArtifactStore(tmp_path / "a"),
            attempt_is_closed=lambda: False, output_names=_default_names(),
        )
        assert outcome.status == "failed" and outcome.retryable is False
        assert outcome.detail == "remote_status_envelope_invalid"
        assert jobs.get(outcome.job_id).status == "failed"

    @pytest.mark.asyncio
    async def test_stop_receipt_budget_failure_does_not_mask_timed_out(self, tmp_path: Any) -> None:
        """A receipt-budget failure while storing the stop receipt is part of
        the best-effort stop: the timed_out outcome and the terminal job stand."""
        transport = _FakeTransport()
        transport.script_default()
        transport.script["GET:/api/v1/status"] = (
            200,
            b'{"current_task":{"task_id":"t-1","status":"running"}}',
        )
        transport.script["POST:/api/v1/stop"] = (200, b'{"status":"stopping"}')
        near = datetime.now(UTC) + timedelta(seconds=0.15)
        # max_receipts=2: submit + the (deduplicated) status receipt exhaust
        # the budget; storing the stop receipt would raise
        # remote_receipt_budget_exhausted.
        wire = _WireHost(transport, SystemClock())
        runtime = RemoteCoderRuntime(
            wire=wire.wire,
            store=RemoteSubmissionStore(tmp_path, max_receipts=2, max_receipt_bytes=65536),
            policy=_AllowPolicy(),
            policy_digest="ab" * 32,
            poll_interval_s=0.02,
        )
        jobs = _jobs("st_" + "a" * 27, _Journal(), SystemClock())
        request = _request(deadline=near)
        outcome = await runtime.execute(
            request, jobs=jobs, artifacts=LocalArtifactStore(tmp_path / "a"),
            attempt_is_closed=lambda: False, output_names=_default_names(),
        )
        assert outcome.status == "timed_out" and outcome.retryable is True
        assert jobs.get(outcome.job_id).status == "timed_out"

    @pytest.mark.asyncio
    async def test_download_deadline_elapsed_is_timed_out_budget(self, tmp_path: Path) -> None:
        """The budget elapsing between the terminal claim and the downloads is
        the same honest timed_out outcome — not a plain non-retryable failure."""
        transport = _FakeTransport()
        transport.script_default()
        transport.script["POST:/api/v1/stop"] = (200, b'{"status":"stopping"}')
        near = datetime.now(UTC) + timedelta(seconds=0.15)
        runtime = _runtime(tmp_path, transport, poll_interval_s=0.01, clock=SystemClock())

        class _SlowToStartDownload(runtime.__class__):  # type: ignore[name-defined]
            """Delays BEFORE the download exchange: it then refuses to start
            because the budget elapsed (remote_deadline_elapsed pre-open)."""

            async def _download(
                self,
                task_id: str,
                *,
                path: str,
                deadline: datetime,
                expected_sha256: str | None,
                expected_size: int | None,
            ) -> bytes:
                await asyncio.sleep(0.3)
                return await super()._download(
                    task_id,
                    path=path,
                    deadline=deadline,
                    expected_sha256=expected_sha256,
                    expected_size=expected_size,
                )

        jobs = _jobs("st_" + "a" * 27, _Journal(), SystemClock())
        request = _request(deadline=near)
        outcome = await _SlowToStartDownload(
            wire=runtime.wire,
            store=runtime.store,
            policy=runtime.policy,
            policy_digest=runtime.policy_digest,
            poll_interval_s=runtime.poll_interval_s,
        ).execute(
            request, jobs=jobs, artifacts=LocalArtifactStore(tmp_path / "a"),
            attempt_is_closed=lambda: False, output_names=_default_names(),
        )
        assert outcome.status == "timed_out" and outcome.retryable is True
        assert "stop ack is not termination proof" in outcome.detail
        assert jobs.get(outcome.job_id).status == "timed_out"

    @pytest.mark.asyncio
    async def test_operator_cancelled_is_failed_not_retryable_timeout(self, tmp_path: Path) -> None:
        """An operator cancellation reports honestly as failed/non-retryable —
        it must never look like a retryable timeout that a retry would override."""
        transport = _FakeTransport()
        transport.script_default()
        transport.script["GET:/api/v1/status"] = (
            200,
            b'{"current_task":{"task_id":"t-1","status":"running"}}',
        )
        transport.script["POST:/api/v1/stop"] = (200, b'{"status":"stopping"}')
        runtime = _runtime(tmp_path, transport, poll_interval_s=0.02)

        class _CancelAfterStart:
            """Cancels the job right after the runtime starts it."""

            def __init__(self, inner: InMemoryJobStore) -> None:
                self._inner = inner

            def submit(
                self,
                kind: JobKind,
                spec: dict[str, Any],
                attempt_id: str,
                deadline: datetime,
            ) -> str:
                return self._inner.submit(kind, spec, attempt_id, deadline)

            def mark_running(self, job_id: str) -> None:
                self._inner.mark_running(job_id)
                self._inner.cancel(job_id)

            def __getattr__(self, name: str) -> Any:
                return getattr(self._inner, name)

        jobs = _CancelAfterStart(_jobs("st_" + "a" * 27, _Journal(), SystemClock()))
        outcome = await runtime.execute(
            _request(), jobs=jobs, artifacts=LocalArtifactStore(tmp_path / "a"),
            attempt_is_closed=lambda: False, output_names=_default_names(),
        )
        assert outcome.status == "failed" and outcome.retryable is False
        assert outcome.detail == "remote_job_cancelled"


class TestDeadline:
    @pytest.mark.asyncio
    async def test_deadline_stops_and_times_out_retryable(self, tmp_path: Path) -> None:
        transport = _FakeTransport()
        transport.script_default()
        transport.script["GET:/api/v1/status"] = (
            200,
            b'{"current_task":{"task_id":"t-1","status":"running"}}',
        )
        transport.script["POST:/api/v1/stop"] = (200, b'{"status":"stopping"}')
        near = datetime.now(UTC) + timedelta(seconds=0.15)
        runtime = _runtime(tmp_path, transport, poll_interval_s=0.02, clock=SystemClock())
        jobs = _jobs("st_" + "a" * 27, _Journal(), SystemClock())
        request = _request(deadline=near)
        outcome = await runtime.execute(
            request, jobs=jobs, artifacts=LocalArtifactStore(tmp_path / "a"),
            attempt_is_closed=lambda: False, output_names=_default_names(),
        )
        assert outcome.status == "timed_out" and outcome.retryable is True
        assert "stop ack is not termination proof" in outcome.detail
        assert jobs.get(outcome.job_id).status == "timed_out"
        stops = [url for method, url in transport.opened if method == "POST" and "/api/v1/stop" in url]
        assert len(stops) == 1 and "task_id=t-1" in stops[0]
        scope = runtime.store.scope(request.study_id, request.attempt_id, request.call_id)
        assert any(r.operation == "stop" and r.status_code == 200 for r in runtime.store.receipts(scope))
        # the binding survives the stop (never deleted)
        assert runtime.store.get(scope).task_id == "t-1"


class TestUnits:
    def test_retryable_classification(self) -> None:
        assert remote_reason_retryable("remote_transport_uncertain") is True
        assert remote_reason_retryable("remote_submit_http_502") is True
        assert remote_reason_retryable("remote_status_http_503") is True
        assert remote_reason_retryable("remote_download_http_500") is True
        assert remote_reason_retryable("remote_status_http_404") is False
        assert remote_reason_retryable("remote_turn_error") is False
        assert remote_reason_retryable("remote_request_identity_conflict") is False

    def test_output_names_validation(self) -> None:
        request = _request()
        assert output_names_for(request, {"outputs/result.json": "coder"}) == {"outputs/result.json": "coder"}
        with pytest.raises(RemoteCoderError, match="remote_output_names_invalid"):
            output_names_for(request, {"other/path": "coder"})
        with pytest.raises(RemoteCoderError, match="remote_output_names_invalid"):
            output_names_for(request, {"outputs/result.json": " "})
        two = _request(
            outputs=(
                RemoteOutput(path="a.json", media_type="application/json", sha256=None, size=None),
                RemoteOutput(path="b.json", media_type="application/json", sha256=None, size=None),
            )
        )
        with pytest.raises(RemoteCoderError, match="remote_output_names_invalid"):
            output_names_for(two, {"a.json": "same", "b.json": "same"})

    def test_settings_policy_digest_binds_values_without_storing_them(self) -> None:
        def _settings(token: str) -> RemoteCoderSettings:
            return RemoteCoderSettings(
                base_url="http://10.32.2.2:8884/",
                headers={"Authorization": token},
                limits=LIMITS,
                max_receipts=8,
                max_receipt_bytes=1024,
                poll_interval_s=0.05,
                http_factory=_FakeTransport,
            )

        base = _settings("Bearer test")
        assert base.policy_digest == _settings("Bearer test").policy_digest
        assert base.policy_digest != _settings("Bearer other").policy_digest
        assert "Bearer" not in base.policy_digest

    def test_settings_refuse_bad_poll_interval(self) -> None:
        with pytest.raises(RemoteCoderError, match="remote_poll_interval_invalid"):
            RemoteCoderSettings(
                base_url="http://10.32.2.2:8884",
                headers={},
                limits=LIMITS,
                max_receipts=8,
                max_receipt_bytes=1024,
                poll_interval_s=61.0,
                http_factory=_FakeTransport,
            )

    def test_runtime_refuses_bad_policy_digest(self, tmp_path: Path) -> None:
        from coscientist.tools.coder.remote_wire import RemoteWire

        wire = RemoteWire(
            base_url="http://10.32.2.2:8884",
            http=_FakeTransport(),
            clock=FakeClock(),
            headers={},
            limits=LIMITS,
        )
        with pytest.raises(RemoteCoderError, match="remote_digest_invalid"):
            RemoteCoderRuntime(
                wire=wire,
                store=RemoteSubmissionStore(tmp_path, max_receipts=1, max_receipt_bytes=64),
                policy=_AllowPolicy(),
                policy_digest="short",
                poll_interval_s=0.01,
            )

    def test_receipt_roundtrip_via_runtime_store(self, tmp_path: Path) -> None:
        store = RemoteSubmissionStore(tmp_path, max_receipts=4, max_receipt_bytes=1024)
        scope = store.scope("s", "a", "c")
        store.reserve(scope, {"x": 1})
        receipt = HttpReceipt("status", 200, b'{"current_task":{}}')
        store.receipt(scope, receipt)
        assert store.receipts(scope) == (receipt,)

    @pytest.mark.asyncio
    async def test_status_polls_do_not_count_toward_receipt_budget(self, tmp_path: Path) -> None:
        """Status polls are transport diagnostics, not deliverable traffic.

        Live wall st_042b6110: 177 unique-body status receipts (~108KB each)
        exhausted a 16 MiB per-study budget mid-study. The budget counts
        submit/download/stop receipts only; status rows stay journaled.
        """
        transport = _FakeTransport()
        transport.script_default()
        transport.script["GET:/api/v1/status"] = (
            200,
            b'{"current_task":{"task_id":"t-1","status":"running","tick":'
            + str(int(datetime.now(UTC).timestamp())).encode() + b"}}",
        )
        transport.script["POST:/api/v1/stop"] = (200, b'{"status":"stopping"}')
        near = datetime.now(UTC) + timedelta(seconds=0.15)
        runtime = RemoteCoderRuntime(
            wire=_WireHost(transport, SystemClock()).wire,
            store=RemoteSubmissionStore(tmp_path, max_receipts=3, max_receipt_bytes=65536),
            policy=_AllowPolicy(),
            policy_digest="ab" * 32,
            poll_interval_s=0.02,
        )
        jobs = _jobs("st_" + "b" * 27, _Journal(), SystemClock())
        request = _request(deadline=near)
        outcome = await runtime.execute(
            request, jobs=jobs, artifacts=LocalArtifactStore(tmp_path / "a"),
            attempt_is_closed=lambda: False, output_names=_default_names(),
        )
        assert outcome.status in ("ok", "timed_out")
        with runtime.store._connection() as connection:
            status_rows = connection.execute(
                "SELECT COUNT(*) FROM receipts WHERE operation='status'").fetchone()[0]
            assert status_rows >= 1
            budgeted = connection.execute(
                "SELECT COUNT(*) FROM receipts WHERE operation<>'status'").fetchone()[0]
            assert budgeted < 3, "status rows must not consume the receipt budget"
