"""Core must refuse journals that still have open HITL gates."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from coscientist.core.errors import HitlUnsupported
from coscientist.core.machine import StudyRuntime as CoreStudyRuntime
from coscientist.core.recover import recover
from coscientist.core.runtime_factory import close_runtime, open_runtime
from coscientist.experimental.runtime import StudyRuntime as ExpStudyRuntime
from tests.helpers import attach_executor, record_success


async def _journal_with_open_hitl(tmp_path: Path, clock) -> tuple[Path, str]:
    """Write a study journal that leaves one HITL gate open; return (root, owner)."""
    root = tmp_path / "hitl_journal"
    rt = await open_runtime(
        root, "st_hitl", clock=clock, runtime_cls=ExpStudyRuntime, owner="owner-hitl"
    )
    await rt.create_study(
        profile="research",
        request="q",
        catalog_version="0.1.0",
        deadline=clock.now() + timedelta(minutes=30),
        sid="st_hitl",
    )
    _, arts = attach_executor(rt, root)
    await record_success(rt, await rt.start_attempt("SPEC"), arts)
    await rt.open_hitl("PLAN", kind="plan_approval", timeout_s=60)
    assert any(h.open for h in rt.hitl.values())
    owner = rt.owner
    await close_runtime(rt)
    # FakeClock lease TTL is 30s; expire it so a later open can acquire.
    clock.advance(timedelta(seconds=60))
    return root, owner


@pytest.mark.asyncio
async def test_core_open_runtime_rejects_open_hitl_journal(tmp_path, clock):
    root, _owner = await _journal_with_open_hitl(tmp_path, clock)
    with pytest.raises(HitlUnsupported, match="open HITL"):
        await open_runtime(
            root, "st_hitl", clock=clock, runtime_cls=CoreStudyRuntime, owner="core-o"
        )


@pytest.mark.asyncio
async def test_core_recover_rejects_open_hitl_journal(tmp_path, clock):
    """Core recover refuses open HITL even if the store is already attached."""
    root, owner = await _journal_with_open_hitl(tmp_path, clock)
    exp = await open_runtime(
        root, "st_hitl", clock=clock, runtime_cls=ExpStudyRuntime, owner=owner
    )
    core = CoreStudyRuntime(exp.store, exp.writer, clock=clock, owner=exp.owner)
    for ev in exp.store.read_all():
        core._project_local(ev)  # noqa: SLF001 — HitlOpened ignored by core fold
    with pytest.raises(HitlUnsupported, match="open HITL"):
        await recover(core)
    await close_runtime(exp)


@pytest.mark.asyncio
async def test_experimental_open_runtime_accepts_open_hitl_journal(tmp_path, clock):
    root, owner = await _journal_with_open_hitl(tmp_path, clock)
    rt = await open_runtime(
        root, "st_hitl", clock=clock, runtime_cls=ExpStudyRuntime, owner=owner
    )
    assert any(h.open for h in rt.hitl.values())
    await close_runtime(rt)
