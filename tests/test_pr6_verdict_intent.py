"""M1 PR6: tests.intent → survived; k_ref + refutation_priority; R17."""

from __future__ import annotations

from datetime import timedelta

import pytest

from coscientist.core.r17 import (
    claim_counts_under_r17,
    r17_criterion_before_evidence,
    r17_threshold_source_not_counted,
)
from coscientist.core.runtime_factory import close_runtime, open_runtime
from coscientist.core.upcasters import upcast


def test_refute_intent_met_is_survived(impl):
    """tests.intent=refute + met → stance supports, verdict survived."""
    c = impl.Claim("met", intent="refute")
    assert c.stance == "supports"
    assert impl.verdict([c], True)[0] == "survived"
    assert impl.verdict([impl.Claim("met", intent="confirm")], True)[0] == "supported"


def test_refutation_priority_k_ref(impl):
    """refutation_priority: distinct real non-lit refs ≥ k_ref → refuted despite supports."""
    sup = impl.Claim("met", attempt_id="S1", artifact_id="AS")
    ref1 = impl.Claim("not_met", attempt_id="R1", artifact_id="AR1")
    ref2 = impl.Claim("not_met", attempt_id="R2", artifact_id="AR2")
    assert impl.verdict([sup, ref1], True)[0] == "inconclusive"
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
    assert (
        impl.verdict(
            [sup, lit_ref],
            True,
            refutations_needed=1,
            refutation_priority=True,
        )[0]
        == "inconclusive"
    )
    stub_ref = impl.Claim(
        "not_met", provenance="stub", attempt_id="Z1", artifact_id="AZ"
    )
    assert (
        impl.verdict(
            [sup, stub_ref],
            True,
            refutations_needed=1,
            refutation_priority=True,
        )[0]
        == "inconclusive"
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


def test_upcast_study_created_k_ref_defaults():
    v, p = upcast(
        "StudyCreated",
        2,
        {
            "study_id": "s",
            "profile": "research",
            "finalize_reserve_s": 30,
        },
    )
    assert v == 3
    assert p["refutations_needed"] == 1
    assert p["refutation_priority"] is False


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
    assert created.schema_version == 3
    assert created.payload["refutations_needed"] == 3
    await close_runtime(rt)
