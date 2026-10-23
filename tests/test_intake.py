"""PR 4: intake Accept/Refuse before SPEC."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest

from coscientist.core.domain import Budget
from coscientist.core.events import EventEnvelope, make_idem
from coscientist.core.finalize_nodes import conclusion_id_for_study
from coscientist.core.intake import IntakeAccept, IntakeRefuse, intake
from coscientist.core.reasons import RefusalKind
from coscientist.core.runtime_factory import close_runtime, open_runtime
from coscientist.core.study_view import study_phase, study_view
from tests.helpers import attach_executor


def test_intake_empty_request():
    r = intake("", [], "research", None)
    assert isinstance(r, IntakeRefuse)
    assert r.kind == RefusalKind.EMPTY_REQUEST


def test_intake_budget_zero():
    r = intake("q", [], "research", Budget(max_cost_rub=Decimal("0")))
    assert isinstance(r, IntakeRefuse)
    assert r.kind == RefusalKind.BUDGET_ZERO


def test_intake_below_min_plan_cost():
    r = intake(
        "q",
        [],
        "research",
        Budget(max_cost_rub=Decimal("5")),
        min_plan_cost=Decimal("10"),
    )
    assert isinstance(r, IntakeRefuse) and r.kind == RefusalKind.BUDGET_ZERO


def test_intake_duration_too_short():
    r = intake(
        "q",
        [],
        "research",
        None,
        duration_s=30,
        finalize_reserve_s=30,
    )
    assert isinstance(r, IntakeRefuse)
    assert r.kind == RefusalKind.DURATION_TOO_SHORT


def test_intake_accepts_ok():
    assert isinstance(
        intake("do science", [], "research", Budget(max_cost_rub=Decimal("100"))),
        IntakeAccept,
    )


def test_intake_safety_port_refuses():
    class Port:
        def classify(self, request, inputs):
            return RefusalKind.OUT_OF_SCOPE

    r = intake("q", [], "research", None, safety=Port())
    assert isinstance(r, IntakeRefuse) and r.kind == RefusalKind.OUT_OF_SCOPE
    assert r.classifier_error is False


def test_intake_safety_port_timeout():
    class Port:
        def classify(self, request, inputs):
            raise TimeoutError

    r = intake("q", [], "research", None, safety=Port(), safety_timeout_s=0.5)
    assert isinstance(r, IntakeRefuse) and r.kind == RefusalKind.SAFETY
    assert r.classifier_error is True


def test_intake_safety_port_hung_enforces_timeout():
    import time

    class Port:
        def classify(self, request, inputs):
            time.sleep(2.0)
            return None

    r = intake("q", [], "research", None, safety=Port(), safety_timeout_s=0.2)
    assert isinstance(r, IntakeRefuse)
    assert r.kind == RefusalKind.SAFETY
    assert r.classifier_error is True
    assert "timeout" in r.reason


def test_intake_safety_port_error_sets_classifier_error():
    class Port:
        def classify(self, request, inputs):
            raise RuntimeError("boom")

    r = intake("q", [], "research", None, safety=Port())
    assert isinstance(r, IntakeRefuse)
    assert r.kind == RefusalKind.SAFETY
    assert r.classifier_error is True
    assert "RuntimeError" in r.reason


@pytest.mark.asyncio
async def test_intake_refuses_before_spec(tmp_path, clock):
    rt = await open_runtime(tmp_path, "st_in", clock=clock, owner="t1")
    _, arts = attach_executor(rt, tmp_path)
    rt.artifacts = arts
    await rt.create_study(
        profile="research",
        request="",
        catalog_version="0.1.0",
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
    fins = [e for e in events if e.type == "StudyFinalized"]
    assert len(fins) == 1
    assert fins[0].payload["trigger"] == "intake"
    assert fins[0].payload["terminal_reason"] == "refused"
    refusal = str(fins[0].payload.get("refusal") or "")
    assert "empty_request" in refusal
    # M1: report and study conclusion both explain the refusal.
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
        catalog_version="0.1.0",
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
        catalog_version="0.1.0",
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
async def test_intake_refuse_survives_crash(tmp_path, clock):
    """Crash after StudyCreated+StudyRefused → reopen finalizes trigger=intake."""
    from coscientist.core.domain import HitlPolicy, Study
    from coscientist.core.graph import GraphNode

    sid = "st_crash_ref"
    rt = await open_runtime(tmp_path, sid, clock=clock, owner="t1")
    now = clock.now()
    dl = now + timedelta(seconds=60)
    study = Study(
        study_id=sid,
        profile="research",
        request="",
        catalog_version="0.1.0",
        hitl_policy=HitlPolicy(),
        deadline=dl,
        created_at=now,
        finalize_reserve_s=5,
    )
    await rt._append(
        [
            EventEnvelope(
                study_id=sid,
                type="StudyCreated",
                entity_key=sid,
                idem_key=make_idem("StudyCreated", sid),
                payload=study.model_dump(mode="json"),
                ts=now,
                schema_version=2,
            ),
            EventEnvelope(
                study_id=sid,
                type="GraphNodeUpserted",
                entity_key=f"{sid}:study",
                idem_key=make_idem("GraphNodeUpserted", f"{sid}:study"),
                payload=GraphNode(
                    node_id=f"{sid}:study",
                    kind="study",
                    study_id=sid,
                    ref_id=sid,
                    created_at=now,
                ).model_dump(mode="json"),
                ts=now,
            ),
            EventEnvelope(
                study_id=sid,
                type="StudyRefused",
                entity_key=sid,
                idem_key=make_idem("StudyRefused", sid),
                payload={
                    "kind": RefusalKind.EMPTY_REQUEST.value,
                    "reason": "empty request",
                    "classifier_error": False,
                },
                ts=now,
                schema_version=1,
            ),
        ]
    )
    assert rt._intake_refusal is not None
    assert not rt._finalized
    await close_runtime(rt)

    clock.advance(timedelta(seconds=120))  # past ed — intake must still win
    rt2 = await open_runtime(tmp_path, sid, clock=clock, owner="t2")
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
