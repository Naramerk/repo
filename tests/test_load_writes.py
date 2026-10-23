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

# DESIGN §3.5a absolute SLO — do not raise; no retries/skips/xfail.
_P99_SLO_MS = 50.0
# Predetermined independent full loads; gate is max(round_p99) ≤ 50ms so one
# bad round fails (min/best-of would hide it). Replaces the old outer CI 5×
# pytest loop. Not equivalent: at n=1049, p99 is the 12th-from-end sample
# (int(n*0.99)-1), so a handful of rare fsync outliers are more tolerated
# than with n≈149 where p99 was ~2nd/3rd-worst. Threshold stays 50ms.
_PERF_ROUNDS = 3
_PERF_MIN_SAMPLES = 1000
# RouteReturned count per EXP attempt so one load yields ≥_PERF_MIN_SAMPLES.
_PERF_ROUTES_PER_ATTEMPT = 50


def _p99_ms(samples: list[float]) -> float:
    lat_sorted = sorted(samples)
    return lat_sorted[max(0, int(len(lat_sorted) * 0.99) - 1)]


def _assert_max_round_p99(
    round_p99: list[float],
    *,
    p99_slo: float = _P99_SLO_MS,
) -> float:
    """Every round's p99 must be ≤ slo via max(round_p99). Returns gate p99."""
    assert len(round_p99) == _PERF_ROUNDS, (
        f"need exactly {_PERF_ROUNDS} rounds, got {len(round_p99)}"
    )
    gate_p99 = max(round_p99)
    print(
        f"perf max-of-{len(round_p99)} gate_p99={gate_p99:.2f}ms "
        f"rounds_p99={[round(x, 2) for x in round_p99]} "
        f"(p99≤{p99_slo}ms)"
    )
    assert gate_p99 <= p99_slo, (
        f"max-of-{len(round_p99)} p99 {gate_p99:.2f}ms > {p99_slo}ms "
        f"(rounds p99={round_p99!r})"
    )
    return gate_p99

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
    "question_disposals",
    "disposal_rejects",
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


async def _run_load(
    tmp_path: Path,
    runtime_cls: type,
    *,
    routes_per_attempt: int = 5,
):
    """20 parallel attempts: full writer path (queue → to_thread → fence/insert/fsync)
    plus runtime/executor ``_project_local`` after durable append (real disk ``tmp_path``).

    ``routes_per_attempt`` defaults to 5 (correctness tests). Perf SLO uses
    ``_PERF_ROUTES_PER_ATTEMPT`` so ``latency_samples_ms`` reaches ≥1000.
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
        for _ in range(routes_per_attempt):
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
    """Append AttemptTainted / SourceFetched / FetchIntegrityRaised / HitlOpened /
    GraphNodeUpserted / GraphEdgeAdded(kind=decomposes) /
    QuestionDisposed / QuestionDisposeRejected.

    Exercises ``fetch_calls`` load for SourceFetched, ``fetch_integrity`` write set,
    ``hitl_requests``, dirty-key ``graph_nodes`` / ``graph_edges`` (PR16), and PR15
    ``question_disposals`` / ``disposal_rejects`` so the equivalence test covers
    non-empty sparse tables.
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

    # PR16: GraphNodeUpserted + GraphEdgeAdded(kind=decomposes) must project via
    # #27 dirty-key apply_event (GraphEdgeAdded ∈ _affected_tables → graph_edges).
    q_root = f"{sid}:question:Q"
    q_child = f"{sid}:question:Q.a"
    for nid, ref, text in ((q_root, "Q", "root"), (q_child, "Q.a", "child")):
        await rt.writer.append(
            EventEnvelope(
                study_id=sid,
                type="GraphNodeUpserted",
                entity_key=nid,
                idem_key=make_idem("GraphNodeUpserted", nid, "seed"),
                payload={
                    "node_id": nid,
                    "kind": "question",
                    "study_id": sid,
                    "ref_id": ref,
                    "attrs": {"formulation": text},
                },
                ts=now,
                schema_version=1,
            )
        )
    eid = f"decomposes:{q_child}:{q_root}"
    await rt.writer.append(
        EventEnvelope(
            study_id=sid,
            type="GraphEdgeAdded",
            entity_key=eid,
            idem_key=make_idem("GraphEdgeAdded", eid),
            payload={
                "edge_id": eid,
                "kind": "decomposes",
                "study_id": sid,
                "src_id": q_child,
                "dst_id": q_root,
                "attrs": {"origin": "explicit"},
            },
            ts=now,
            schema_version=1,
        )
    )

    # PR15: QuestionDisposed + QuestionDisposeRejected (dirty-key tables).
    await rt.writer.append(
        EventEnvelope(
            study_id=sid,
            type="QuestionDisposed",
            entity_key="Qseed",
            idem_key=make_idem("QuestionDisposed", sid, "Qseed"),
            payload={
                "question_id": "Qseed",
                "disposition": "infeasible",
                "reason": "seed",
                "refs": ["hitl:h-seed"],
                "design_artifact_id": None,
            },
            ts=now,
            schema_version=1,
        )
    )
    await rt.writer.append(
        EventEnvelope(
            study_id=sid,
            type="QuestionDisposeRejected",
            entity_key="Qrej",
            idem_key=make_idem("QuestionDisposeRejected", sid, "Qrej", "deadbeefdeadbeef"),
            payload={
                "question_id": "Qrej",
                "disposition": "needs_more_data",
                "detail": "missing_experiment_design: seed",
                "reason": "missing_experiment_design",
                "report_line": "не выполнено: needs_more_data без дизайна эксперимента",
                "row_key": "Qrej:deadbeefdeadbeef",
            },
            ts=now,
            schema_version=1,
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

    Also seeds AttemptTainted / SourceFetched / FetchIntegrityRaised / HitlOpened /
    GraphNodeUpserted / GraphEdgeAdded(kind=decomposes) / QuestionDisposed /
    QuestionDisposeRejected so ``source_fetches``, ``fetch_calls``,
    ``fetch_integrity``, ``hitl_requests``, ``graph_nodes``, ``graph_edges``,
    ``question_disposals``, and ``disposal_rejects`` are non-empty.
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
        "graph_nodes",
        "graph_edges",
        "question_disposals",
        "disposal_rejects",
    ):
        assert folded.get(table), f"{table} must be non-empty after seed"
        assert sqlite_map.get(table), f"{table} missing from SQLite projection"
    # Decomposes edge must land in the projected graph_edges set.
    decomp_keys = [
        k
        for k, row in (sqlite_map.get("graph_edges") or {}).items()
        if (row.get("kind") == "decomposes")
    ]
    assert decomp_keys, "GraphEdgeAdded kind=decomposes missing from SQLite projection"
    await close_runtime(rt)


@pytest.mark.perf
@pytest.mark.serial
@pytest.mark.asyncio
async def test_twenty_parallel_attempts_fsync_p99_slo(
    tmp_path: Path, runtime_cls: type
):
    """Absolute DESIGN §3.5a SLO: max-of-N p99 full-path write ≤ 50ms.

    Measures ``StudyWriter.latency_samples_ms`` (queue item → ``to_thread`` →
    fence/insert/JSON/projection-upsert/commit under ``synchronous=FULL``) for
    ``_PERF_ROUNDS`` independent 20-parallel loads on real-disk ``tmp_path``,
    each with ≥``_PERF_MIN_SAMPLES`` writes. Passes iff ``max(round_p99)`` ≤
    50ms (every round must clear the SLO). Replaces the old outer CI 5× loop;
    at n≥1000, p99 is the 12th-from-end sample (more tolerant of rare tail
    outliers than n≈149). No shm, no commit-only timer, no wal/GC knobs, no
    retries/skips/xfail.
    """
    round_p99: list[float] = []
    for r in range(_PERF_ROUNDS):
        round_dir = tmp_path / f"round_{r}"
        round_dir.mkdir()
        rt = await _run_load(
            round_dir,
            runtime_cls,
            routes_per_attempt=_PERF_ROUTES_PER_ATTEMPT,
        )
        lat = rt.writer.latency_samples_ms()
        assert lat, "no write latencies recorded"
        assert len(lat) >= _PERF_MIN_SAMPLES, (
            f"need ≥{_PERF_MIN_SAMPLES} samples for stable p99, got n={len(lat)}"
        )
        p99 = _p99_ms(lat)
        round_p99.append(p99)
        print(
            f"perf round {r + 1}/{_PERF_ROUNDS} p99={p99:.2f}ms "
            f"median={statistics.median(lat):.2f}ms "
            f"mean={statistics.mean(lat):.2f}ms "
            f"n={len(lat)} (full writer path; tmp_path disk)"
        )
        await close_runtime(rt)
    _assert_max_round_p99(round_p99)


def test_p99_gate_max_of_rounds_trips_on_one_bad_round():
    """max-of-3 must fail when one round has ~60ms p99 (best-of would hide it).

    Builds three round p99s via ``_p99_ms`` (same path as the perf job) and
    asserts through ``_assert_max_round_p99``. Two clean rounds + one bad
    (≈60ms-on-write tail) must trip the gate; three clean rounds must pass.
    """
    n = _PERF_MIN_SAMPLES
    clean = [1.0] * n
    # ~60ms on enough samples that p99 is the slow tail (not a single spike).
    bad = [1.0] * (n - 15) + [60.0] * 15
    round_p99 = [_p99_ms(clean), _p99_ms(bad), _p99_ms(clean)]
    assert round_p99[0] <= _P99_SLO_MS
    assert round_p99[1] > _P99_SLO_MS
    assert round_p99[2] <= _P99_SLO_MS
    # One bad round of three → max fails (min/best-of would pass on the cleans).
    with pytest.raises(AssertionError, match=r"max-of-3 p99 .*> 50\.0ms"):
        _assert_max_round_p99(round_p99)
    # Uniform ~60ms every round also fails.
    with pytest.raises(AssertionError, match=r"max-of-3 p99 .*> 50\.0ms"):
        _assert_max_round_p99([_p99_ms(bad)] * _PERF_ROUNDS)
    # Three clean rounds pass (noise like 13ms still under 50).
    assert _assert_max_round_p99([13.53, 1.27, 1.19]) == 13.53


@pytest.mark.serial
@pytest.mark.asyncio
async def test_p99_gate_trips_on_slow_append_many_real_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """~60ms sleep in real ``SqliteEventStore.append_many`` trips max-of-3.

    One measured round (default API runtime — cheap) + two clean synthetic
    rounds through ``_assert_max_round_p99``.
    """
    from coscientist.core.store import SqliteEventStore

    real_append_many = SqliteEventStore.append_many

    async def _slow_append_many(self: Any, *args: Any, **kwargs: Any) -> Any:
        await asyncio.sleep(0.060)
        return await real_append_many(self, *args, **kwargs)

    monkeypatch.setattr(SqliteEventStore, "append_many", _slow_append_many)

    clock = SystemClock()
    # helpers.open_runtime defaults to api.StudyRuntime (create_study mixin).
    rt = await open_runtime(tmp_path, "st_delay", clock=clock)
    await rt.create_study(
        profile="research",
        request="delay-gate",
        catalog_version="0.1.0",
        deadline=clock.now() + timedelta(hours=1),
        sid="st_delay",
    )
    for i in range(20):
        await rt.writer.append(
            EventEnvelope(
                study_id="st_delay",
                type="RouteReturned",
                entity_key=f"st_delay/EXP-delay:{i}",
                idem_key=f"RouteReturned:delay:{i}",
                payload={
                    "attempt_id": f"st_delay/EXP-delay:{i}",
                    "agent_output_digest": f"d{i}",
                },
                ts=clock.now(),
            )
        )
    lat = rt.writer.latency_samples_ms()
    await close_runtime(rt)
    assert lat, "no write latencies recorded"
    bad_p99 = _p99_ms(lat)
    print(f"delay-real-path p99={bad_p99:.2f}ms n={len(lat)}")
    assert bad_p99 > _P99_SLO_MS, (
        f"injected 60ms delay should exceed SLO (p99={bad_p99:.2f})"
    )
    with pytest.raises(AssertionError, match=r"max-of-3 p99 .*> 50\.0ms"):
        _assert_max_round_p99([1.0, bad_p99, 1.0])
