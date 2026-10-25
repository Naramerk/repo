"""Coder workflow attach: hard sandbox preflight + invoke_tool seam."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from coscientist.checks.sandbox import SandboxLimits, require_cgroup_delegation
from coscientist.core.events.clock import SystemClock
from coscientist.core.events.errors import SandboxInfraError
from coscientist.core.events.events import EventEnvelope, UnstampedEvent, make_idem
from coscientist.core.events.upcasters import writer_schema_version
from coscientist.core.execution.artifacts import LocalArtifactStore
from coscientist.core.execution.domain import HitlPolicy
from coscientist.core.execution.tool_loader import ensure_core_tools_ready
from coscientist.core.execution.tool_registry import (
    _reset_registry_for_tests,
    get_registered,
)
from coscientist.tools.coder import (
    CODER_TOOL,
    CoderOutcome,
    CoderOutputSpec,
    CoderRequest,
    invoke_coder_for_study,
    require_supported_linux_sandbox,
)
from coscientist.tools.coder import run as coder_run
from coscientist.tools.contract import CallCtx, ToolError
from coscientist.tools.jobs import InMemoryJobStore
from tests.checks.sandbox_test_limits import sandbox_limits
from tests.helpers import close_runtime, open_runtime


class _Journal:
    def __init__(self) -> None:
        self.events: list[UnstampedEvent] = []

    def record(self, event: UnstampedEvent) -> None:
        self.events.append(event)


def _jobs() -> InMemoryJobStore:
    return InMemoryJobStore(
        study_id="st1",
        clock=SystemClock(),
        journal=_Journal(),
        on_clock_change=None,
    )


def _request(
    *,
    allow_cgroup_degraded: bool,
    allow_network: bool = False,
) -> CoderRequest:
    limits = sandbox_limits(
        timeout_s=5.0,
        memory_mb=64,
        max_output_bytes=4096,
        allow_cgroup_degraded=allow_cgroup_degraded,
        allow_network=allow_network,
    )
    assert isinstance(limits, SandboxLimits)
    return CoderRequest(
        attempt_id="att1",
        call_id="call1",
        argv=("python3", "-c", "print('ok')"),
        inputs={},
        env={},
        limits=limits,
        deadline=datetime.now(UTC) + timedelta(seconds=30),
        outputs=(
            CoderOutputSpec(name="stdout.txt", source="stdout", media_type="text/plain"),
        ),
        attempt_is_closed=lambda: False,
    )


def _ctx(*, study_id: str = "st1", attempt_id: str = "att1") -> CallCtx:
    return CallCtx(
        study_id=study_id,
        task_key="EXP-1",
        attempt_id=attempt_id,
        hitl_policy=HitlPolicy(defaults={"side_effect": "allow"}),
    )


async def _runtime_with_attempt(tmp_path: Path, sid: str, *, attempt_id: str = "att1"):
    rt = await open_runtime(tmp_path, sid)
    await rt.create_study(
        profile="research",
        request="coder workflow attach",
        catalog_version=3,
        sid=sid,
        finalize_reserve_s=5,
        require_hypotheses=False,
        inputs=[],
    )
    await rt.writer.append(
        EventEnvelope(
            study_id=sid,
            type="AttemptStarted",
            entity_key=attempt_id,
            idem_key=make_idem("AttemptStarted", attempt_id),
            payload={
                "attempt_id": attempt_id,
                "n": 1,
                "task_id": f"{sid}/EXP-1",
                "study_id": sid,
                "provenance": "real",
                "deadline": datetime.now(UTC).isoformat(),
                "inputs": [],
                "tainted_context": False,
                "limitations": [],
            },
            ts=datetime.now(UTC),
            schema_version=writer_schema_version("AttemptStarted"),
        )
    )
    return rt


@pytest.fixture(autouse=True)
def _registry() -> Any:
    _reset_registry_for_tests()
    ensure_core_tools_ready()
    yield
    _reset_registry_for_tests()


def test_coder_builtin_registered() -> None:
    spec = get_registered("coder")
    assert spec is not None
    assert spec.provider == "coder"
    assert spec.effect == "write"
    assert CODER_TOOL.spec.name == "coder"


def test_preflight_refuses_without_bwrap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "coscientist.tools.coder.attach.bwrap_available", lambda: False
    )
    with pytest.raises(ToolError, match="bwrap unavailable") as ei:
        require_supported_linux_sandbox(allow_network=False)
    assert ei.value.kind == "local"


def test_preflight_refuses_soft_isolation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "coscientist.tools.coder.attach.bwrap_available", lambda: True
    )
    monkeypatch.setattr(
        "coscientist.tools.coder.attach.resolve_isolation",
        lambda *, allow_network: ("soft", "soft"),
    )
    monkeypatch.setattr(
        "coscientist.tools.coder.attach.isolation_fully_hard",
        lambda net, fs: False,
    )
    with pytest.raises(ToolError, match="isolation not hard") as ei:
        require_supported_linux_sandbox(allow_network=False)
    assert ei.value.kind == "local"


def test_preflight_refuses_without_cgroup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "coscientist.tools.coder.attach.bwrap_available", lambda: True
    )
    monkeypatch.setattr(
        "coscientist.tools.coder.attach.resolve_isolation",
        lambda *, allow_network: ("hard", "hard"),
    )
    monkeypatch.setattr(
        "coscientist.tools.coder.attach.isolation_fully_hard",
        lambda net, fs: True,
    )
    monkeypatch.delenv("CGROUP_JOB", raising=False)

    with pytest.raises(SandboxInfraError, match="cgroup_job_required"):
        require_cgroup_delegation()

    with pytest.raises(ToolError, match="cgroup unavailable") as ei:
        require_supported_linux_sandbox(allow_network=False)
    assert ei.value.kind == "local"
    assert "cgroup_job_required" in ei.value.detail


@pytest.mark.asyncio
async def test_workflow_refuses_cgroup_degraded_bypass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "coscientist.tools.coder.attach.require_supported_linux_sandbox",
        lambda *, allow_network: None,
    )
    rt = await open_runtime(tmp_path, "st_coder_refuse")
    try:
        arts = LocalArtifactStore(tmp_path / "arts")
        with pytest.raises(ToolError, match="allow_cgroup_degraded bypass forbidden"):
            await invoke_coder_for_study(
                _request(allow_cgroup_degraded=True),
                jobs=_jobs(),
                writer=rt.writer,
                artifacts=arts,
                ctx=_ctx(),
            )
    finally:
        await close_runtime(rt)


@pytest.mark.asyncio
async def test_invoke_coder_for_study_via_invoke_tool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Workflow path reaches run_coder_job through invoke_tool (no invent)."""
    monkeypatch.setattr(
        "coscientist.tools.coder.attach.require_supported_linux_sandbox",
        lambda *, allow_network: None,
    )

    async def _fake_run(
        request: CoderRequest,
        *,
        jobs: InMemoryJobStore,
        artifacts: LocalArtifactStore,
    ) -> CoderOutcome:
        assert request.call_id == "call1"
        assert isinstance(jobs, InMemoryJobStore)
        assert isinstance(artifacts, LocalArtifactStore)
        return CoderOutcome(
            job_id="job_fake",
            attempt_id=request.attempt_id,
            status="succeeded",
            retryable=False,
            artifact_ids=[],
            detail="ok",
        )

    monkeypatch.setattr(coder_run, "run_coder_job", _fake_run)
    monkeypatch.setattr(
        "coscientist.tools.coder.attach.run_coder_job", _fake_run
    )

    rt = await _runtime_with_attempt(tmp_path, "st_coder_invoke")
    try:
        arts = LocalArtifactStore(tmp_path / "arts")
        outcome, tool_out = await invoke_coder_for_study(
            _request(allow_cgroup_degraded=False),
            jobs=_jobs(),
            writer=rt.writer,
            artifacts=arts,
            ctx=_ctx(study_id="st_coder_invoke"),
        )
        assert outcome.status == "succeeded"
        assert outcome.job_id == "job_fake"
        assert tool_out.data["status"] == "succeeded"
        # EffectDone journaled by invoke_tool write path.
        events = await rt.store.read_all()
        types = [e.type for e in events]
        assert "EffectIntended" in types
        assert "EffectDone" in types
    finally:
        await close_runtime(rt)


@pytest.mark.asyncio
async def test_coder_tool_refuses_mismatched_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "coscientist.tools.coder.attach.require_supported_linux_sandbox",
        lambda *, allow_network: None,
    )
    arts = LocalArtifactStore(tmp_path / "arts")
    ctx = _ctx(attempt_id="att1")
    ctx.extra = {
        "coder_request": _request(allow_cgroup_degraded=False),
        "jobs": _jobs(),
        "artifacts": arts,
    }
    with pytest.raises(ToolError, match="does not match request"):
        await CODER_TOOL.call(
            {"call_id": "call1", "attempt_id": "att_other"},
            ctx,
        )



def test_preflight_refuses_non_bool_allow_network() -> None:
    with pytest.raises(ToolError, match="allow_network must be bool"):
        require_supported_linux_sandbox(allow_network=MagicMock())  # type: ignore[arg-type]
