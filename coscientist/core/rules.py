"""Runtime research-model rules (DESIGN / RESEARCH_MODEL) — M0 state-core surface.

Graph / add_paper / Claim-with-test-defaults live in tests/refmodel.py (oracle).
HITL costly gate / DelegatedBudget live in ``coscientist.experimental.costly`` (pre-M2).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from coscientist.core.graph import load_schema
from coscientist.core.stance import (  # re-export
    DEFAULT_TESTS_INTENT,
    STANCE,
    TestsIntent,
    claim_stance,
)
from coscientist.core.verdict import (
    claim_counts_for_verdict,
    excluded_confirmations_note,
)
from coscientist.core.verdict import verdict as hyp_verdict

SCHEMA = load_schema()
PAIRS = {e["name"]: {tuple(p) for p in e["pairs"]} for e in SCHEMA["edges"]}

__all__ = [
    "DEFAULT_TESTS_INTENT",
    "STANCE",
    "TestsIntent",
    "claim_stance",
    "edge_allowed",
    "priority_refuted_support_note",
    "study_fulfilled",
    "validate_plan",
    "verdict",
]


def edge_allowed(name: str, src_kind: str, dst_kind: str) -> bool:
    return (src_kind, dst_kind) in PAIRS.get(name, set())


class PlanInvalid(ValueError):
    pass


class _GraphLike(Protocol):
    def out(self, name: str, src: str) -> list[str]: ...
    def inc(self, name: str, dst: str) -> list[str]: ...
    def of(self, kind: str) -> list[str]: ...


def validate_plan(gr: _GraphLike, plan_id: str) -> None:
    exps = gr.out("plans", plan_id)
    for x in gr.of("experiment"):
        if len(gr.inc("plans", x)) != 1:
            raise PlanInvalid(f"RT1: {x} linked to {len(gr.inc('plans', x))} plans")
    for x in exps:
        if not gr.out("tests", x) and not gr.out("addresses", x):
            raise PlanInvalid(f"{x}: needs tests or addresses")
    covered: set[str] = set()
    for x in exps:
        covered |= set(gr.out("addresses", x))
        for h in gr.out("tests", x):
            covered |= set(gr.out("addresses", h))
    missing = set(gr.of("question")) - covered
    if missing:
        raise PlanInvalid(f"questions not covered by LIT/EXP: {sorted(missing)}")


class _ClaimLike(Protocol):
    accepted: bool
    provenance: str
    # tests.intent on the experiment→hypothesis edge (default confirm).
    intent: TestsIntent
    # R17 metadata (DESIGN_M1 §12); None = not supplied → base counts() only.
    criterion_frozen_at_seq: int | None
    first_cited_evidence_seq: int | None
    confirming_evidence_ids: set[str] | frozenset[str] | list[str] | None
    threshold_source_evidence_ids: set[str] | frozenset[str] | list[str] | None

    @property
    def stance(self) -> str: ...

    @property
    def is_lit(self) -> bool: ...

    def counts(self) -> bool: ...

    @property
    def attempt_id(self) -> str: ...

    @property
    def artifact_id(self) -> str: ...


def _tests_intent(claim: _ClaimLike) -> TestsIntent:
    """Read ``tests.intent`` explicitly; typed default is ``confirm``."""
    intent: TestsIntent = claim.intent or DEFAULT_TESTS_INTENT
    if intent not in ("confirm", "refute"):
        raise ValueError(f"invalid tests.intent: {intent!r}")
    return intent


def _claim_counts(claim: _ClaimLike) -> bool:
    """Base ``counts()`` plus R17 when freeze/evidence metadata is present.

    Delegates to ``verdict.claim_counts_for_verdict`` (no reverse import cycle).
    """
    return claim_counts_for_verdict(claim)


def verdict(
    claims: Sequence[_ClaimLike],
    any_experiment_done: bool,
    k: int = 1,
    *,
    refutations_needed: int = 1,
    refutation_priority: bool = False,
    supported_needed: int = 2,
):
    """Hypothesis/question verdict (PR8b / DESIGN_HYP_DELTA).

    Stubs/partial/lit/invalid are excluded **before** counting. ``supported``
    needs ≥ ``supported_needed`` severe consistent claims with ≥1 capable
    ``intent=refute``. ``k`` is PR8 ``confirmations_needed`` (snapshot field).
    """
    return hyp_verdict(
        claims,
        any_experiment_done,
        k,
        refutations_needed=refutations_needed,
        refutation_priority=refutation_priority,
        supported_needed=supported_needed,
    )


def priority_refuted_support_note(
    claims: Sequence[_ClaimLike],
    any_experiment_done: bool,
    k: int = 1,
    *,
    refutations_needed: int = 1,
    refutation_priority: bool = False,
    supported_needed: int = 2,
) -> str | None:
    """PR8 note when refuted and stub/partial confirmations were excluded."""
    v, _ = verdict(
        claims,
        any_experiment_done,
        k,
        refutations_needed=refutations_needed,
        refutation_priority=refutation_priority,
        supported_needed=supported_needed,
    )
    if v != "refuted":
        return None
    return excluded_confirmations_note(claims, verdict_value=v)


# Caps fulfilled for every final_type (single source used by study_view + finalize).
_FULFILLMENT_BLOCKING = frozenset(
    {
        "untested",
        "not_attempted",
        "inconclusive",
        "rubric_not_met",
        "rubric_pending",
    }
)
# Disposition values that settle a question (not raw claim "supported").
# ``inconclusive`` is intentionally excluded — it blocks fulfilled (PR13).
_SETTLED_DISPOSITIONS = frozenset(
    {
        "answered",
        "refuted",
        "survived",
        "needs_more_data",
        "infeasible",
        "ill_posed",
    }
)


def study_fulfilled(
    question_verdicts: dict[str, str],
    all_required_success: bool,
    *,
    question_final_types: dict[str, str] | None = None,
) -> bool:
    """Fulfilled when required tasks succeed and every question is settled.

    Always blocked by not_attempted / inconclusive / rubric_not_met /
    rubric_pending / untested (never raise study_view past partial).

    ``final_type=artifact``: settled ⇔ disposition is answered (rubric MET).
    ``final_type=both``: settled ⇔ disposition in answered/refuted/survived/….
    ``final_type=verdict``: blocking set above; other settled values ok.
    """
    if not all_required_success:
        return False
    ftypes = question_final_types or {}
    for qid, v in question_verdicts.items():
        if v in _FULFILLMENT_BLOCKING:
            return False
        ft = ftypes.get(qid, "verdict")
        if ft == "artifact":
            if v != "answered":
                return False
            continue
        if ft == "both":
            if v not in _SETTLED_DISPOSITIONS:
                return False
            continue
        # verdict: blocking already applied; remaining values may fulfill
    return True


def study_view_verdict(
    all_required_success: bool,
    question_verdicts: dict[str, str],
    *,
    question_final_types: dict[str, str] | None = None,
) -> str:
    """Wrapper aligned with study_view(): fulfilled iff study_fulfilled(...)."""
    if study_fulfilled(
        question_verdicts,
        all_required_success,
        question_final_types=question_final_types,
    ):
        return "fulfilled"
    if not all_required_success:
        return "failed"
    return "partial"


def _bounds(card: str) -> tuple[int, int | None]:
    lo, hi = card.split("..")
    return int(lo), (None if hi == "n" else int(hi))


def check_cardinality(gr: _GraphLike) -> list[str]:
    bad: list[str] = []
    for e in SCHEMA["edges"]:
        name, card = e["name"], e["card"]
        for src_kind in {a for a, _ in e["pairs"]}:
            lo, hi = _bounds(card.get("out_by_src", {}).get(src_kind, card["out"]))
            for nid in gr.of(src_kind):
                n = len(gr.out(name, nid))
                if n < lo or (hi is not None and n > hi):
                    bad.append(f"{name} out {nid}={n}")
        lo, hi = _bounds(card["in"])
        for dst_kind in {b for _, b in e["pairs"]}:
            for nid in gr.of(dst_kind):
                n = len(gr.inc(name, nid))
                if n < lo or (hi is not None and n > hi):
                    bad.append(f"{name} in {nid}={n}")
    return bad
