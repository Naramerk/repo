"""RC8 (#104): question-path verdict matrix = ``hypothesis_verdict`` only.

One test per RESEARCH_MODEL §4.1 / research-cycle RC8 matrix row.
Asserts ``(verdict, rule)`` — no refmodel compare. Tail rows 12–14 stay distinct.
"""

from __future__ import annotations

from coscientist.core.claims.claims import RuntimeClaim
from coscientist.core.claims.verdict import hypothesis_verdict

_THRESH = dict(
    refutations_needed=1,
    refutation_priority=False,
    supported_needed=2,
    confirmations_needed=1,
)


def _effect(
    *,
    attempt_id: str = "A1#1",
    countable: bool = True,
    **extra: object,
) -> dict:
    base: dict = {
        "est": 1.5,
        "ci_low": 1.5,
        "ci_high": 1.5,
        "producer": "core",
        "trusted": True,
        "core_evaluated": True,
        "countable": countable,
        "produced_by_call_id": f"{attempt_id}:1",
    }
    base.update(extra)
    return base


def _severe(**kw: object) -> RuntimeClaim:
    """Countable severe consistent claim (feeds ``use_con`` / ``use_severe``)."""
    attempt = str(kw.get("attempt_id") or "A1#1")
    base: dict = dict(
        criterion_status="met",
        accepted=True,
        provenance="real",
        frozen=True,
        evidence_kind="computational",
        attempt_id=attempt,
        artifact_id="Art1",
        validity_ok=True,
        check_role="prediction",
        claim_outcome="consistent",
        severe=True,
        independence_key="k1",
        experiment_id="A1",
        intent="confirm",
        sesoi=0.5,
        uses_sesoi=True,
        used_sesoi=True,
        predicts_null=False,
        predicted_direction="positive",
        effect=_effect(attempt_id=attempt),
        refute_capable=True,
    )
    base.update(kw)
    return RuntimeClaim(**base)  # type: ignore[arg-type]


def _inconsistent(**kw: object) -> RuntimeClaim:
    """Countable inconsistent claim (feeds ``use_inc``)."""
    attempt = str(kw.get("attempt_id") or "R1#1")
    base: dict = dict(
        criterion_status="not_met",
        accepted=True,
        provenance="real",
        frozen=True,
        evidence_kind="computational",
        attempt_id=attempt,
        artifact_id="ArtR",
        validity_ok=True,
        check_role="prediction",
        claim_outcome="inconsistent",
        severe=False,
        independence_key="r1",
        experiment_id="R1",
        intent="confirm",
        sesoi=None,
        uses_sesoi=False,
        used_sesoi=False,
        refute_capable=False,
        effect=_effect(attempt_id=attempt, est=-1.0, ci_low=-1.0, ci_high=-1.0),
    )
    base.update(kw)
    return RuntimeClaim(**base)  # type: ignore[arg-type]


def _timeout(
    *,
    kind: str,
    independence_key: str = "t1",
    attempt_id: str = "T1#1",
    experiment_id: str = "T1",
) -> RuntimeClaim:
    """Refute-role unresolved timeout (never countable)."""
    return RuntimeClaim(
        criterion_status="unassessed",
        accepted=True,
        provenance="real",
        frozen=True,
        evidence_kind="computational",
        attempt_id=attempt_id,
        artifact_id="ArtT",
        validity_ok=True,
        check_role="prediction",
        claim_outcome="indeterminate",
        severe=False,
        independence_key=independence_key,
        experiment_id=experiment_id,
        intent="refute",
        sesoi=None,
        refute_capable=False,
        refute_unresolved=True,
        timeout_kind=kind,
        effect={
            "producer": "sandbox",
            "trusted": False,
            "core_evaluated": True,
            "countable": False,
            "observation_only": True,
            "timeout_kind": kind,
            "refute_unresolved": True,
            "produced_by_call_id": f"{attempt_id}:sandbox:pred",
        },
    )


def _hv(claims: list[RuntimeClaim], **over: object) -> tuple[str, str | None]:
    kw = {**_THRESH, **over}
    v, _lit, _th, rule = hypothesis_verdict(claims, True, **kw)  # type: ignore[arg-type]
    return v, rule


# --- matrix rows (research-cycle.md RC8) ---


def test_branch_no_about() -> None:
    v, rule = _hv([])
    assert (v, rule) == ("untested", "no_counted")


def test_branch_derivation_review_only() -> None:
    review = RuntimeClaim(
        criterion_status="met",
        accepted=True,
        evidence_kind="derivation",
        provenance="real",
        frozen=True,
        claim_outcome="consistent",
        severe=True,
        validity_ok=True,
        check_role="prediction",
        derivation_executable=False,
        independence_key="d1",
        attempt_id="D0#1",
        intent="confirm",
        uses_sesoi=True,
        used_sesoi=True,
        sesoi=0.1,
        effect=_effect(attempt_id="D0#1", est=0.5, ci_low=0.2, ci_high=0.8),
    )
    v, rule = _hv([review])
    assert (v, rule) == ("inconclusive", "derivation_review_only")


def test_branch_k_ref_priority() -> None:
    v, rule = _hv([_inconsistent()], refutation_priority=True)
    assert (v, rule) == ("refuted", "k_ref")


def test_branch_k_ref_conflict() -> None:
    ref = _inconsistent()
    con = _severe(
        independence_key="c1",
        attempt_id="C1#1",
        experiment_id="C1",
        artifact_id="ArtC",
        intent="refute",
    )
    v, rule = _hv([con, ref], refutation_priority=False)
    assert (v, rule) == ("inconclusive", "k_ref")


def test_branch_k_ref_no_severe() -> None:
    v, rule = _hv([_inconsistent()], refutation_priority=False)
    assert (v, rule) == ("refuted", "k_ref")


def test_branch_inc_below_k_ref() -> None:
    v, rule = _hv([_inconsistent()], refutations_needed=2)
    assert (v, rule) == ("inconclusive", "inc_below_k_ref")


def test_branch_idle_before_support() -> None:
    """Row 7: unresolved idle with no counted severe/inc — before support gate."""
    v, rule = _hv([_timeout(kind="idle_timeout")])
    assert (v, rule) == ("untestable_by_agent", "idle_timeout")


def test_branch_support_refute_unresolved() -> None:
    """Row 8a: k_sup met + capable refute + wall unresolved (not idle)."""
    a = _severe(
        intent="refute",
        independence_key="a",
        attempt_id="A#1",
        experiment_id="A",
    )
    b = _severe(
        intent="confirm",
        independence_key="b",
        attempt_id="B#1",
        experiment_id="B",
        artifact_id="Art2",
        effect=_effect(attempt_id="B#1"),
    )
    wall = _timeout(kind="wall_timeout", independence_key="t", attempt_id="T#1")
    v, rule = _hv([a, b, wall])
    assert (v, rule) == ("inconclusive", "refute_unresolved")


def test_branch_support_min() -> None:
    a = _severe(
        intent="refute",
        independence_key="a",
        attempt_id="A#1",
        experiment_id="A",
    )
    b = _severe(
        intent="confirm",
        independence_key="b",
        attempt_id="B#1",
        experiment_id="B",
        artifact_id="Art2",
        effect=_effect(attempt_id="B#1"),
    )
    v, rule = _hv([a, b])
    assert (v, rule) == ("supported", "support_min")


def test_branch_survived_idle() -> None:
    """Row 9a: one severe + unresolved idle → untestable (same rule as idle path)."""
    one = _severe(intent="refute")
    idle = _timeout(kind="idle_timeout", independence_key="t", attempt_id="T#1")
    v, rule = _hv([one, idle])
    assert (v, rule) == ("untestable_by_agent", "idle_timeout")


def test_branch_survived() -> None:
    v, rule = _hv([_severe(intent="refute")])
    assert (v, rule) == ("survived", "single_severe")


def test_branch_idle_after_con() -> None:
    """Row 10: unresolved idle after con path exhausted (no severe counted)."""
    # Non-severe consistent counted claim + idle unresolved.
    weak = _severe(
        severe=False,
        sesoi=None,
        uses_sesoi=False,
        used_sesoi=False,
        refute_capable=False,
        claim_outcome="consistent",
        criterion_status="met",
        effect=_effect(),
    )
    # Without sesoi, claim_is_severe is False → use_con==0; idle fires as row 10/7.
    idle = _timeout(kind="idle_timeout", independence_key="t", attempt_id="T#1")
    v, rule = _hv([weak, idle])
    assert (v, rule) == ("untestable_by_agent", "idle_timeout")


def test_branch_refute_unresolved() -> None:
    """Row 11: unresolved without idle (wall) and no support/severe."""
    v, rule = _hv([_timeout(kind="wall_timeout")])
    assert (v, rule) == ("inconclusive", "refute_unresolved")


def test_branch_counted_no_support() -> None:
    """Row 12: counted remain below support — distinct from 13/14."""
    weak = RuntimeClaim(
        criterion_status="met",
        accepted=True,
        provenance="real",
        frozen=True,
        evidence_kind="computational",
        attempt_id="W#1",
        artifact_id="ArtW",
        validity_ok=True,
        check_role="prediction",
        claim_outcome="consistent",
        severe=False,
        independence_key="w1",
        experiment_id="W",
        intent="confirm",
        sesoi=None,
        uses_sesoi=False,
        used_sesoi=False,
        refute_capable=False,
        effect=_effect(attempt_id="W#1"),
    )
    assert weak.counts()
    v, rule = _hv([weak])
    assert (v, rule) == ("inconclusive", "no_counted")


def test_branch_validity_failed() -> None:
    """Row 13: only-invalid bag — rule stays ``validity_failed`` (not merged)."""
    bad = RuntimeClaim(
        criterion_status="unassessed",
        accepted=True,
        provenance="real",
        frozen=True,
        evidence_kind="computational",
        attempt_id="V#1",
        artifact_id="ArtV",
        validity_ok=False,
        check_role="validity",
        claim_outcome=None,
        severe=False,
        independence_key="v1",
        exclusion_reason="validity_failed",
        sesoi=None,
        refute_capable=False,
    )
    assert not bad.counts()
    v, rule = _hv([bad])
    assert (v, rule) == ("inconclusive", "validity_failed")


def test_branch_excluded_only() -> None:
    """Row 14: excluded-only (stub) — ``no_counted``, distinct from validity_failed."""
    stub = RuntimeClaim(
        criterion_status="met",
        accepted=True,
        provenance="stub",
        frozen=True,
        evidence_kind="computational",
        attempt_id="S#1",
        artifact_id="ArtS",
        validity_ok=True,
        check_role="prediction",
        claim_outcome="consistent",
        severe=False,
        independence_key="s1",
        sesoi=None,
        refute_capable=False,
    )
    assert not stub.counts()
    v, rule = _hv([stub])
    assert (v, rule) == ("inconclusive", "no_counted")
