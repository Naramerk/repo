"""Runtime research-model rules (DESIGN / RESEARCH_MODEL) — M0 state-core surface.

Graph / add_paper / Claim-with-test-defaults live in tests/refmodel.py (oracle).
HITL costly gate / DelegatedBudget live in ``coscientist.experimental.costly`` (pre-M2).
"""

from __future__ import annotations

from typing import Literal, Protocol

from coscientist.core.graph import load_schema

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


class _ClaimLike(Protocol):
    accepted: bool
    provenance: str

    @property
    def stance(self) -> str: ...

    @property
    def is_lit(self) -> bool: ...

    def counts(self) -> bool: ...

    @property
    def attempt_id(self) -> str: ...

    @property
    def artifact_id(self) -> str: ...


def verdict(claims: list[_ClaimLike], any_experiment_done: bool, k: int = 1):
    acc = [c for c in claims if c.accepted]
    if not acc and not any_experiment_done:
        return "untested", False
    q = [c for c in acc if c.counts()]
    sup = [c for c in q if c.stance == "supports"]
    ref = [c for c in q if c.stance == "refutes"]

    def distinct(cs: list[_ClaimLike]) -> int:
        return (
            min(len({c.attempt_id for c in cs}), len({c.artifact_id for c in cs}))
            if cs
            else 0
        )

    if sup and ref:
        v = "inconclusive"
    elif distinct(sup) >= k:
        v = "supported"
    elif distinct(ref) >= k:
        v = "refuted"
    else:
        v = "inconclusive"
    if v in ("supported", "refuted") and any(c.provenance != "real" for c in q):
        v = "inconclusive"
    decisive = sup + ref
    return v, bool(decisive) and all(c.is_lit for c in decisive)


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
