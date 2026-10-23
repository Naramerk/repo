"""Runtime research-model rules (DESIGN / RESEARCH_MODEL) — M0 state-core surface.

Graph / add_paper / Claim-with-test-defaults live in tests/refmodel.py (oracle).
HITL costly gate / DelegatedBudget live in ``coscientist.experimental.costly`` (pre-M2).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Literal, Protocol

from coscientist.core.graph import load_schema
from coscientist.core.r17 import claim_counts_under_r17

SCHEMA = load_schema()
PAIRS = {e["name"]: {tuple(p) for p in e["pairs"]} for e in SCHEMA["edges"]}


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


STANCE = {"met": "supports", "not_met": "refutes", "unassessed": "inconclusive"}


def claim_stance(criterion_status: str) -> Literal["supports", "refutes", "inconclusive"]:
    return STANCE[criterion_status]  # type: ignore[return-value]


TestsIntent = Literal["confirm", "refute"]
DEFAULT_TESTS_INTENT: TestsIntent = "confirm"


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

    R17 (§12) applies only to confirming claims; ``refutes`` skip the
    confirming-evidence gate.
    """
    if not claim.counts():
        return False
    if claim.stance == "refutes":
        return True
    frozen_at = claim.criterion_frozen_at_seq
    first_ev = claim.first_cited_evidence_seq
    confirming = claim.confirming_evidence_ids
    threshold_src = claim.threshold_source_evidence_ids
    # No R17 metadata → base counts() already decided.
    if frozen_at is None and first_ev is None and confirming is None:
        return True
    return claim_counts_under_r17(
        criterion_frozen_at_seq=frozen_at,
        first_cited_evidence_seq=first_ev,
        confirming_evidence_ids=set(confirming or ()),
        threshold_source_evidence_ids=set(threshold_src or ()),
        stance=claim.stance,
    )


def verdict(
    claims: Sequence[_ClaimLike],
    any_experiment_done: bool,
    k: int = 1,
    *,
    refutations_needed: int = 1,
    refutation_priority: bool = False,
    min_independent_support: int = 2,
    aggregate: Literal["count", "evalue"] = "count",
    evalue_alpha: float = 0.05,
):
    """Hypothesis/question verdict (PR8b / DESIGN_HYP_DELTA).

    Stubs/partial/lit/invalid are excluded **before** counting (no provenance
    ceiling). ``supported`` needs ≥ ``min_independent_support`` severe consistent
    claims with ≥1 ``intent=refute``. Runtime calls via ``dispositions()``.
    """
    from coscientist.core.verdict import verdict as _hyp_verdict

    return _hyp_verdict(
        claims,
        any_experiment_done,
        k,
        refutations_needed=refutations_needed,
        refutation_priority=refutation_priority,
        min_independent_support=min_independent_support,
        aggregate=aggregate,
        evalue_alpha=evalue_alpha,
    )


def priority_refuted_support_note(
    claims: Sequence[_ClaimLike],
    any_experiment_done: bool,
    k: int = 1,
    *,
    refutations_needed: int = 1,
    refutation_priority: bool = False,
    min_independent_support: int = 2,
    aggregate: Literal["count", "evalue"] = "count",
    evalue_alpha: float = 0.05,
) -> str | None:
    """Report note when refuted and stub/partial confirmations were excluded.

    PR8b: stubs never count; note lists excluded supporting stubs/partials
    (merges the old PR8 priority-win stub note).
    """
    from coscientist.core.verdict import excluded_confirmations_note

    v, _ = verdict(
        claims,
        any_experiment_done,
        k,
        refutations_needed=refutations_needed,
        refutation_priority=refutation_priority,
        min_independent_support=min_independent_support,
        aggregate=aggregate,
        evalue_alpha=evalue_alpha,
    )
    if v != "refuted":
        return None
    note = excluded_confirmations_note(claims, verdict_value=v)
    if note is not None:
        return note
    # Fall through to legacy distinct stub/partial supports among non-counting.
    acc = [c for c in claims if c.accepted]
    q = [c for c in acc if _claim_counts(c)]
    supports = [c for c in q if c.stance == "supports"]
    stub_partial = [c for c in supports if c.provenance in ("stub", "partial")]
    if not stub_partial:
        return None
    # Distinct supporting results (same metric as verdict counts).
    n = min(
        len({c.attempt_id for c in stub_partial}),
        len({c.artifact_id for c in stub_partial}),
    )
    if n < 1:
        n = len(stub_partial)
    return f"refuted; {n} supporting results were stub/partial"


def study_fulfilled(question_verdicts: dict[str, str], all_required_success: bool) -> bool:
    return all_required_success and all(v != "untested" for v in question_verdicts.values())


def study_view_verdict(all_required_success: bool, question_verdicts: dict[str, str]) -> str:
    """Wrapper aligned with study_view(): fulfilled iff study_fulfilled(...)."""
    if study_fulfilled(question_verdicts, all_required_success):
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
