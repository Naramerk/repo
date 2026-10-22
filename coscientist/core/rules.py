"""Runtime research-model rules (DESIGN / RESEARCH_MODEL).

Graph / add_paper / Claim-with-test-defaults live in tests/refmodel.py (oracle).
This module keeps only what the M0 runtime and reports need.
"""

from __future__ import annotations

from typing import Any, Literal, Protocol

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


class DelegatedBudget:
    """Budget(delegated_costly), ₽: stateful counter; actual spend is debited after each attempt."""

    def __init__(self, limit_rub: float) -> None:
        self.limit_rub, self.spent_rub, self.blocked = float(limit_rub), 0.0, False

    @property
    def remaining_rub(self) -> float:
        return max(self.limit_rub - self.spent_rub, 0.0)

    def refuse_reason(self, estimate_rub: float) -> str | None:
        if self.blocked:
            return "перерасход: делегирование заблокировано"
        if estimate_rub > self.remaining_rub:
            return "превышает остаток"
        return None

    def debit(self, actual_rub: float, estimate_rub: float) -> float:
        self.spent_rub += actual_rub
        if actual_rub > estimate_rub:
            self.blocked = True
        return actual_rub

    def to_state(self) -> dict[str, Any]:
        return {
            "limit_rub": self.limit_rub,
            "spent_rub": self.spent_rub,
            "blocked": self.blocked,
        }

    @classmethod
    def from_state(cls, state: dict[str, Any]) -> DelegatedBudget:
        b = cls(state["limit_rub"])
        b.spent_rub = float(state.get("spent_rub") or 0.0)
        b.blocked = bool(state.get("blocked"))
        return b


def costly_gate(
    mode: str,
    answer: str | None,
    cost_estimate_rub: float = 0.0,
    budget: DelegatedBudget | None = None,
    critic_accepts: bool = True,
):
    """HITL gate costly_or_nonreal. answer: approve | reject | delegate | None (timeout)."""
    if answer in ("approve", "reject"):
        eff = "RUN" if answer == "approve" else "SKIPPED(human_rejected)"
        return eff, {
            "decided_by": "human",
            "delegated_by": None,
            "reason": answer,
            "spent_rub": 0.0,
        }
    if mode not in ("fail", "delegate_capped", "delegate"):
        raise ValueError(mode)
    delegated_by = "human" if answer == "delegate" else "timeout"
    if mode == "fail" and answer is None:
        return (
            "FAILED(human_timeout)",
            {
                "decided_by": None,
                "delegated_by": None,
                "reason": "human_timeout",
                "spent_rub": 0.0,
            },
        )
    if mode != "delegate":
        why = "нет бюджета" if budget is None else budget.refuse_reason(cost_estimate_rub)
        if why:
            ev = {
                "decided_by": None,
                "delegated_by": delegated_by,
                "spent_rub": 0.0,
                "human_message": why,
            }
            if mode == "fail":
                return "WAIT", {**ev, "reason": "delegation_disabled"}
            return (
                "SKIPPED(budget)",
                {
                    **ev,
                    "decided_by": "critic",
                    "reason": "delegated_costly_exhausted",
                },
            )
    eff = "RUN" if critic_accepts else "SKIPPED(critic_rejected)"
    return (
        eff,
        {
            "decided_by": "critic",
            "delegated_by": delegated_by,
            "reason": f"critic:{mode}",
            "spent_rub": 0.0,
            "cost_estimate_rub": cost_estimate_rub,
        },
    )


def settle(event: dict[str, Any], budget: DelegatedBudget | None, actual_rub: float):
    """After the attempt: debit actual spend (if budgeted) and record it for the report."""
    if budget is not None and event.get("decided_by") == "critic":
        budget.debit(actual_rub, event.get("cost_estimate_rub", 0.0))
    event["spent_rub"] = actual_rub
    return event


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
