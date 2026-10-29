"""Remote coder slice-2 e2e: coder-hinted task on the remote backend (run path).

Fake transport only — no real submit (docs/remote-coder-runtime.md §22). The
model authors the task content (generation body), the dispatch validates
coverage, and the composed remote runtime drives submit → status → download.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from coscientist.agents.schemas import OutputSpecDraft, RemoteCoderTaskSpec, TaskSpecDraft
from coscientist.cli.coder_remote import _validate_generated_coverage
from coscientist.cli.errors import CliRefuse
from coscientist.cli.static_router import StaticToolRouter
from coscientist.core.events.clock import FakeClock
from coscientist.core.events.store import SqliteEventStore
from coscientist.core.execution.artifacts import LocalArtifactStore
from coscientist.tools.coder.remote_runtime import RemoteCoderSettings
from coscientist.tools.coder.remote_wire import RemoteLimits
from tests.cli.test_run_study import (
    TaskIntent,
    _coder_plan_json,
    _coder_router,
    _intent_json,
    _ok_body,
    _run,
    _spec_json,
)

_RESULT_BODY = b'{"answer": 42}'


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


class _FakeRemoteTransport:
    def __init__(self) -> None:
        self.opened: list[tuple[str, str]] = []

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
        if method == "POST" and "/api/v1/run" in url:
            return _Stream(status_code=200, body=b'{"task_id":"t-1"}')
        if method == "GET" and "/api/v1/status" in url:
            payload = {"current_task": {"task_id": "t-1", "status": "completed"}}
            return _Stream(status_code=200, body=json.dumps(payload).encode())
        if method == "GET" and "/api/v1/files/download" in url:
            return _Stream(status_code=200, body=_RESULT_BODY)
        raise AssertionError(f"unexpected exchange: {method} {url}")


def _remote_settings(transport: _FakeRemoteTransport) -> RemoteCoderSettings:
    return RemoteCoderSettings(
        base_url="http://10.32.2.2:8884",
        headers={},
        limits=RemoteLimits(
            request_bytes=65536,
            response_bytes=1_048_576,
            download_bytes=1_048_576,
            read_chunk_bytes=8192,
            json_depth=16,
            timeout_s=30.0,
            close_timeout_s=5.0,
        ),
        max_receipts=64,
        max_receipt_bytes=262_144,
        poll_interval_s=0.01,
        http_factory=lambda: transport,
    )


def _generation_body_invented_output() -> str:
    """A spec naming an output the task never declared (invention, not coverage)."""
    return RemoteCoderTaskSpec(
        task="Напиши python-скрипт result.py: он записывает outputs/result.json с ответом 42.",
        outputs=(
            {"name": "result", "path": "outputs/result.json", "media_type": "application/json"},
        ),
    ).model_dump_json()


def _generation_body_with_output() -> str:
    return RemoteCoderTaskSpec(
        task="Напиши python-скрипт result.py: он записывает outputs/result.json с ответом 42.",
        outputs=(
            {"name": "coder", "path": "outputs/result.json", "media_type": "application/json"},
        ),
    ).model_dump_json()


def _coder_plan_two_outputs_json() -> str:
    """Coder task declaring TWO expected outputs (derive marks both required)."""
    return _intent_json(
        (
            TaskIntent(
                question_indices=(1,),
                goal="run the analysis code",
                route="experiment",
                key="EXP-0",
                tools_hint=("coder",),
                output_names=("coder", "figures"),
            ),
        )
    )


def _generation_body_subset_output() -> str:
    """Schema-valid SUBSET: only 'coder' returned of the two declared outputs."""
    return RemoteCoderTaskSpec(
        task="Напиши python-скрипт result.py: он записывает outputs/result.json с ответом 42.",
        outputs=(
            {"name": "coder", "path": "outputs/result.json", "media_type": "application/json"},
        ),
    ).model_dump_json()


@pytest.mark.asyncio
async def test_remote_coder_task_fulfilled_end_to_end(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    transport = _FakeRemoteTransport()
    bodies = [
        _ok_body(content=_spec_json(), prompt_tokens=3, completion_tokens=5),
        _ok_body(content=_coder_plan_json(), prompt_tokens=10, completion_tokens=20),
        _ok_body(content=_generation_body_with_output(), prompt_tokens=7, completion_tokens=11),
    ]
    code, summary, _router = await _run(
        tmp_path,
        monkeypatch=monkeypatch,
        bodies=bodies,
        router=_coder_router(),
        coder_backend="remote-openhands",
        remote_coder=_remote_settings(transport),
        study_id="remote_coder_study",
    )
    assert code == 0
    assert summary["verdict"] == "fulfilled"
    coder_rows = [u for u in summary["usage"] if u["role"] == "coder"]
    assert len(coder_rows) == 1 and coder_rows[0]["status"] == "succeeded"
    gen_rows = [u for u in summary["usage"] if u["role"] == "remote-coder-gen"]
    assert len(gen_rows) == 1
    # exchanges: POST run + GET status + GET download; no stop
    assert [m for m, _ in transport.opened] == ["POST", "GET", "GET"]
    assert not [u for u in summary["usage"] if u["role"] == "executor-route"]

    store = SqliteEventStore(
        "remote_coder_study", tmp_path / "data" / "remote_coder_study" / "events.db", clock=FakeClock()
    )
    events = await store.read_all()
    types = [e.type for e in events]
    assert "JobSubmitted" in types and "EffectDone" in types and "ToolCalled" in types
    await store.close()

    artifact_ids = coder_rows[0]["artifact_ids"]
    arts = LocalArtifactStore(tmp_path / "data" / "remote_coder_study" / "arts")
    meta = arts.get_meta(artifact_ids[0])
    assert meta is not None
    assert meta.name == "coder"
    assert meta.producer == "sandbox" and meta.provenance == "tool"
    assert arts.read(artifact_ids[0]) == _RESULT_BODY
    # durable submission metadata on the run path
    binding_store = tmp_path / "data" / "remote_coder_study" / "coder-remote" / "remote-coder.sqlite3"
    assert binding_store.exists()


@pytest.mark.asyncio
async def test_remote_auth_header_value_never_reaches_durable_storage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Journal-scan: the Authorization value must not appear in ANY durable
    byte — study journal, binding store, receipts — only header fingerprints
    (one-way digests) may bind the auth configuration."""
    token = "Bearer super-secret-token-xyz-never-store"
    transport = _FakeRemoteTransport()
    settings = _remote_settings(transport)
    storage = RemoteCoderSettings(
        base_url=settings.base_url,
        headers={"Authorization": token},
        limits=settings.limits,
        max_receipts=settings.max_receipts,
        max_receipt_bytes=settings.max_receipt_bytes,
        poll_interval_s=settings.poll_interval_s,
        http_factory=settings.http_factory,
    )
    bodies = [
        _ok_body(content=_spec_json()),
        _ok_body(content=_coder_plan_json()),
        _ok_body(content=_generation_body_with_output()),
    ]
    code, _summary, _router = await _run(
        tmp_path,
        monkeypatch=monkeypatch,
        bodies=bodies,
        router=_coder_router(),
        coder_backend="remote-openhands",
        remote_coder=storage,
        study_id="remote_coder_secrets",
    )
    assert code == 0
    # the wire actually carried the header (transport saw the request)
    assert transport.opened and "/api/v1/run" in transport.opened[0][1]
    study_dir = tmp_path / "data" / "remote_coder_secrets"
    events_blob = (study_dir / "events.db").read_bytes()
    binding_blob = (study_dir / "coder-remote" / "remote-coder.sqlite3").read_bytes()
    assert token.encode() not in events_blob
    assert token.encode() not in binding_blob
    assert token.split()[1].encode() not in events_blob
    assert token.split()[1].encode() not in binding_blob


@pytest.mark.asyncio
async def test_remote_generation_invented_output_refuses_at_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The invented output name fails the generation schema itself (code 4), no remote submit.

    The response schema pins ``outputs[].name`` to the task's declared expected
    outputs (structural anti-invention, st_idem_234ce9bd), so an invented name
    is refused at generation — before the dispatch coverage validator. That
    validator stays as the honest backstop (media_type/path/coverage), but name
    invention can no longer reach it.
    """
    transport = _FakeRemoteTransport()
    bodies = [
        _ok_body(content=_spec_json()),
        _ok_body(content=_coder_plan_json()),
        _ok_body(content=_generation_body_invented_output()),
    ]
    with pytest.raises(CliRefuse, match="generation for task 'EXP-0' failed") as ei:
        await _run(
            tmp_path,
            monkeypatch=monkeypatch,
            bodies=bodies,
            router=_coder_router(),
            coder_backend="remote-openhands",
            remote_coder=_remote_settings(transport),
            study_id="remote_coder_refuse",
        )
    assert ei.value.code == 4
    assert transport.opened == []  # refusal before any task request


@pytest.mark.asyncio
async def test_remote_generation_subset_refuses_missing_required(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A schema-valid SUBSET passes the generation enum and must die at coverage.

    The pinned schema enumerates declared names (membership), it does not force
    coverage: the model returned only 'coder' of the two declared outputs and
    passes generation — the dispatch coverage validator refuses (code 3), no
    remote submit.
    """
    transport = _FakeRemoteTransport()
    bodies = [
        _ok_body(content=_spec_json()),
        _ok_body(content=_coder_plan_two_outputs_json()),
        _ok_body(content=_generation_body_subset_output()),
    ]
    with pytest.raises(CliRefuse, match="does not cover required outputs") as ei:
        await _run(
            tmp_path,
            monkeypatch=monkeypatch,
            bodies=bodies,
            router=_coder_router(),
            coder_backend="remote-openhands",
            remote_coder=_remote_settings(transport),
            study_id="remote_coder_subset",
        )
    assert ei.value.code == 3
    assert transport.opened == []  # refusal before any task request


def test_dispatch_coverage_validator_refuses_invented_output() -> None:
    """The invented-outputs branch pinned directly (bypasses generation).

    Unreachable through the adapter since the response schema pins output names
    to the declared enum (structural anti-invention); kept as the honest
    fail-closed backstop for direct-spec paths — exercised here via
    ``_validate_generated_coverage``, not deleted.
    """
    draft = TaskSpecDraft(
        task_key="EXP-0",
        title="compute",
        goal="посчитать метрику",
        route="experiment",
        depends_on=(),
        addresses=("q1",),
        tools_hint=("coder",),
        expected_outputs=(
            OutputSpecDraft(
                name="coder", media_type="application/json", schema_ref=None, required=True, rubric=None
            ),
        ),
        criteria=(),
    )
    spec = RemoteCoderTaskSpec(
        task="напиши и запусти скрипт",
        outputs=(
            {"name": "coder", "path": "outputs/result.json", "media_type": "application/json"},
            # required outputs fully covered above; the extra name is invention
            {"name": "chemical_space_map", "path": "outputs/map.png", "media_type": "image/png"},
        ),
    )
    with pytest.raises(CliRefuse, match="invented outputs") as ei:
        _validate_generated_coverage(draft, spec)
    assert ei.value.code == 3


@pytest.mark.asyncio
async def test_remote_backend_without_settings_refuses(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bodies = [
        _ok_body(content=_spec_json()),
        _ok_body(content=_coder_plan_json()),
    ]
    router = _coder_router()
    assert isinstance(router, StaticToolRouter)
    with pytest.raises(CliRefuse, match="remote coder settings") as ei:
        await _run(
            tmp_path,
            monkeypatch=monkeypatch,
            bodies=bodies,
            router=router,
            coder_backend="remote-openhands",
            study_id="remote_coder_nosettings",
        )
    # composition-time gate: usage error before any plan/LLM work
    assert ei.value.code == 2


class _Down:
    async def open(self, *a: Any, **k: Any) -> _Stream:
        raise OSError("network unreachable")


def _down_settings() -> RemoteCoderSettings:
    base = _remote_settings(_FakeRemoteTransport())
    return RemoteCoderSettings(
        base_url=base.base_url,
        headers={},
        limits=base.limits,
        max_receipts=base.max_receipts,
        max_receipt_bytes=base.max_receipt_bytes,
        poll_interval_s=base.poll_interval_s,
        http_factory=_Down,
    )


@pytest.mark.asyncio
async def test_remote_transport_down_is_honest_refuse(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bodies = [
        _ok_body(content=_spec_json()),
        _ok_body(content=_coder_plan_json()),
        _ok_body(content=_generation_body_with_output()),
    ]
    with pytest.raises(CliRefuse, match="remote_transport_uncertain") as ei:
        await _run(
            tmp_path,
            monkeypatch=monkeypatch,
            bodies=bodies,
            router=_coder_router(),
            coder_backend="remote-openhands",
            remote_coder=_down_settings(),
            study_id="remote_coder_down",
        )
    assert ei.value.code == 4
    # honest retryable transport failure — never a success
    assert "remote_transport_uncertain" in ei.value.detail


class _400DownloadTransport(_FakeRemoteTransport):
    """The live failure class (issue #332): the job executes to completion,
    then the sandbox refuses every files/download with HTTP 400."""

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
        if method == "GET" and "/api/v1/files/download" in url:
            self.opened.append((method, url))
            return _Stream(status_code=400, body=b'{"error":"no such file"}')
        return await super().open(
            method, url, headers=headers, body=body, timeout_s=timeout_s,
            connect_ip=connect_ip, server_hostname=server_hostname,
        )


@pytest.mark.asyncio
async def test_remote_download_400_is_typed_skip_without_task_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Download 4xx is NOT a coder refusal and NOT a retry: the dispatch
    finishes on what materialized with typed outputs_unavailable facts, and
    the task code is never regenerated (issue #332: job_d96a02fb burned a
    ~900s generation per retry against the same deterministic 400)."""
    transport = _400DownloadTransport()
    bodies = [
        _ok_body(content=_spec_json()),
        _ok_body(content=_coder_plan_json()),
        _ok_body(content=_generation_body_with_output()),
    ]
    raised: CliRefuse | None = None
    code, summary, _router = -1, {}, None
    try:
        code, summary, _router = await _run(
            tmp_path,
            monkeypatch=monkeypatch,
            bodies=bodies,
            router=_coder_router(),
            coder_backend="remote-openhands",
            remote_coder=_remote_settings(transport),
            study_id="remote_coder_skip400",
        )
    except CliRefuse as exc:
        # The only honest terminal refuse left is the study verdict itself.
        raised = exc
    # Exactly one submit and one generation body consumed: no retry loop
    # re-drove the task against the same deterministic 400.
    submits = [u for m, u in transport.opened if m == "POST" and "/api/v1/run" in u]
    assert len(submits) == 1
    downloads = [u for m, u in transport.opened if m == "GET" and "files/download" in u]
    assert len(downloads) == 1
    if raised is None:
        assert code == 0 and summary["verdict"] == "fulfilled"
        coder_rows = [u for u in summary["usage"] if u["role"] == "coder"]
        assert len(coder_rows) == 1
        assert coder_rows[0]["status"] == "succeeded"
        assert coder_rows[0]["artifact_ids"] == []
        assert coder_rows[0]["outputs_unavailable"] == {
            "outputs/result.json": "remote_download_http_400"
        }
    else:
        # Acceptance judged honestly on the empty output set (no artifact was
        # materialized): the study closes non-fulfilled ('partial') — never a
        # coder refusal, never a silent pass of the missing artifact.
        assert raised.code == 7
        assert "verdict='partial'" in raised.detail
