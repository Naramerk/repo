"""PR 4: intake Accept/Refuse before SPEC."""

from __future__ import annotations

import json
import time
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import jsonschema
import pytest

from coscientist.core.claims.reasons import RefusalKind
from coscientist.core.execution.domain import Budget
from coscientist.core.finalize.finalize_nodes import conclusion_id_for_study
from coscientist.core.finalize.study_view import study_phase, study_view
from coscientist.core.plan.intake import REASON_MAX, IntakeAccept, IntakeRefuse, intake
from tests.helpers import attach_executor, close_runtime, open_runtime


@pytest.mark.asyncio
async def test_intake_empty_request():
    r = await intake("", [], "research", None)
    assert isinstance(r, IntakeRefuse)
    assert r.kind == RefusalKind.EMPTY_REQUEST


@pytest.mark.asyncio
async def test_intake_budget_zero():
    r = await intake("q", [], "research", Budget(max_cost_rub=Decimal("0")))
    assert isinstance(r, IntakeRefuse)
    assert r.kind == RefusalKind.BUDGET_ZERO


@pytest.mark.asyncio
async def test_intake_below_min_plan_cost():
    r = await intake(
        "q",
        [],
        "research",
        Budget(max_cost_rub=Decimal("5")),
        min_plan_cost=Decimal("10"),
    )
    assert isinstance(r, IntakeRefuse) and r.kind == RefusalKind.BUDGET_ZERO


@pytest.mark.asyncio
async def test_intake_duration_too_short():
    r = await intake(
        "q",
        [],
        "research",
        None,
        duration_s=30,
        finalize_reserve_s=30,
    )
    assert isinstance(r, IntakeRefuse)
    assert r.kind == RefusalKind.DURATION_TOO_SHORT


@pytest.mark.asyncio
async def test_intake_accepts_ok():
    assert isinstance(
        await intake(
            "do science", [], "research", Budget(max_cost_rub=Decimal("100"))
        ),
        IntakeAccept,
    )


@pytest.mark.asyncio
async def test_intake_safety_port_refuses():
    class Port:
        def classify(self, request, inputs):
            return RefusalKind.OUT_OF_SCOPE

    r = await intake("q", [], "research", None, safety=Port())
    assert isinstance(r, IntakeRefuse) and r.kind == RefusalKind.OUT_OF_SCOPE
    assert r.classifier_error is False


@pytest.mark.asyncio
async def test_intake_safety_port_timeout():
    class Port:
        def classify(self, request, inputs):
            raise TimeoutError

    r = await intake("q", [], "research", None, safety=Port(), safety_timeout_s=0.5)
    assert isinstance(r, IntakeRefuse) and r.kind == RefusalKind.SAFETY
    assert r.classifier_error is True


@pytest.mark.asyncio
async def test_intake_safety_port_hung_enforces_timeout():
    """Hung sync classify must not block past safety_timeout_s (event-loop safe)."""
    import threading

    started = threading.Event()
    release = threading.Event()

    class Port:
        def classify(self, request, inputs):
            started.set()
            # Hang far longer than safety_timeout_s; release cleans up the thread.
            release.wait(timeout=60.0)
            return None

    t0 = time.monotonic()
    r = await intake("q", [], "research", None, safety=Port(), safety_timeout_s=0.2)
    elapsed = time.monotonic() - t0
    assert elapsed < 1.0, f"timeout waited too long: {elapsed:.2f}s"
    assert isinstance(r, IntakeRefuse)
    assert r.kind == RefusalKind.SAFETY
    assert r.classifier_error is True
    assert "timeout" in r.reason
    assert started.wait(1.0)
    release.set()


@pytest.mark.asyncio
async def test_intake_safety_port_error_sets_classifier_error():
    class Port:
        def classify(self, request, inputs):
            raise RuntimeError("boom")

    r = await intake("q", [], "research", None, safety=Port())
    assert isinstance(r, IntakeRefuse)
    assert r.kind == RefusalKind.SAFETY
    assert r.classifier_error is True
    assert "RuntimeError" in r.reason


@pytest.mark.asyncio
async def test_intake_classifier_reason_truncated():
    class Port:
        def classify(self, request, inputs):
            raise RuntimeError("x" * 500)

    r = await intake("q", [], "research", None, safety=Port())
    assert isinstance(r, IntakeRefuse)
    assert len(r.reason) <= REASON_MAX


def test_study_refused_schema_valid():
    schema = json.loads(
        (
            Path(__file__).resolve().parents[1]
            / "coscientist/schema/study_refused.schema.json"
        ).read_text()
    )
    payload = {
        "kind": "empty_request",
        "reason": "empty request",
        "classifier_error": False,
        "catalog_version": 3,
    }
    jsonschema.validate(payload, schema)
    jsonschema.validate({**payload, "catalog_version": None}, schema)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(
            {
                "kind": "empty_request",
                "reason": "empty request",
                "classifier_error": False,
            },
            schema,
        )
    assert "duration_too_short" in schema["properties"]["kind"]["enum"]
    assert "catalog_version" in schema["required"]


def test_study_refused_v1_upcast_sets_catalog_version_null():
    """v1 had no catalog_version field — upcast records that fact as null."""
    from coscientist.core.events.upcasters import upcast

    version, payload = upcast(
        "StudyRefused",
        1,
        {
            "kind": "empty_request",
            "reason": "empty request",
            "classifier_error": False,
        },
    )
    assert version == 2
    assert "catalog_version" in payload
    assert payload["catalog_version"] is None


@pytest.mark.asyncio
async def test_real_replay_study_refused_v1_gets_null_catalog_version(tmp_path, clock):
    """Legacy StudyRefused v1 journals upcast to catalog_version=null on open."""
    import sqlite3

    from coscientist.core.events.digest import canonical_json, sha256_hex
    from coscientist.core.events.events import EventEnvelope, make_idem

    sid = "st_refused_v1"
    rt = await open_runtime(tmp_path, sid, clock=clock, owner="t1")
    await rt.create_study(
        profile="research",
        request="",
        catalog_version=3,
        deadline=clock.now() + timedelta(seconds=60),
        sid=sid,
        finalize_reserve_s=5,
    )
    await close_runtime(rt)

    db = tmp_path / sid / "events.db"
    conn = sqlite3.connect(db)
    try:
        row = conn.execute(
            "SELECT seq, payload FROM events WHERE type='StudyRefused'"
        ).fetchone()
        assert row is not None
        seq, payload_raw = row
        payload = json.loads(payload_raw)
        payload.pop("catalog_version", None)
        new_raw = canonical_json(payload)
        conn.execute(
            "UPDATE events SET schema_version=1, payload=?, payload_digest=? WHERE seq=?",
            (new_raw, sha256_hex(new_raw), seq),
        )
        conn.commit()
    finally:
        conn.close()

    rt2 = await open_runtime(tmp_path, sid, clock=clock, owner="t2")
    try:
        events = await rt2.store.read_all()
        refused = [e for e in events if e.type == "StudyRefused"]
        assert len(refused) == 1
        assert refused[0].schema_version == 2
        assert refused[0].payload.get("catalog_version") is None
    finally:
        await close_runtime(rt2)


@pytest.mark.asyncio
async def test_real_replay_study_refused_v2_missing_catalog_version_refused(
    tmp_path, clock
):
    """Native v2 StudyRefused without catalog_version key → CatalogError on fold."""
    import sqlite3

    from coscientist.core.events.digest import canonical_json, sha256_hex
    from coscientist.core.events.errors import CatalogError

    sid = "st_refused_v2_missing"
    rt = await open_runtime(tmp_path, sid, clock=clock, owner="t1")
    await rt.create_study(
        profile="research",
        request="",
        catalog_version=3,
        deadline=clock.now() + timedelta(seconds=60),
        sid=sid,
        finalize_reserve_s=5,
    )
    await close_runtime(rt)

    db = tmp_path / sid / "events.db"
    conn = sqlite3.connect(db)
    try:
        row = conn.execute(
            "SELECT seq, payload FROM events WHERE type='StudyRefused'"
        ).fetchone()
        assert row is not None
        seq, payload_raw = row
        payload = json.loads(payload_raw)
        payload.pop("catalog_version", None)
        new_raw = canonical_json(payload)
        conn.execute(
            "UPDATE events SET schema_version=2, payload=?, payload_digest=? WHERE seq=?",
            (new_raw, sha256_hex(new_raw), seq),
        )
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(CatalogError, match="StudyRefused.v2:catalog_version_required"):
        await open_runtime(tmp_path, sid, clock=clock, owner="t2")


@pytest.mark.asyncio
async def test_intake_refuses_before_spec(tmp_path, clock):
    rt = await open_runtime(tmp_path, "st_in", clock=clock, owner="t1")
    _, arts = attach_executor(rt, tmp_path)
    rt.artifacts = arts
    await rt.create_study(
        profile="research",
        request="",
        catalog_version=3,
        deadline=clock.now() + timedelta(seconds=60),
        sid="st_in",
        finalize_reserve_s=5,
    )
    assert rt._finalized
    assert not rt.tasks
    events = await rt.store.read_all()
    refused = [e for e in events if e.type == "StudyRefused"]
    assert len(refused) == 1
    assert refused[0].payload["kind"] == "empty_request"
    schema = json.loads(
        (
            Path(__file__).resolve().parents[1]
            / "coscientist/schema/study_refused.schema.json"
        ).read_text()
    )
    jsonschema.validate(refused[0].payload, schema)
    fins = [e for e in events if e.type == "StudyFinalized"]
    assert len(fins) == 1
    assert fins[0].payload["trigger"] == "intake"
    assert fins[0].payload["terminal_reason"] == "refused"
    refusal = str(fins[0].payload.get("refusal") or "")
    assert "empty_request" in refusal
    art_id = fins[0].payload.get("report_artifact_id")
    assert art_id
    md = arts.read(art_id).decode("utf-8")
    assert "empty_request" in md or "empty request" in md
    assert "refusal" in md
    scid = conclusion_id_for_study("st_in")
    node = rt.graph.get_node(scid)
    assert node is not None
    synth = str((node.attrs or {}).get("synthesis") or "")
    assert "refused" in synth and "empty_request" in synth
    assert rt.view().phase == "closed"
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_budget_zero_refused(tmp_path, clock):
    rt = await open_runtime(tmp_path, "st_b0", clock=clock, owner="t1")
    await rt.create_study(
        profile="research",
        request="q",
        catalog_version=3,
        deadline=clock.now() + timedelta(seconds=60),
        sid="st_b0",
        finalize_reserve_s=5,
        budget=Budget(max_cost_rub=Decimal("0")),
    )
    assert rt._finalized and not rt.tasks
    fins = [e for e in await rt.store.read_all() if e.type == "StudyFinalized"]
    assert fins and fins[0].payload["trigger"] == "intake"
    assert fins[0].payload["terminal_reason"] == "refused"
    assert "budget_zero" in str(fins[0].payload.get("refusal") or "")
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_duration_le_reserve_refused(tmp_path, clock):
    rt = await open_runtime(tmp_path, "st_dur", clock=clock, owner="t1")
    await rt.create_study(
        profile="research",
        request="q",
        catalog_version=3,
        deadline=clock.now() + timedelta(seconds=30),
        sid="st_dur",
        finalize_reserve_s=30,
    )
    assert rt._finalized and not rt.tasks
    events = await rt.store.read_all()
    refused = [e for e in events if e.type == "StudyRefused"]
    assert refused and refused[0].payload["kind"] == "duration_too_short"
    fins = [e for e in events if e.type == "StudyFinalized"]
    assert fins[0].payload["trigger"] == "intake"
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_intake_refuse_survives_crash(tmp_path, clock, runtime_cls, monkeypatch):
    """create_study writes StudyRefused then finalize crashes → reopen → intake."""
    sid = "st_crash_ref"

    async def boom(*_a, **_k):
        raise RuntimeError("inject:finalize_crash")

    monkeypatch.setattr("coscientist.core.finalize.finalize.finalize", boom)

    rt = await open_runtime(
        tmp_path, sid, clock=clock, owner="t1", runtime_cls=runtime_cls
    )
    with pytest.raises(RuntimeError, match="inject:finalize_crash"):
        await rt.create_study(
            profile="research",
            request="",
            catalog_version=3,
            deadline=clock.now() + timedelta(seconds=60),
            sid=sid,
            finalize_reserve_s=5,
        )
    types = {e.type for e in await rt.store.read_all()}
    assert "StudyCreated" in types and "StudyRefused" in types
    assert "StudyFinalized" not in types
    assert rt._intake_refusal is not None
    await close_runtime(rt)

    clock.advance(timedelta(seconds=120))  # past ed — intake must still win
    rt2 = await open_runtime(
        tmp_path, sid, clock=clock, owner="t2", runtime_cls=runtime_cls
    )
    assert rt2._intake_refusal is not None
    assert rt2._intake_refusal.kind == RefusalKind.EMPTY_REQUEST
    assert not rt2._finalized
    await rt2.tick()
    fin = [e for e in await rt2.store.read_all() if e.type == "StudyFinalized"]
    assert len(fin) == 1
    assert fin[0].payload["trigger"] == "intake"
    assert fin[0].payload["terminal_reason"] == "refused"
    assert "empty_request" in str(fin[0].payload.get("refusal") or "")
    await close_runtime(rt2)


def test_empty_study_past_ed_is_finalizing():
    """No tasks + past effective_deadline → finalizing (core == refmodel)."""
    import refmodel

    assert study_phase(past_effective=True) == "finalizing"
    assert refmodel.study_phase(past_effective=True) == "finalizing"
    from datetime import UTC, datetime

    now = datetime(2026, 1, 2, tzinfo=UTC)
    ed = datetime(2026, 1, 1, tzinfo=UTC)
    assert study_view([], now=now, effective_deadline=ed).phase == "finalizing"
