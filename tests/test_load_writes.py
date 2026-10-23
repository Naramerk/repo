"""Load: 20 parallel attempts in one study, fsync on (DESIGN §3.5a)."""

from __future__ import annotations

import asyncio
import json
import random
import statistics
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from coscientist.core.clock import SystemClock
from coscientist.core.events import EventEnvelope, make_idem
from coscientist.core.projections import _row_key, fold_events
from coscientist.core.source_fetch import verify_fetch_capability
from coscientist.tools.effects import _mint_fetch_capability_for_tests
from tests.helpers import attach_executor, close_runtime, exp_spec, open_runtime, record_success

# DESIGN §3.5a absolute SLO — do not raise; no retries/skips.
_P99_SLO_MS = 50.0

# Tables ``fold_events`` / ``apply_event`` may populate (compare SQLite ↔ fold).
_PROJECTION_TABLES = (
    "studies",
    "plans",
    "tasks",
    "attempts",
    "results",
    "artifacts",
    "effects",
    "source_fetches",
    "fetch_calls",
    "fetch_integrity",
    "requirements",
    "hitl_requests",
    "graph_nodes",
    "graph_edges",
    "meta",
)


def _canon_row(row: dict[str, Any]) -> str:
    """Strict JSON canon — no ``default=str``; type mismatches must fail."""
    return json.dumps(row, sort_keys=True, allow_nan=False)


async def _sqlite_projection_map(store: Any) -> dict[str, dict[str, dict[str, Any]]]:
    out: dict[str, dict[str, dict[str, Any]]] = {}
    for table in _PROJECTION_TABLES:
        rows = await store.get_projection(table)
        for row in rows:
            if not isinstance(row, dict):
                continue
            key = _row_key(table, row)
            if key:
                out.setdefault(table, {})[key] = row
    return out


def _assert_projection_equiv(
    sqlite_map: dict[str, dict[str, dict[str, Any]]],
    folded: dict[str, dict[str, dict[str, Any]]],
) -> None:
    """Every table: same keys and same canonical row payloads."""
    tables = sorted(set(sqlite_map) | set(folded) | set(_PROJECTION_TABLES))
    for table in tables:
        left = sqlite_map.get(table) or {}
        right = folded.get(table) or {}
        assert set(left) == set(right), (
            f"{table}: key mismatch sqlite={sorted(left)} fold={sorted(right)}"
        )
        for key in sorted(left):
            assert _canon_row(left[key]) == _canon_row(right[key]), (
                f"{table}[{key}] sqlite!=fold"
            )


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


async def _seed_fetch_hitl_projection_events(rt: Any) -> None:
    """Append AttemptTainted / SourceFetched / FetchIntegrityRaised / HitlOpened.

    Exercises ``fetch_calls`` load for SourceFetched, ``fetch_integrity`` write set,
    and ``hitl_requests`` so the equivalence test covers non-empty sparse tables.
    Uses a synthetic attempt id (no ``start_attempt``) so seeding works after the
    parallel load has already closed EXP tasks.
    """
    sid = "st_load"
    now = rt.clock.now()
    aid = "st_load/EXP-seed:1"
    call_id = f"{aid}:fetch1"
    tool = "papers_get"
    digest = "a" * 64

    await rt.writer.append(
        EventEnvelope(
            study_id=sid,
            type="AttemptTainted",
            entity_key=aid,
            idem_key=make_idem("AttemptTainted", aid, call_id),
            payload={
                "attempt_id": aid,
                "reason": "source_fetch",
                "call_id": call_id,
                "tool": tool,
                "is_fetch": True,
            },
            ts=now,
            schema_version=1,
        )
    )

    cap = _mint_fetch_capability_for_tests(
        tool=tool, call_id=call_id, study_id=sid, attempt_id=aid
    )
    verify_fetch_capability(
        cap,
        tool=tool,
        call_id=call_id,
        study_id=sid,
        attempt_id=aid,
    )
    await rt.writer.append(
        EventEnvelope(
            study_id=sid,
            type="SourceFetched",
            entity_key=digest,
            idem_key=make_idem("SourceFetched", digest),
            payload={
                "call_id": call_id,
                "tool": tool,
                "digest": digest,
                "url": "https://example.org/p",
                "attempt_id": aid,
            },
            ts=now,
            schema_version=1,
        ),
        _source_fetched_cap_id=cap.id,
    )

    await rt.writer.append(
        EventEnvelope(
            study_id=sid,
            type="FetchIntegrityRaised",
            entity_key="integrity-seed",
            idem_key=make_idem("FetchIntegrityRaised", "integrity-seed"),
            payload={
                "kind": "fetch_tool_reclassified",
                "tool": "legacy_fetch",
                "call_id": "integrity-seed",
                "internal_error": True,
            },
            ts=now,
            schema_version=1,
        )
    )

    rid = "hitl-seed-1"
    await rt.writer.append(
        EventEnvelope(
            study_id=sid,
            type="HitlOpened",
            entity_key=rid,
            idem_key=make_idem("HitlOpened", rid),
            payload={
                "request_id": rid,
                "kind": "plan_approval",
                "task_id": f"{sid}/PLAN",
                "open": True,
                "cost_rub": 0.0,
            },
            ts=now,
        )
    )


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


@pytest.mark.slow
@pytest.mark.serial
@pytest.mark.asyncio
async def test_twenty_parallel_sqlite_projection_matches_fold(
    tmp_path: Path, runtime_cls: type
):
    """After the full 20-parallel load, SQLite projections == fold_events (every table).

    Also seeds AttemptTainted / SourceFetched / FetchIntegrityRaised / HitlOpened so
    ``source_fetches``, ``fetch_calls``, ``fetch_integrity``, and ``hitl_requests``
    are non-empty (covers dirty-key load deps + #26 integrity rows).
    """
    rt = await _run_load(tmp_path, runtime_cls)
    await _seed_fetch_hitl_projection_events(rt)
    events = await rt.store.read_all()
    folded = fold_events(events)
    sqlite_map = await _sqlite_projection_map(rt.store)
    _assert_projection_equiv(sqlite_map, folded)
    for table in (
        "source_fetches",
        "fetch_calls",
        "fetch_integrity",
        "hitl_requests",
    ):
        assert folded.get(table), f"{table} must be non-empty after seed"
        assert sqlite_map.get(table), f"{table} missing from SQLite projection"
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
