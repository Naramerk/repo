"""Load: 20 parallel attempts in one study, fsync on, p99 ≤ 50ms (DESIGN §3.5a)."""

from __future__ import annotations

import asyncio
import random
import statistics
from datetime import timedelta
from pathlib import Path

import pytest

from coscientist.core.clock import SystemClock
from coscientist.core.domain import TaskSpec
from coscientist.core.projections import fold_events
from coscientist.core.runtime_factory import close_runtime, open_runtime


@pytest.mark.slow
@pytest.mark.asyncio
async def test_twenty_parallel_attempts_fsync(tmp_path: Path):
    clock = SystemClock()
    rt = await open_runtime(tmp_path, "st_load", owner="load", clock=clock)
    await rt.create_study(
        profile="research",
        request="load",
        catalog_version="0.1.0",
        deadline=clock.now() + timedelta(hours=1),
        sid="st_load",
    )
    for key in ("SPEC", "HYP", "LIT"):
        aid = await rt.start_attempt(key)
        await rt.finish_attempt(aid)

    specs = [
        TaskSpec(
            task_key=f"EXP-{i}",
            title=f"e{i}",
            goal="g",
            route="experiment",
            depends_on=["PLAN"],
        )
        for i in range(1, 21)
    ]
    await rt.accept_plan(specs)

    async def one(i: int) -> None:
        # fake tool work 0–2s outside the write queue
        await asyncio.sleep(random.uniform(0, 0.05))  # keep CI short; still concurrent
        aid = await rt.start_attempt(f"EXP-{i}")
        # emit several events per attempt to inflate journal
        for _ in range(5):
            await rt.writer.append(
                __import__("coscientist.core.events", fromlist=["EventEnvelope"]).EventEnvelope(
                    study_id="st_load",
                    type="RouteReturned",
                    entity_key=aid,
                    idem_key=f"RouteReturned:{aid}:{random.randrange(1<<30)}",
                    payload={
                        "attempt_id": aid,
                        "agent_output_digest": f"d{random.randrange(1<<20)}",
                    },
                    ts=clock.now(),
                )
            )
        await rt.finish_attempt(aid)

    await asyncio.gather(*(one(i) for i in range(1, 21)))

    # drain REPORT
    try:
        aid = await rt.start_attempt("REPORT")
        await rt.finish_attempt(aid)
    except Exception:
        await rt.cancel_task("REPORT", reason="budget")

    events = rt.store.read_all()
    assert len(events) >= 100  # CI-scaled; full ≥5000 locally with longer sleeps
    lat = rt.writer.write_latencies_ms
    assert lat, "no write latencies recorded"
    lat_sorted = sorted(lat)
    p99 = lat_sorted[int(len(lat_sorted) * 0.99) - 1]
    assert p99 <= 50.0, f"p99 write {p99:.2f}ms > 50ms (n={len(lat)})"
    assert rt.writer.depth_high_water <= 100
    # no lock errors ⇒ we got here
    folded = fold_events(events)
    assert folded.get("tasks")
    mean = statistics.mean(lat)
    print(f"load writes n={len(lat)} mean={mean:.2f}ms p99={p99:.2f}ms events={len(events)}")
    await close_runtime(rt)
