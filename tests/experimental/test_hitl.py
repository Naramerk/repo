"""HITL gate tests (experimental / pre-M2)."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from coscientist.core.events.clock import FakeClock
from coscientist.core.execution.domain import HitlPolicy
from coscientist.experimental.runtime import StudyRuntime as ExpStudyRuntime
from tests.helpers import attach_executor, close_runtime, open_runtime, record_success


async def _boot(tmp_path: Path, clock: FakeClock, seconds: int = 60, **kw):
    rt = await open_runtime(
        tmp_path, "st_tr", clock=clock, runtime_cls=ExpStudyRuntime
    )
    kw.setdefault("finalize_reserve_s", 5 if seconds > 5 else 1)
    await rt.create_study(
        profile="research",
        request="q",
        catalog_version=3,
        deadline=clock.now() + timedelta(seconds=seconds),
        sid="st_tr",
        **kw,
        require_hypotheses=False,
    )
    attach_executor(rt, tmp_path)
    return rt


@pytest.mark.asyncio
async def test_hitl_delegate_records_critic(tmp_path, clock):
    rt = await _boot(
        tmp_path,
        clock,
        seconds=60,
        hitl_policy=HitlPolicy(on_timeout="delegate"),
    )
    _, arts = attach_executor(rt, tmp_path)
    aid = await rt.start_attempt("SPEC")
    await record_success(rt, aid, arts)
    rid = await rt.open_hitl("PLAN", kind="plan_approval", timeout_s=1)
    clock.advance(timedelta(seconds=2))
    await rt.tick()
    assert rt.hitl[rid].open is False
    decided = [e for e in await rt.store.read_all() if e.type == "HitlDecided"]
    assert any(e.payload.get("approved_by") == "critic" for e in decided)
    assert any(e.payload.get("delegated_by") == "timeout" for e in decided)
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_hitl_human_delegate_records_critic(tmp_path, clock):
    """Non-costly answer_hitl('delegate') → critic + delegated_by=human (§7.3)."""
    rt = await _boot(tmp_path, clock, seconds=120)
    _, arts = attach_executor(rt, tmp_path)
    aid = await rt.start_attempt("SPEC")
    await record_success(rt, aid, arts)
    rid = await rt.open_hitl("PLAN", kind="plan_approval", timeout_s=60)
    await rt.answer_hitl(rid, "delegate")
    assert rt.hitl[rid].open is False
    decided = [
        e for e in await rt.store.read_all() if e.type == "HitlDecided" and e.entity_key == rid
    ]
    assert len(decided) == 1
    assert decided[0].payload.get("approved_by") == "critic"
    assert decided[0].payload.get("delegated_by") == "human"
    await close_runtime(rt)
