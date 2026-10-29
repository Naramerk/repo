"""Remote coder attach (slice-2): CoderTool / invoke_coder_for_study remote route.

Fake transport only; the runtime under test is composed exactly like the CLI
does (docs/remote-coder-runtime.md §1/§6).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from coscientist.core.events.clock import FakeClock
from coscientist.core.execution.artifacts import LocalArtifactStore
from coscientist.core.execution.domain import HitlPolicy
from coscientist.core.execution.tool_loader import ensure_core_tools_ready
from coscientist.core.execution.tool_registry import _reset_registry_for_tests
from coscientist.tools.coder import CoderRequest, invoke_coder_for_study
from coscientist.tools.coder.remote_contract import (
    RemoteCoderTaskRequest,
    RemoteOutput,
)
from coscientist.tools.coder.remote_runtime import RemoteCoderRuntime
from coscientist.tools.coder.remote_store import RemoteSubmissionStore
from coscientist.tools.coder.remote_wire import RemoteLimits, RemoteWire
from coscientist.tools.contract import CallCtx, ToolError
from coscientist.tools.jobs import InMemoryJobStore
from tests.checks.sandbox_test_limits import sandbox_limits
from tests.helpers import close_runtime, open_runtime
from tests.tools.coder.test_remote_coder_runtime import _FakeTransport

DEADLINE = datetime.now(UTC) + timedelta(seconds=60)


def _remote_request(attempt_id: str = "att1", call_id: str = "call1") -> RemoteCoderTaskRequest:
    return RemoteCoderTaskRequest(
        study_id="st1",
        attempt_id=attempt_id,
        call_id=call_id,
        task="напиши скрипт, который пишет outputs/result.json",
        inputs=(),
        outputs=(RemoteOutput(path="outputs/result.json", media_type="application/json", sha256=None, size=None),),
        deadline=DEADLINE,
        wait_in_queue=True,
    )


def _local_request() -> CoderRequest:
    limits = sandbox_limits(timeout_s=5.0, memory_mb=64, max_output_bytes=4096)
    from coscientist.tools.coder import CoderOutputSpec

    assert limits is not None
    return CoderRequest(
        attempt_id="att1",
        call_id="call1",
        argv=("python3", "-c", "print('ok')"),
        inputs={},
        env={},
        limits=limits,
        deadline=DEADLINE,
        outputs=(CoderOutputSpec(name="coder", source="stdout", media_type="application/json"),),
        attempt_is_closed=lambda: False,
    )


def _runtime(tmp_path: Path) -> RemoteCoderRuntime:
    transport = _FakeTransport()
    transport.script_default(result_body=b'{"answer": 42}')

    class _AllowPolicy:
        def check(self, identity: dict[str, object]) -> None:
            return None

    return RemoteCoderRuntime(
        wire=RemoteWire(
            base_url="http://10.32.2.2:8884",
            http=transport,
            clock=FakeClock(),
            headers={},
            limits=RemoteLimits(
                request_bytes=8192,
                response_bytes=8192,
                download_bytes=8192,
                read_chunk_bytes=1024,
                json_depth=16,
                timeout_s=5.0,
                close_timeout_s=2.0,
            ),
        ),
        store=RemoteSubmissionStore(tmp_path / "bindings", max_receipts=16, max_receipt_bytes=8192),
        policy=_AllowPolicy(),
        policy_digest="ab" * 32,
        poll_interval_s=0.01,
    )


def _jobs() -> InMemoryJobStore:
    class _Journal:
        def record(self, event: object) -> None:
            return None

    return InMemoryJobStore(study_id="st1", clock=FakeClock(), journal=_Journal(), on_clock_change=None)


def _ctx(attempt_id: str = "att1") -> CallCtx:
    return CallCtx(
        study_id="st1",
        task_key="EXP-1",
        attempt_id=attempt_id,
        hitl_policy=HitlPolicy(defaults={"side_effect": "allow"}),
    )


@pytest.fixture(autouse=True)
def _registry() -> Any:
    _reset_registry_for_tests()
    ensure_core_tools_ready()
    yield
    _reset_registry_for_tests()


async def _start_attempt(rt: Any, *, attempt_id: str, task_id: str, study_id: str = "st1") -> None:
    """Journal an open attempt so the write-effect coder tool is untainted."""
    from coscientist.core.events.events import EventEnvelope, make_idem
    from coscientist.core.events.upcasters import writer_schema_version

    await rt.writer.append(
        EventEnvelope(
            study_id=study_id,
            type="AttemptStarted",
            entity_key=attempt_id,
            idem_key=make_idem("AttemptStarted", attempt_id),
            payload={
                "attempt_id": attempt_id,
                "n": 1,
                "dispatch_id": f"dispatch:{attempt_id}",
                "route_returned": False,
                "closed": False,
                "task_id": task_id,
                "study_id": "st1",
                "provenance": "real",
                "deadline": datetime.now(UTC).isoformat(),
                "inputs": [],
                "tainted_context": False,
                "limitations": [],
                "attempt_role": "agent",
            },
            ts=datetime.now(UTC),
            schema_version=writer_schema_version("AttemptStarted"),
        )
    )


@pytest.mark.asyncio
async def test_invoke_remote_via_invoke_tool(tmp_path: Path) -> None:
    """The remote request flows through invoke_tool(coder) → remote runtime →
    ToolOutput with CAS artifacts named by the expected output."""
    rt = await open_runtime(tmp_path, "st1")
    try:
        await _start_attempt(rt, attempt_id="att1", task_id="st1/EXP-1")
        arts = LocalArtifactStore(tmp_path / "arts")
        outcome, tool_out = await invoke_coder_for_study(
            _remote_request(),
            backend="remote-openhands",
            jobs=_jobs(),
            writer=rt.writer,
            artifacts=arts,
            ctx=_ctx(),
            remote_runtime=_runtime(tmp_path),
            remote_output_names={"outputs/result.json": "coder"},
            attempt_is_closed=lambda: False,
        )
        assert outcome.status == "succeeded"
        assert len(tool_out.artifacts) == 1
        meta = tool_out.artifacts[0]
        assert meta.name == "coder"
        assert meta.producer == "sandbox" and meta.provenance == "tool"
    finally:
        await close_runtime(rt)


@pytest.mark.asyncio
async def test_invoke_remote_download_400_is_typed_skip_not_tool_error(tmp_path: Path) -> None:
    """The invoke boundary carries the typed skip as data (issue #332): a
    download 4xx never surfaces as ToolError → CliRefuse → retry; the agent
    sees the facts and finishes the attempt on what exists."""
    rt = await open_runtime(tmp_path, "st5")
    try:
        await _start_attempt(rt, attempt_id="att1", task_id="st5/EXP-1", study_id="st5")
        transport = _FakeTransport()
        transport.script_default(result_body=b'{"answer": 42}')
        transport.script["GET:/api/v1/files/download"] = (400, b'{"error":"bad path"}')

        class _AllowPolicy:
            def check(self, identity: dict[str, object]) -> None:
                return None

        runtime = RemoteCoderRuntime(
            wire=RemoteWire(
                base_url="http://10.32.2.2:8884",
                http=transport,
                clock=FakeClock(),
                headers={},
                limits=RemoteLimits(
                    request_bytes=8192,
                    response_bytes=8192,
                    download_bytes=8192,
                    read_chunk_bytes=1024,
                    json_depth=16,
                    timeout_s=5.0,
                    close_timeout_s=2.0,
                ),
            ),
            store=RemoteSubmissionStore(tmp_path / "bindings", max_receipts=16, max_receipt_bytes=8192),
            policy=_AllowPolicy(),
            policy_digest="ab" * 32,
            poll_interval_s=0.01,
        )
        outcome, tool_out = await invoke_coder_for_study(
            _remote_request(),
            backend="remote-openhands",
            jobs=_jobs(),
            writer=rt.writer,
            artifacts=LocalArtifactStore(tmp_path / "arts"),
            ctx=_ctx(),
            remote_runtime=runtime,
            remote_output_names={"outputs/result.json": "coder"},
            attempt_is_closed=lambda: False,
        )
        assert outcome.status == "succeeded" and outcome.retryable is False
        assert outcome.outputs_unavailable == {"outputs/result.json": "remote_download_http_400"}
        assert tool_out.artifacts == []
        data = tool_out.data
        assert isinstance(data, dict) and data["outputs_unavailable"] == {
            "outputs/result.json": "remote_download_http_400"
        }
    finally:
        await close_runtime(rt)


@pytest.mark.asyncio
async def test_remote_without_runtime_refuses(tmp_path: Path) -> None:
    rt = await open_runtime(tmp_path, "st2")
    try:
        with pytest.raises(ToolError, match="requires remote_runtime"):
            await invoke_coder_for_study(
                _remote_request(),
                backend="remote-openhands",
                jobs=_jobs(),
                writer=rt.writer,
                artifacts=LocalArtifactStore(tmp_path / "a"),
                ctx=_ctx(),
            )
    finally:
        await close_runtime(rt)


@pytest.mark.asyncio
async def test_remote_backend_with_local_request_refuses(tmp_path: Path) -> None:
    rt = await open_runtime(tmp_path, "st3")
    try:
        with pytest.raises(ToolError, match="requires a RemoteCoderTaskRequest"):
            await invoke_coder_for_study(
                _local_request(),
                backend="remote-openhands",
                jobs=_jobs(),
                writer=rt.writer,
                artifacts=LocalArtifactStore(tmp_path / "a"),
                ctx=_ctx(),
                remote_runtime=_runtime(tmp_path),
                remote_output_names={"outputs/result.json": "coder"},
                attempt_is_closed=lambda: False,
            )
    finally:
        await close_runtime(rt)


@pytest.mark.asyncio
async def test_local_backend_with_remote_request_refuses(tmp_path: Path) -> None:
    rt = await open_runtime(tmp_path, "st4")
    try:
        with pytest.raises(ToolError, match="local-python invoke requires a local CoderRequest"):
            await invoke_coder_for_study(
                _remote_request(),
                backend="local-python",
                jobs=_jobs(),
                writer=rt.writer,
                artifacts=LocalArtifactStore(tmp_path / "a"),
                ctx=_ctx(),
            )
    finally:
        await close_runtime(rt)
