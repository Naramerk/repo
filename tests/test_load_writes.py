"""Load: 20 parallel attempts in one study, fsync on (DESIGN §3.5a)."""

from __future__ import annotations

import asyncio
import random
import statistics
from datetime import timedelta
from pathlib import Path

import pytest

from coscientist.core.clock import SystemClock
from coscientist.core.events import EventEnvelope
from coscientist.core.projections import fold_events
from tests.helpers import attach_executor, close_runtime, exp_spec, open_runtime, record_success

# DESIGN §3.5a absolute SLO — do not raise; no retries/skips.
_P99_SLO_MS = 50.0


async def _run_load(tmp_path: Path, runtime_cls: type):
    """20 parallel attempts: full writer path (queue → to_thread → fence/insert/fsync)
    plus runtime/executor ``_project_local`` after durable append (real disk ``tmp_path``).
    """
    clock = SystemClock()
    rt = await open_runtime(tmp_path, "st_load", clock=clock, runtime_cls=runtime_cls)
    await rt.create_study(
        profile="research",
        request="load",
        catalog_version="0.1.0",
        deadline=clock.now() + timedelta(hours=1),
        sid="st_load",
    )
    _, arts = attach_executor(rt, tmp_path)
    for key in ("SPEC", "HYP", "LIT"):
        aid = await rt.start_attempt(key)
        await record_success(rt, aid, arts)

    await rt.accept_plan([exp_spec(f"EXP-{i}") for i in range(1, 21)])

    async def one(i: int) -> None:
        await asyncio.sleep(random.uniform(0, 0.05))
        aid = await rt.start_attempt(f"EXP-{i}")
        for _ in range(5):
            await rt.writer.append(
                EventEnvelope(
                    study_id="st_load",
                    type="RouteReturned",
                    entity_key=aid,
                    idem_key=f"RouteReturned:{aid}:{random.randrange(1 << 30)}",
                    payload={
                        "attempt_id": aid,
                        "agent_output_digest": f"d{random.randrange(1 << 20)}",
                    },
                    ts=clock.now(),
                )
            )
        await record_success(rt, aid, arts)

    await asyncio.gather(*(one(i) for i in range(1, 21)))
    await rt.cancel_task("REPORT", reason="budget")
    return rt


@pytest.mark.slow
@pytest.mark.serial
@pytest.mark.asyncio
async def test_twenty_parallel_attempts_fsync(tmp_path: Path, runtime_cls: type):
    """Deterministic correctness under fsync load (no flaky latency budget)."""
    rt = await _run_load(tmp_path, runtime_cls)
    events = await rt.store.read_all()
    assert len(events) >= 100
    assert rt.writer.depth_high_water <= 100
    assert fold_events(events).get("tasks")
    lat = rt.writer.latency_samples_ms()
    assert lat, "no write latencies recorded"
    print(
        f"load writes n={len(lat)} mean={statistics.mean(lat):.2f}ms "
        f"events={len(events)} (latency SLO asserted in perf job)"
    )
    await close_runtime(rt)


@pytest.mark.perf
@pytest.mark.serial
@pytest.mark.asyncio
async def test_twenty_parallel_attempts_fsync_p99_slo(
    tmp_path: Path, runtime_cls: type
):
    """Absolute DESIGN §3.5a SLO: p99 full-path write ≤ 50ms.

    Measures ``StudyWriter.latency_samples_ms`` (queue item → ``to_thread`` →
    fence/insert/JSON/projection-upsert/commit under ``synchronous=FULL``) for
    the 20-parallel load on real-disk ``tmp_path``. No shm, no commit-only
    timer, no wal/GC knobs, no retries/skips.
    """
    rt = await _run_load(tmp_path, runtime_cls)
    lat = rt.writer.latency_samples_ms()
    assert lat, "no write latencies recorded"
    lat_sorted = sorted(lat)
    p99 = lat_sorted[max(0, int(len(lat_sorted) * 0.99) - 1)]
    med = statistics.median(lat_sorted)
    print(
        f"perf p99={p99:.2f}ms median={med:.2f}ms "
        f"mean={statistics.mean(lat):.2f}ms n={len(lat)} "
        f"(full writer path; tmp_path disk)"
    )
    assert p99 <= _P99_SLO_MS, (
        f"p99 write {p99:.2f}ms > {_P99_SLO_MS}ms (n={len(lat)})"
    )
    await close_runtime(rt)
