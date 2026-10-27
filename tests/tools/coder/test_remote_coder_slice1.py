"""Remote coder slice-1: contract validators, durable submission store, bounded wire.

Fake transport only — the design forbids any real submit in tests
(docs/remote-coder-design.md §28).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from coscientist.core.events.clock import FakeClock
from coscientist.mcp.errors import McpError
from coscientist.tools.coder.remote_contract import (
    HttpReceipt,
    RemoteCoderError,
    RemoteCoderTaskRequest,
    RemoteInput,
    RemoteOutput,
    SubmitIdentity,
    TaskIdentity,
    digest,
    endpoint,
    workspace_path,
)
from coscientist.tools.coder.remote_store import RemoteSubmissionStore
from coscientist.tools.coder.remote_wire import RemoteLimits, RemoteWire, parse_object

DEADLINE = datetime(2027, 1, 1, tzinfo=UTC)


def _request(**overrides: Any) -> RemoteCoderTaskRequest:
    fields: dict[str, Any] = {
        "study_id": "st_" + "a" * 27,
        "attempt_id": "st_" + "a" * 27 + "/EXP-0#1",
        "call_id": "st_" + "a" * 27 + "/EXP-0#1:1",
        "task": "построй тепловую карту из ld50_predictions.csv",
        "inputs": (
            RemoteInput(
                artifact_id="art_" + "b" * 32,
                sha256=digest("ab" * 32),
                size=10,
                path="inputs/ld50_predictions.csv",
                url="http://10.32.2.2:9000/inputs/ld50.csv",
                required=True,
            ),
        ),
        "outputs": (RemoteOutput(path="outputs/heatmap.png", media_type="image/png", sha256=None, size=None),),
        "deadline": DEADLINE,
        "wait_in_queue": True,
    }
    fields.update(overrides)
    return RemoteCoderTaskRequest(**fields)


class TestContract:
    def test_identity_excludes_expiring_url_but_includes_digest(self) -> None:
        request = _request()
        base = request.identity("http://10.32.2.2:8884", "cd" * 32)
        new_url = request.inputs[0].__class__(
            artifact_id=request.inputs[0].artifact_id,
            sha256=request.inputs[0].sha256,
            size=request.inputs[0].size,
            path=request.inputs[0].path,
            url="http://10.32.2.2:9000/other-signed-url",
            required=True,
        )
        resigned = RemoteCoderTaskRequest(
            study_id=request.study_id,
            attempt_id=request.attempt_id,
            call_id=request.call_id,
            task=request.task,
            inputs=(new_url,),
            outputs=request.outputs,
            deadline=request.deadline,
            wait_in_queue=request.wait_in_queue,
        )
        assert resigned.identity("http://10.32.2.2:8884", "cd" * 32) == base
        assert "url" not in base["inputs"][0]

    def test_identity_binds_endpoint_policy_and_wire_shape(self) -> None:
        request = _request()
        identity = request.identity("http://10.32.2.2:8884/", "cd" * 32)
        assert identity["endpoint"] == "http://10.32.2.2:8884"
        assert identity["policy_digest"] == "cd" * 32
        assert identity["new_container"] is True
        wire = request.wire()
        assert b"ld50.csv" in wire and b"new_container" in wire

    def test_duplicate_paths_and_empty_outputs_refuse(self) -> None:
        output = RemoteOutput(path="out/result.json", media_type="application/json", sha256=None, size=None)
        with pytest.raises(RemoteCoderError, match="remote_duplicate_path"):
            _request(outputs=(output, output))
        with pytest.raises(RemoteCoderError, match="remote_outputs_required"):
            _request(outputs=())

    def test_naive_deadline_and_bad_paths_refuse(self) -> None:
        with pytest.raises(RemoteCoderError, match="remote_deadline_naive"):
            _request(deadline=datetime(2027, 1, 1))
        with pytest.raises(RemoteCoderError, match="remote_workspace_path_invalid"):
            _request(inputs=(_request().inputs[0].__class__(
                artifact_id="art_" + "b" * 32,
                sha256=digest("ab" * 32),
                size=1,
                path="../escape.csv",
                url="http://10.32.2.2:9000/x",
                required=True,
            ),))

    def test_workspace_path_and_digest_validators(self) -> None:
        assert workspace_path("inputs/ld50.csv") == "inputs/ld50.csv"
        with pytest.raises(RemoteCoderError):
            workspace_path("/abs/path")
        with pytest.raises(RemoteCoderError):
            digest("nothex")

    def test_output_path_colliding_with_input_refuses(self) -> None:
        input_entry = _request().inputs[0]
        with pytest.raises(RemoteCoderError, match="remote_duplicate_path"):
            _request(
                outputs=(RemoteOutput(path=input_entry.path, media_type="text/csv", sha256=None, size=None),),
            )

    def test_non_datetime_deadline_refuses_typed(self) -> None:
        with pytest.raises(RemoteCoderError, match="remote_deadline_naive"):
            _request(deadline="2027-01-01T00:00:00+00:00")


    def test_endpoint_normalizes_and_refuses_credentials(self) -> None:
        assert endpoint("http://10.32.2.2:8884/") == "http://10.32.2.2:8884"
        with pytest.raises(RemoteCoderError):
            endpoint("http://user:pass@10.32.2.2:8884")

    def test_identity_envelopes_strict(self) -> None:
        assert SubmitIdentity.model_validate({"task_id": "t-1"}).task_id == "t-1"
        assert TaskIdentity.model_validate({"task_id": "t-1", "status": "cooldown"}).status == "cooldown"
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            TaskIdentity.model_validate({"task_id": "t-1", "status": "somewhere"})


class TestStore:
    def test_reserve_bind_get_and_identity_conflict(self, tmp_path: Any) -> None:
        store = RemoteSubmissionStore(tmp_path, max_receipts=4, max_receipt_bytes=4096)
        scope = store.scope("st_" + "a" * 27, "attempt-1", "call-1")
        identity = {"task": "t", "inputs": []}
        binding, created = store.reserve(scope, identity)
        assert created and binding.task_id is None
        binding2, created2 = store.reserve(scope, identity)
        assert created2 is False and binding2.request_digest == binding.request_digest
        with pytest.raises(RemoteCoderError, match="remote_request_identity_conflict"):
            store.reserve(scope, {"task": "changed"})
        store.bind(scope, binding.request_digest, "remote-task-7")
        assert store.get(scope).task_id == "remote-task-7"
        with pytest.raises(RemoteCoderError, match="remote_task_binding_conflict"):
            store.bind(scope, binding.request_digest, "remote-task-8")

    def test_receipt_budget_and_digest_roundtrip(self, tmp_path: Any) -> None:
        store = RemoteSubmissionStore(tmp_path, max_receipts=2, max_receipt_bytes=64)
        scope = store.scope("st_" + "a" * 27, "attempt-1", "call-1")
        store.reserve(scope, {"task": "t"})
        receipt = HttpReceipt("submit", 200, b'{"task_id":"t-1"}')
        store.receipt(scope, receipt)
        store.receipt(scope, receipt)
        assert store.receipts(scope) == (receipt,)
        with pytest.raises(RemoteCoderError, match="remote_receipt_budget_exhausted"):
            store.receipt(scope, HttpReceipt("submit", 200, b"x" * 63))
        with pytest.raises(RemoteCoderError, match="remote_receipt_budget_exhausted"):
            store.receipt(scope, HttpReceipt("status", 200, b"z" * 63))


class _Stream:
    def __init__(self, *, status_code: int, headers: dict[str, str], body: bytes) -> None:
        self.status_code = status_code
        self.headers = headers
        self._body = body

    async def read(self, n: int) -> bytes:
        chunk, self._body = self._body[:n], self._body[n:]
        return chunk

    async def aclose(self) -> None:
        return None


class _Transport:
    def __init__(self, *, status_code: int = 200, headers: dict[str, str] | None = None, body: bytes = b"{}",
                 fail: bool = False) -> None:
        self.status_code = status_code
        self.headers = headers or {}
        self.body = body
        self.fail = fail
        self.opened: list[tuple[str, str]] = []

    async def open(self, method: str, url: str, *, headers: dict[str, str], body: bytes | None,
                   timeout_s: float, connect_ip: str | None, server_hostname: str | None = None) -> _Stream:
        self.opened.append((method, url))
        if self.fail:
            raise McpError("transport down")
        return _Stream(status_code=self.status_code, headers=self.headers, body=self.body)


LIMITS = RemoteLimits(
    request_bytes=4096, response_bytes=4096, download_bytes=4096, read_chunk_bytes=1024,
    json_depth=16, timeout_s=5.0, close_timeout_s=2.0,
)


def _wire(transport: _Transport) -> RemoteWire:
    return RemoteWire(
        base_url="http://10.32.2.2:8884",
        http=transport,
        clock=FakeClock(),
        headers={"Authorization": "Bearer test"},
        limits=LIMITS,
    )


class TestWire:
    @pytest.mark.asyncio
    async def test_exchange_returns_receipt_and_hits_api_path(self) -> None:
        transport = _Transport(body=b'{"task_id":"t-9"}')
        wire = _wire(transport)
        receipt = await wire.exchange(
            "POST", "run", body=b"{}", deadline=datetime.now(UTC) + timedelta(seconds=30),
            operation="submit", max_bytes=1024,
        )
        assert receipt.status_code == 200 and receipt.body == b'{"task_id":"t-9"}'
        assert transport.opened == [("POST", "http://10.32.2.2:8884/api/v1/run")]
        assert receipt.sha256

    @pytest.mark.asyncio
    async def test_redirect_and_encoding_refused(self) -> None:
        redirected = _Transport(status_code=302)
        with pytest.raises(RemoteCoderError, match="remote_redirect_refused"):
            await _wire(redirected).exchange(
                "GET", "status", body=None, deadline=datetime.now(UTC) + timedelta(seconds=5),
                operation="status", max_bytes=64,
            )
        gzipped = _Transport(headers={"Content-Encoding": "gzip"})
        with pytest.raises(RemoteCoderError, match="remote_encoding_refused"):
            await _wire(gzipped).exchange(
                "GET", "status", body=None, deadline=datetime.now(UTC) + timedelta(seconds=5),
                operation="status", max_bytes=64,
            )

    @pytest.mark.asyncio
    async def test_oversize_response_and_transport_error_are_typed(self) -> None:
        big = _Transport(body=b"x" * 2048)
        with pytest.raises(RemoteCoderError, match="remote_response_exceeded"):
            await _wire(big).exchange(
                "GET", "files/download", body=None, deadline=datetime.now(UTC) + timedelta(seconds=5),
                operation="download", max_bytes=64,
            )
        down = _Transport(fail=True)
        with pytest.raises(RemoteCoderError, match="remote_transport_uncertain"):
            await _wire(down).exchange(
                "GET", "status", body=None, deadline=datetime.now(UTC) + timedelta(seconds=5),
                operation="status", max_bytes=64,
            )

    @pytest.mark.asyncio
    async def test_elapsed_deadline_refuses_before_open(self) -> None:
        transport = _Transport()
        past = datetime(2026, 1, 1, tzinfo=UTC) - timedelta(seconds=1)
        wire = RemoteWire(
            base_url="http://10.32.2.2:8884",
            http=transport,
            clock=FakeClock(start=datetime(2026, 1, 1, tzinfo=UTC)),
            headers={},
            limits=LIMITS,
        )
        with pytest.raises(RemoteCoderError, match="remote_deadline_elapsed"):
            await wire.exchange(
                "GET", "status", body=None, deadline=past,
                operation="status", max_bytes=64,
            )
        assert transport.opened == []


class TestParseObject:
    def test_duplicate_key_refused(self) -> None:
        with pytest.raises(RemoteCoderError, match="remote_json_duplicate_key"):
            parse_object(b'{"a": 1, "a": 2}', max_depth=8)

    def test_depth_nonfinite_and_nonobject_refused(self) -> None:
        with pytest.raises(RemoteCoderError, match="remote_json_depth_exceeded"):
            parse_object(b'{"a":' + b"[" * 9 + b"]" * 9 + b"}", max_depth=8)
        with pytest.raises(RemoteCoderError, match="remote_json_invalid"):
            parse_object(b'{"a": NaN}', max_depth=8)
        with pytest.raises(RemoteCoderError, match="remote_json_object_required"):
            parse_object(b"[1,2]", max_depth=8)
