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
):
    """Hypothesis/question verdict (RM §4.1 + DESIGN_M1 PR 6).

    Pure rule function. Runtime finalize/report calls this via
    ``dispositions()`` (M1 PR 8) with Study ``refutations_needed`` /
    ``refutation_priority``. ``tests.intent=refute`` + met → stance supports,
    verdict ``survived``. With ``refutation_priority``, distinct real non-lit
    refs ≥ k_ref → ``refuted`` even with supports; that outcome is **not** capped
    by stub/partial supports. Lit/non-real refs never override real supports.
    """
    acc = [c for c in claims if c.accepted]
    if not acc and not any_experiment_done:
        return "untested", False
    q = [c for c in acc if _claim_counts(c)]
    confirm_sup = [
        c for c in q if c.stance == "supports" and _tests_intent(c) != "refute"
    ]
    survived_sup = [
        c for c in q if c.stance == "supports" and _tests_intent(c) == "refute"
    ]
    ref = [c for c in q if c.stance == "refutes"]
    all_sup = confirm_sup + survived_sup

    def distinct(cs: list[_ClaimLike]) -> int:
        return (
            min(len({c.attempt_id for c in cs}), len({c.artifact_id for c in cs}))
            if cs
            else 0
        )

    def priority_refs(cs: list[_ClaimLike]) -> list[_ClaimLike]:
        # Real computational/experimental only; literature must not flip priority.
        return [c for c in cs if c.provenance == "real" and not c.is_lit]

    priority_win = (
        refutation_priority and distinct(priority_refs(ref)) >= refutations_needed
    )
    v: str
    if priority_win:
        v = "refuted"
    elif all_sup and ref:
        # Lit / non-real refs with supports → inconclusive (no priority win).
        v = "inconclusive"
    elif distinct(confirm_sup) >= k:
        v = "supported"
    elif distinct(survived_sup) >= k:
        v = "survived"
    elif distinct(ref) >= k:
        v = "refuted"
    else:
        v = "inconclusive"

    # Provenance ceiling over counted claims. Exception: ≥ k_ref real priority
    # refs stay ``refuted`` — stub/partial supports must not cap a refutation.
    if v in ("supported", "survived") and any(c.provenance != "real" for c in q):
        v = "inconclusive"
    elif (
        v == "refuted"
        and not priority_win
        and any(c.provenance != "real" for c in q)
    ):
        v = "inconclusive"
    decisive = all_sup + ref
    return v, bool(decisive) and all(c.is_lit for c in decisive)


def priority_refuted_support_note(
    claims: Sequence[_ClaimLike],
    any_experiment_done: bool,
    k: int = 1,
    *,
    refutations_needed: int = 1,
    refutation_priority: bool = False,
) -> str | None:
    """Report note when priority-win keeps ``refuted`` despite stub/partial supports.

    Returns e.g. ``\"refuted; 2 supporting results were stub/partial\"``, or
    ``None`` when the exception does not apply (no priority-win, or no
    stub/partial counting supports).
    """
    if not refutation_priority:
        return None
    v, _ = verdict(
        claims,
        any_experiment_done,
        k,
        refutations_needed=refutations_needed,
        refutation_priority=refutation_priority,
    )
    if v != "refuted":
        return None
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
