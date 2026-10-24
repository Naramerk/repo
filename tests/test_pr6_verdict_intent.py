"""M1 PR6: tests.intent → survived; k_ref + refutation_priority; R17."""

from __future__ import annotations

from datetime import timedelta

import pytest

from coscientist.core.claims.graph import load_schema
from coscientist.core.claims.r17 import (
    claim_counts_under_r17,
    r17_criterion_before_evidence,
    r17_threshold_source_not_counted,
)
from coscientist.core.claims.reasons import TaskReason
from coscientist.core.execution.domain import TaskSpec
from tests.helpers import close_runtime, open_runtime


def test_refute_intent_met_is_survived(impl):
    """tests.intent=refute + met → stance supports; defaults → survived."""
    c = impl.Claim("met", intent="refute")
    assert c.stance == "supports"
    # supported_needed default 2: one severe refute-intent claim → survived.
    assert impl.verdict([c], True)[0] == "survived"
    assert impl.verdict([c], True, supported_needed=2)[0] == "survived"
    # Confirm alone never reaches supported (needs capable intent=refute).
    assert impl.verdict([impl.Claim("met", intent="confirm")], True)[0] == "survived"


def test_refutation_priority_k_ref(impl):
    """refutation_priority: distinct real non-lit refs ≥ k_ref → refuted despite supports."""
    sup = impl.Claim("met", attempt_id="S1", artifact_id="AS")
    ref1 = impl.Claim("not_met", attempt_id="R1", artifact_id="AR1")
    ref2 = impl.Claim("not_met", attempt_id="R2", artifact_id="AR2")
    # Without priority: conflict → inconclusive.
    assert impl.verdict([sup, ref1], True, refutation_priority=False)[0] == "inconclusive"
    assert (
        impl.verdict(
            [sup, ref1],
            True,
            refutations_needed=1,
            refutation_priority=True,
        )[0]
        == "refuted"
    )
    assert (
        impl.verdict(
            [sup, ref1],
            True,
            refutations_needed=2,
            refutation_priority=True,
        )[0]
        == "inconclusive"
    )
    assert (
        impl.verdict(
            [sup, ref1, ref2],
            True,
            refutations_needed=2,
            refutation_priority=True,
        )[0]
        == "refuted"
    )
    lit_ref = impl.Claim(
        "not_met",
        evidence_kind="literature",
        lit_own_criterion=True,
        attempt_id="L1",
        artifact_id="AL",
    )
    # Lit never counts → only severe confirm remains → survived.
    assert (
        impl.verdict(
            [sup, lit_ref],
            True,
            refutations_needed=1,
            refutation_priority=True,
        )[0]
        == "survived"
    )
    stub_ref = impl.Claim(
        "not_met", provenance="stub", attempt_id="Z1", artifact_id="AZ"
    )
    # Stub excluded → only severe confirm → survived.
    assert (
        impl.verdict(
            [sup, stub_ref],
            True,
            refutations_needed=1,
            refutation_priority=True,
        )[0]
        == "survived"
    )


def test_priority_refuted_not_capped_by_stub_supports(impl):
    """PR8b: stubs excluded before counting; ≥ k_ref real refs → refuted + note."""
    from coscientist.core.claims.rules import priority_refuted_support_note

    stub_sup = impl.Claim(
        "met", provenance="stub", attempt_id="S1", artifact_id="AS"
    )
    real_ref = impl.Claim(
        "not_met", provenance="real", attempt_id="R1", artifact_id="AR1"
    )
    assert (
        impl.verdict(
            [stub_sup, real_ref],
            True,
            refutations_needed=1,
            refutation_priority=True,
        )[0]
        == "refuted"
    )
    # Without priority, stub excluded → only real ref → still refuted.
    assert impl.verdict([stub_sup, real_ref], True, refutation_priority=False)[0] == "refuted"
    note = priority_refuted_support_note(
        [stub_sup, real_ref],
        True,
        refutations_needed=1,
        refutation_priority=True,
    )
    assert note == "refuted; 1 supporting results were stub/partial"
    # Note also when priority=False (excluded stubs still reported).
    assert (
        priority_refuted_support_note(
            [stub_sup, real_ref],
            True,
            refutations_needed=1,
            refutation_priority=False,
        )
        == "refuted; 1 supporting results were stub/partial"
    )


def test_r17_criterion_after_lit_valid():
    """Criterion frozen after LIT evidence seq is still valid for later claims."""
    assert r17_criterion_before_evidence(15, 20) is True
    assert r17_criterion_before_evidence(15, 10) is False


def test_r17_threshold_source_not_counted():
    confirming = {"ev_paper_num", "ev_holdout"}
    threshold_src = {"ev_paper_num"}
    usable = r17_threshold_source_not_counted(confirming, threshold_src)
    assert usable == {"ev_holdout"}
    assert claim_counts_under_r17(
        criterion_frozen_at_seq=5,
        first_cited_evidence_seq=10,
        confirming_evidence_ids=usable,
        threshold_source_evidence_ids=set(),
    )
    assert not claim_counts_under_r17(
        criterion_frozen_at_seq=5,
        first_cited_evidence_seq=10,
        confirming_evidence_ids={"ev_paper_num"},
        threshold_source_evidence_ids={"ev_paper_num"},
    )


def test_r17_claim_excluded_from_verdict(impl):
    """R17 failure → confirming claim does not count; refutes still count (§12)."""
    bad = impl.Claim(
        "met",
        attempt_id="B1",
        artifact_id="AB",
        criterion_frozen_at_seq=20,
        first_cited_evidence_seq=10,
        confirming_evidence_ids=frozenset({"ev1"}),
        threshold_source_evidence_ids=frozenset(),
    )
    assert bad.counts() is True  # base RM §4.1
    assert impl.verdict([bad], True)[0] == "inconclusive"

    only_threshold = impl.Claim(
        "met",
        attempt_id="B2",
        artifact_id="AB2",
        criterion_frozen_at_seq=5,
        first_cited_evidence_seq=10,
        confirming_evidence_ids=frozenset({"ev_paper"}),
        threshold_source_evidence_ids=frozenset({"ev_paper"}),
    )
    assert only_threshold.counts() is True
    assert impl.verdict([only_threshold], True)[0] == "inconclusive"

    good = impl.Claim(
        "met",
        attempt_id="G1",
        artifact_id="AG",
        criterion_frozen_at_seq=5,
        first_cited_evidence_seq=10,
        confirming_evidence_ids=frozenset({"ev_holdout"}),
        threshold_source_evidence_ids=frozenset({"ev_paper"}),
    )
    assert good.counts() is True
    # PR8b: single severe confirm → survived (supported needs ≥2 + refute intent).
    assert impl.verdict([good], True)[0] == "survived"

    # Refuting claim with empty confirming set still counts (R17 is confirm-only).
    ref_empty = impl.Claim(
        "not_met",
        attempt_id="R1",
        artifact_id="AR",
        criterion_frozen_at_seq=20,
        first_cited_evidence_seq=10,
        confirming_evidence_ids=frozenset(),
        threshold_source_evidence_ids=frozenset(),
    )
    assert ref_empty.stance == "refutes"
    assert impl.verdict([ref_empty], True)[0] == "refuted"


def test_invalid_intent_raises(impl):
    with pytest.raises(ValueError, match="invalid tests.intent"):
        impl.Claim("met", intent="maybe")  # type: ignore[arg-type]


def test_tests_edge_declares_intent_attr():
    """graph_schema: tests.intent is an edge attr, not only a note."""
    schema = load_schema()
    tests = next(e for e in schema["edges"] if e["name"] == "tests")
    attrs = {a["name"]: a for a in tests.get("attrs", [])}
    assert "intent" in attrs
    assert attrs["intent"]["type"] == "enum(confirm|refute)"
    assert attrs["intent"].get("default") == "confirm"




@pytest.mark.asyncio
async def test_create_study_persists_k_ref(tmp_path, clock, runtime_cls):
    sid = "st_kref"
    rt = await open_runtime(
        tmp_path, sid, clock=clock, owner="t1", runtime_cls=runtime_cls
    )
    await rt.create_study(
        profile="research",
        request="q",
        catalog_version="0.1.0",
        deadline=clock.now() + timedelta(seconds=60),
        sid=sid,
        finalize_reserve_s=5,
        refutations_needed=3,
        refutation_priority=True,
    )
    assert rt.study is not None
    assert rt.study.refutations_needed == 3
    assert rt.study.refutation_priority is True
    created = next(e for e in await rt.store.read_all() if e.type == "StudyCreated")
    assert created.schema_version == 1
    assert created.payload["refutations_needed"] == 3
    assert created.payload["confirmations_needed"] == 1
    assert created.payload["aggregate"] == "count"
    assert created.payload["max_proposals"] == 3
    assert created.payload["supported_needed"] == 2
    assert created.payload["max_expansions"] == 2
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_study_refutations_needed_to_verdict(tmp_path, clock, runtime_cls, impl):
    """Study.k_ref → verdict (direct call until PR 8 wires runtime)."""
    sid = "st_kref_v"
    rt = await open_runtime(
        tmp_path, sid, clock=clock, owner="t1", runtime_cls=runtime_cls
    )
    await rt.create_study(
        profile="research",
        request="q",
        catalog_version="0.1.0",
        deadline=clock.now() + timedelta(seconds=60),
        sid=sid,
        finalize_reserve_s=5,
        refutations_needed=2,
        refutation_priority=True,
    )
    assert rt.study is not None
    sup = impl.Claim("met", attempt_id="S1", artifact_id="AS")
    ref1 = impl.Claim("not_met", attempt_id="R1", artifact_id="AR1")
    ref2 = impl.Claim("not_met", attempt_id="R2", artifact_id="AR2")
    # One real ref < k_ref → inconclusive; two → refuted.
    assert (
        impl.verdict(
            [sup, ref1],
            True,
            refutations_needed=rt.study.refutations_needed,
            refutation_priority=rt.study.refutation_priority,
        )[0]
        == "inconclusive"
    )
    assert (
        impl.verdict(
            [sup, ref1, ref2],
            True,
            refutations_needed=rt.study.refutations_needed,
            refutation_priority=rt.study.refutation_priority,
        )[0]
        == "refuted"
    )
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_cancel_study_tick_closes_remaining(
    tmp_path, clock, runtime_cls, monkeypatch
):
    """cancel_study then tick: _close_remaining(CANCELLED) when close was skipped."""
    import coscientist.core.plan.cancel as cancel_mod
    from coscientist.core.execution.domain import TaskState
    from coscientist.core.finalize import finalize as finalize_mod

    sid = "st_cx_tick"
    rt = await open_runtime(
        tmp_path, sid, clock=clock, owner="t1", runtime_cls=runtime_cls
    )
    await rt.create_study(
        profile="research",
        request="q",
        catalog_version="0.1.0",
        deadline=clock.now() + timedelta(seconds=120),
        sid=sid,
        finalize_reserve_s=5,
    )
    for key in ("SPEC", "HYP", "LIT"):
        await rt.start_attempt(key)
        await rt.cancel_task(key, reason="cancelled")
    await rt.accept_plan(
        [
            TaskSpec(
                task_key="P", title="p", goal="g", route="experiment", depends_on=["PLAN"], required=True, tolerate_failed_dep=False),
            TaskSpec(
                task_key="C", title="c", goal="g", route="experiment", depends_on=["P"], required=True, tolerate_failed_dep=False),
        ],
        questions=[],
    )
    await rt.start_attempt("P")
    # cancel_study uses finalize_mod.finalize (module attr) — patch that path.
    real_close = cancel_mod._close_open_work
    real_finalize = finalize_mod.finalize
    monkeypatch.setattr(cancel_mod, "_close_open_work", lambda *a, **k: [])

    async def _no_finalize(_rt, *, trigger):
        return None

    monkeypatch.setattr(finalize_mod, "finalize", _no_finalize)
    await rt.cancel_study(by="user")
    types = [e.type for e in await rt.store.read_all()]
    assert "StudyCancelled" in types
    assert "StudyFinalized" not in types
    assert rt.tasks[f"{sid}/P"].state != TaskState.DONE
    monkeypatch.setattr(cancel_mod, "_close_open_work", real_close)
    monkeypatch.setattr(finalize_mod, "finalize", real_finalize)
    await close_runtime(rt)

    rt2 = await open_runtime(tmp_path, sid, clock=clock, owner="t2", runtime_cls=runtime_cls)
    await rt2.tick()
    assert rt2.tasks[f"{sid}/P"].state == TaskState.DONE
    assert rt2.tasks[f"{sid}/P"].reason == TaskReason.CANCELLED
    assert rt2.tasks[f"{sid}/C"].reason == TaskReason.CANCELLED
    fins = [e for e in await rt2.store.read_all() if e.type == "StudyFinalized"]
    assert len(fins) == 1 and fins[0].payload["trigger"] == "cancel"
    await close_runtime(rt2)


@pytest.mark.asyncio
async def test_cancel_study_then_tick_plain(tmp_path, clock, runtime_cls):
    """Plain cancel_study → tick: already finalized; tick is a no-op."""
    sid = "st_cx_plain"
    rt = await open_runtime(
        tmp_path, sid, clock=clock, owner="t1", runtime_cls=runtime_cls
    )
    await rt.create_study(
        profile="research",
        request="q",
        catalog_version="0.1.0",
        deadline=clock.now() + timedelta(seconds=120),
        sid=sid,
        finalize_reserve_s=5,
    )
    for key in ("SPEC", "HYP", "LIT", "PLAN", "REPORT"):
        await rt.start_attempt(key)
        await rt.cancel_task(key, reason="cancelled")
    await rt.cancel_study(by="user")
    fins = [e for e in await rt.store.read_all() if e.type == "StudyFinalized"]
    assert len(fins) == 1 and fins[0].payload["trigger"] == "cancel"
    await rt.tick()
    fins2 = [e for e in await rt.store.read_all() if e.type == "StudyFinalized"]
    assert len(fins2) == 1
    await close_runtime(rt)
