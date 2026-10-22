"""HITL gate tests (experimental / pre-M2). Skipped on core runtime."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from coscientist.core.clock import FakeClock
from coscientist.core.domain import HitlPolicy
from coscientist.core.runtime_factory import close_runtime, open_runtime
from tests.helpers import attach_executor, record_success


async def _boot(tmp_path: Path, clock: FakeClock, runtime_cls: type, seconds: int = 60, **kw):
    rt = await open_runtime(
        tmp_path, "st_tr", clock=clock, runtime_cls=runtime_cls
    )
    await rt.create_study(
        profile="research",
        request="q",
        catalog_version="0.1.0",
        deadline=clock.now() + timedelta(seconds=seconds),
        sid="st_tr",
        **kw,
    )
    attach_executor(rt, tmp_path)
    return rt


@pytest.mark.asyncio
async def test_hitl_delegate_records_critic(tmp_path, clock, runtime_cls, require_hitl):
    rt = await _boot(
        tmp_path,
        clock,
        runtime_cls,
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
    decided = [e for e in rt.store.read_all() if e.type == "HitlDecided"]
    assert any(e.payload.get("approved_by") == "critic" for e in decided)
    assert any(e.payload.get("delegated_by") == "timeout" for e in decided)
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_hitl_human_delegate_records_critic(
    tmp_path, clock, runtime_cls, require_hitl
):
    """Non-costly answer_hitl('delegate') → critic + delegated_by=human (§7.3)."""
    rt = await _boot(tmp_path, clock, runtime_cls, seconds=120)
    _, arts = attach_executor(rt, tmp_path)
    aid = await rt.start_attempt("SPEC")
    await record_success(rt, aid, arts)
    rid = await rt.open_hitl("PLAN", kind="plan_approval", timeout_s=60)
    await rt.answer_hitl(rid, "delegate")
    assert rt.hitl[rid].open is False
    decided = [
        e for e in rt.store.read_all() if e.type == "HitlDecided" and e.entity_key == rid
    ]
    assert len(decided) == 1
    assert decided[0].payload.get("approved_by") == "critic"
    assert decided[0].payload.get("delegated_by") == "human"
    await close_runtime(rt)
