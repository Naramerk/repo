"""PR 4: intake Accept/Refuse before SPEC."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest

from coscientist.core.domain import Budget
from coscientist.core.intake import IntakeAccept, IntakeRefuse, intake
from coscientist.core.reasons import RefusalKind
from coscientist.core.runtime_factory import close_runtime, open_runtime
from coscientist.core.study_view import study_phase, study_view


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


def test_intake_safety_port_timeout():
    class Port:
        def classify(self, request, inputs):
            raise TimeoutError

    r = intake("q", [], "research", None, safety=Port())
    assert isinstance(r, IntakeRefuse) and r.kind == RefusalKind.SAFETY


@pytest.mark.asyncio
async def test_intake_refuses_before_spec(tmp_path, clock):
    rt = await open_runtime(tmp_path, "st_in", clock=clock, owner="t1")
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
    fins = [e for e in await rt.store.read_all() if e.type == "StudyFinalized"]
    assert len(fins) == 1
    assert fins[0].payload["trigger"] == "intake"
    assert fins[0].payload["terminal_reason"] == "refused"
    assert "empty_request" in str(fins[0].payload.get("refusal") or "")
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


def test_empty_study_past_ed_is_finalizing():
    """No tasks + past effective_deadline → finalizing (core == refmodel)."""
    import refmodel

    assert study_phase(past_effective=True) == "finalizing"
    assert refmodel.study_phase(past_effective=True) == "finalizing"
    from datetime import UTC, datetime

    now = datetime(2026, 1, 2, tzinfo=UTC)
    ed = datetime(2026, 1, 1, tzinfo=UTC)
    assert study_view([], now=now, effective_deadline=ed).phase == "finalizing"
