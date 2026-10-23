"""Load: 20 parallel attempts in one study, fsync on (DESIGN §3.5a)."""

from __future__ import annotations

import asyncio
import json
import os
import random
import statistics
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any, Mapping

import pytest

from coscientist.core.clock import SystemClock
from coscientist.core.events import EventEnvelope, make_idem
from coscientist.core.projections import _row_key, fold_events
from coscientist.core.source_fetch import verify_fetch_capability
from coscientist.tools.effects import _mint_fetch_capability_for_tests
from tests.helpers import attach_executor, close_runtime, exp_spec, open_runtime, record_success

# DESIGN §3.5a absolute SLO defaults — do not loosen. Override via env
# (COSCI_PERF_P99_MS / COSCI_PERF_ROUNDS / COSCI_PERF_SAMPLES) or disable with
# COSCI_PERF_GATE=off (skips the SLO test; never a silent pass).
_DEFAULT_P99_MS = 50.0
_DEFAULT_ROUNDS = 3
_DEFAULT_SAMPLES = 1000
_MIN_P99_MS = 1e-6
_MIN_ROUNDS = 1
_MIN_SAMPLES = 1
_GATE_OFF = frozenset({"off", "0", "false", "no"})
_GATE_ON = frozenset({"on", "1", "true", "yes"})

# Aliases for demos / call sites that pin the DESIGN defaults.
_P99_SLO_MS = _DEFAULT_P99_MS
_PERF_ROUNDS = _DEFAULT_ROUNDS
_PERF_MIN_SAMPLES = _DEFAULT_SAMPLES


@dataclass(frozen=True)
class PerfGateConfig:
    """Journal-write p99 gate: max-of-N rounds, each with ≥samples writes."""

    enabled: bool
    p99_ms: float
    rounds: int
    samples: int

    @property
    def routes_per_attempt(self) -> int:
        """RouteReturned count per EXP so one load yields ≥``samples`` writes."""
        return max(1, (self.samples + 19) // 20)


def _parse_positive_float(raw: str | None, *, default: float, name: str) -> float:
    if raw is None or raw.strip() == "":
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a positive number, got {raw!r}") from exc
    if value < _MIN_P99_MS:
        raise ValueError(f"{name} must be >= {_MIN_P99_MS}, got {value!r}")
    return value


def _parse_positive_int(raw: str | None, *, default: int, name: str, minimum: int) -> int:
    if raw is None or raw.strip() == "":
        return default
    try:
        value = int(raw, 10)
    except ValueError as exc:
        raise ValueError(f"{name} must be a positive integer, got {raw!r}") from exc
    if value < minimum:
        raise ValueError(f"{name} must be >= {minimum}, got {value!r}")
    return value


def resolve_perf_gate_config(
    environ: Mapping[str, str] | None = None,
) -> PerfGateConfig:
    """Load gate settings from env; invalid values raise ``ValueError``."""
    env = os.environ if environ is None else environ
    raw_gate = env.get("COSCI_PERF_GATE")
    if raw_gate is None:
        enabled = True
    else:
        key = raw_gate.strip().lower()
        if key in _GATE_OFF:
            enabled = False
        elif key in _GATE_ON:
            enabled = True
        else:
            raise ValueError(
                f"COSCI_PERF_GATE must be on/off (or true/false/1/0), got {raw_gate!r}"
            )
    return PerfGateConfig(
        enabled=enabled,
        p99_ms=_parse_positive_float(
            env.get("COSCI_PERF_P99_MS"),
            default=_DEFAULT_P99_MS,
            name="COSCI_PERF_P99_MS",
        ),
        rounds=_parse_positive_int(
            env.get("COSCI_PERF_ROUNDS"),
            default=_DEFAULT_ROUNDS,
            name="COSCI_PERF_ROUNDS",
            minimum=_MIN_ROUNDS,
        ),
        samples=_parse_positive_int(
            env.get("COSCI_PERF_SAMPLES"),
            default=_DEFAULT_SAMPLES,
            name="COSCI_PERF_SAMPLES",
            minimum=_MIN_SAMPLES,
        ),
    )


def _skip_if_perf_gate_disabled(cfg: PerfGateConfig) -> None:
    """Skip the SLO test when the gate is off — never a silent pass."""
    if not cfg.enabled:
        pytest.skip(
            "journal-write perf gate disabled via COSCI_PERF_GATE=off "
            f"(defaults: p99≤{_DEFAULT_P99_MS}ms, {_DEFAULT_ROUNDS} rounds, "
            f"≥{_DEFAULT_SAMPLES} samples/round)"
        )


def _p99_ms(samples: list[float]) -> float:
    lat_sorted = sorted(samples)
    return lat_sorted[max(0, int(len(lat_sorted) * 0.99) - 1)]


def _assert_max_round_p99(
    round_p99: list[float],
    *,
    p99_slo: float = _DEFAULT_P99_MS,
    rounds: int = _DEFAULT_ROUNDS,
) -> float:
    """Every round's p99 must be ≤ slo via max(round_p99). Returns gate p99."""
    assert len(round_p99) == rounds, (
        f"need exactly {rounds} rounds, got {len(round_p99)}"
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
    ``PerfGateConfig.routes_per_attempt`` so ``latency_samples_ms`` reaches
    the configured sample floor.
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
    """Absolute DESIGN §3.5a SLO: max-of-N p99 full-path write ≤ threshold.

    Defaults: p99 ≤ 50ms, 3 rounds, ≥1000 samples/round. Configurable via
    ``COSCI_PERF_*``; ``COSCI_PERF_GATE=off`` skips with a clear reason.
    Measures ``StudyWriter.latency_samples_ms`` (queue → ``to_thread`` →
    fence/insert/JSON/projection-upsert/commit under ``synchronous=FULL``).
    Passes iff ``max(round_p99)`` ≤ threshold. Goal: catch a steady slowdown.
    """
    cfg = resolve_perf_gate_config()
    _skip_if_perf_gate_disabled(cfg)
    round_p99: list[float] = []
    for r in range(cfg.rounds):
        round_dir = tmp_path / f"round_{r}"
        round_dir.mkdir()
        rt = await _run_load(
            round_dir,
            runtime_cls,
            routes_per_attempt=cfg.routes_per_attempt,
        )
        lat = rt.writer.latency_samples_ms()
        assert lat, "no write latencies recorded"
        assert len(lat) >= cfg.samples, (
            f"need ≥{cfg.samples} samples for p99 gate, got n={len(lat)}"
        )
        p99 = _p99_ms(lat)
        round_p99.append(p99)
        print(
            f"perf round {r + 1}/{cfg.rounds} p99={p99:.2f}ms "
            f"median={statistics.median(lat):.2f}ms "
            f"mean={statistics.mean(lat):.2f}ms "
            f"n={len(lat)} (full writer path; tmp_path disk)"
        )
        await close_runtime(rt)
    _assert_max_round_p99(round_p99, p99_slo=cfg.p99_ms, rounds=cfg.rounds)


def test_p99_gate_max_of_rounds_trips_on_one_bad_round():
    """max-of-3 must fail when one round has ~60ms p99 (best-of would hide it).

    Builds three round p99s via ``_p99_ms`` (same path as the perf job) and
    asserts through ``_assert_max_round_p99``. Two clean rounds + one bad
    (≈60ms-on-write tail) must trip the gate; three clean rounds must pass.
    """
    n = _DEFAULT_SAMPLES
    clean = [1.0] * n
    # ~60ms on enough samples that p99 is the slow tail (not a single spike).
    bad = [1.0] * (n - 15) + [60.0] * 15
    round_p99 = [_p99_ms(clean), _p99_ms(bad), _p99_ms(clean)]
    assert round_p99[0] <= _DEFAULT_P99_MS
    assert round_p99[1] > _DEFAULT_P99_MS
    assert round_p99[2] <= _DEFAULT_P99_MS
    # One bad round of three → max fails (min/best-of would pass on the cleans).
    with pytest.raises(AssertionError, match=r"max-of-3 p99 .*> 50\.0ms"):
        _assert_max_round_p99(round_p99)
    # Uniform ~60ms every round also fails.
    with pytest.raises(AssertionError, match=r"max-of-3 p99 .*> 50\.0ms"):
        _assert_max_round_p99([_p99_ms(bad)] * _DEFAULT_ROUNDS)
    # Three clean rounds pass (noise like 13ms still under 50).
    assert _assert_max_round_p99([13.53, 1.27, 1.19]) == 13.53


@pytest.mark.serial
@pytest.mark.asyncio
async def test_p99_gate_trips_on_slow_append_many_real_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """~60ms sleep in real ``SqliteEventStore.append_many`` trips max-of-3.

    Three real rounds through the append_many path (default API runtime),
    each with a slow append; gate sees three measured round p99s.
    """
    from coscientist.core.store import SqliteEventStore

    real_append_many = SqliteEventStore.append_many

    async def _slow_append_many(self: Any, *args: Any, **kwargs: Any) -> Any:
        await asyncio.sleep(0.060)
        return await real_append_many(self, *args, **kwargs)

    monkeypatch.setattr(SqliteEventStore, "append_many", _slow_append_many)

    round_p99: list[float] = []
    for r in range(_DEFAULT_ROUNDS):
        round_dir = tmp_path / f"round_{r}"
        round_dir.mkdir()
        clock = SystemClock()
        sid = f"st_delay_{r}"
        # helpers.open_runtime defaults to api.StudyRuntime (create_study mixin).
        rt = await open_runtime(round_dir, sid, clock=clock)
        await rt.create_study(
            profile="research",
            request="delay-gate",
            catalog_version="0.1.0",
            deadline=clock.now() + timedelta(hours=1),
            sid=sid,
        )
        for i in range(20):
            await rt.writer.append(
                EventEnvelope(
                    study_id=sid,
                    type="RouteReturned",
                    entity_key=f"{sid}/EXP-delay:{i}",
                    idem_key=f"RouteReturned:delay:{r}:{i}",
                    payload={
                        "attempt_id": f"{sid}/EXP-delay:{i}",
                        "agent_output_digest": f"d{i}",
                    },
                    ts=clock.now(),
                )
            )
        lat = rt.writer.latency_samples_ms()
        await close_runtime(rt)
        assert lat, "no write latencies recorded"
        bad_p99 = _p99_ms(lat)
        print(f"delay-real-path round {r + 1}/{_DEFAULT_ROUNDS} p99={bad_p99:.2f}ms n={len(lat)}")
        assert bad_p99 > _DEFAULT_P99_MS, (
            f"injected 60ms delay should exceed SLO (p99={bad_p99:.2f})"
        )
        round_p99.append(bad_p99)
    assert len(round_p99) == _DEFAULT_ROUNDS
    with pytest.raises(AssertionError, match=r"max-of-3 p99 .*> 50\.0ms"):
        _assert_max_round_p99(round_p99)


def test_perf_gate_config_defaults():
    """Unset env → gate on with DESIGN defaults (do not loosen)."""
    cfg = resolve_perf_gate_config(environ={})
    assert cfg.enabled is True
    assert cfg.p99_ms == _DEFAULT_P99_MS
    assert cfg.rounds == _DEFAULT_ROUNDS
    assert cfg.samples == _DEFAULT_SAMPLES
    assert cfg.routes_per_attempt == 50


def test_perf_gate_config_env_override():
    """COSCI_PERF_* env overrides threshold, rounds, and samples."""
    cfg = resolve_perf_gate_config(
        environ={
            "COSCI_PERF_GATE": "on",
            "COSCI_PERF_P99_MS": "25.5",
            "COSCI_PERF_ROUNDS": "2",
            "COSCI_PERF_SAMPLES": "200",
        }
    )
    assert cfg.enabled is True
    assert cfg.p99_ms == 25.5
    assert cfg.rounds == 2
    assert cfg.samples == 200
    assert cfg.routes_per_attempt == 10


def test_perf_gate_off_skips_with_reason():
    """COSCI_PERF_GATE=off disables the SLO via skip, not a silent pass."""
    cfg = resolve_perf_gate_config(environ={"COSCI_PERF_GATE": "off"})
    assert cfg.enabled is False
    assert cfg.p99_ms == _DEFAULT_P99_MS
    assert cfg.rounds == _DEFAULT_ROUNDS
    assert cfg.samples == _DEFAULT_SAMPLES
    with pytest.raises(pytest.skip.Exception, match=r"perf gate disabled"):
        _skip_if_perf_gate_disabled(cfg)


@pytest.mark.parametrize(
    ("environ", "match"),
    [
        ({"COSCI_PERF_GATE": "maybe"}, r"COSCI_PERF_GATE"),
        ({"COSCI_PERF_P99_MS": "0"}, r"COSCI_PERF_P99_MS"),
        ({"COSCI_PERF_P99_MS": "-1"}, r"COSCI_PERF_P99_MS"),
        ({"COSCI_PERF_P99_MS": "nope"}, r"COSCI_PERF_P99_MS"),
        ({"COSCI_PERF_ROUNDS": "0"}, r"COSCI_PERF_ROUNDS"),
        ({"COSCI_PERF_ROUNDS": "-2"}, r"COSCI_PERF_ROUNDS"),
        ({"COSCI_PERF_ROUNDS": "1.5"}, r"COSCI_PERF_ROUNDS"),
        ({"COSCI_PERF_SAMPLES": "0"}, r"COSCI_PERF_SAMPLES"),
        ({"COSCI_PERF_SAMPLES": "abc"}, r"COSCI_PERF_SAMPLES"),
    ],
)
def test_perf_gate_invalid_values_fail_loudly(
    environ: dict[str, str], match: str
):
    """Invalid COSCI_PERF_* values raise ValueError (never soft-default)."""
    with pytest.raises(ValueError, match=match):
        resolve_perf_gate_config(environ=environ)
