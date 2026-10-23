"""Independent RESEARCH_MODEL oracle (no coscientist.* imports). Compared via impl=[refmodel,core]."""
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import yaml

S = yaml.safe_load((Path(__file__).resolve().parents[1] / "coscientist/schema/graph_schema.yaml").read_text())
PAIRS = {e["name"]: {tuple(p) for p in e["pairs"]} for e in S["edges"]}

def edge_allowed(name, a, b):
    return (a, b) in PAIRS.get(name, set())

@dataclass
class Graph:
    nodes: dict = field(default_factory=dict)
    edges: list = field(default_factory=list)
    def add(self, nid, kind, **f):
        self.nodes[nid] = {"kind": kind, **f}; return nid
    def link(self, name, src, dst, **attrs):
        if not edge_allowed(name, self.nodes[src]["kind"], self.nodes[dst]["kind"]):
            raise ValueError(f"edge {name} not allowed")
        self.edges.append((name, src, dst, attrs))
    def out(self, name, src):
        return [d for n, s, d, _ in self.edges if n == name and s == src]
    def inc(self, name, dst):
        return [s for n, s, d, _ in self.edges if n == name and d == dst]
    def of(self, kind):
        return [k for k, v in self.nodes.items() if v["kind"] == kind]
    def add_paper(self, producer, doi_or_url, attempt_n=1):
        key = doi_or_url.strip().lower().removeprefix("https://doi.org/")
        for nid, v in self.nodes.items():
            if v.get("kind") == "artifact" and v.get("role") == "paper" and v.get("doi_or_url") == key:
                return nid
        nid = self.add(f"A-{key}", "artifact", role="paper", doi_or_url=key)
        self.link("produced", producer, nid, attempt_n=attempt_n); return nid

class PlanInvalid(ValueError):
    pass

def validate_plan(gr, plan_id):
    for x in gr.of("experiment"):
        if len(gr.inc("plans", x)) != 1:
            raise PlanInvalid(f"RT1: {x} linked to {len(gr.inc('plans', x))} plans")
    covered = set()
    for x in gr.out("plans", plan_id):
        if not gr.out("tests", x) and not gr.out("addresses", x):
            raise PlanInvalid(f"{x}: needs tests or addresses")
        covered |= set(gr.out("addresses", x))
        for h in gr.out("tests", x):
            covered |= set(gr.out("addresses", h))
    if missing := set(gr.of("question")) - covered:
        raise PlanInvalid(f"questions not covered by LIT/EXP: {sorted(missing)}")

def claim_stance(s):
    return {"met": "supports", "not_met": "refutes", "unassessed": "inconclusive"}[s]

TestsIntent = Literal["confirm", "refute"]
DEFAULT_TESTS_INTENT: TestsIntent = "confirm"


@dataclass
class Claim:
    criterion_status: str
    accepted: bool = True
    evidence_kind: str = "computational"
    provenance: str = "real"
    frozen: bool = True
    attempt_outcome: str = "SUCCESS"
    attempt_id: str = "X1#1"
    artifact_id: str = "A1"
    lit_own_criterion: bool = False
    # tests.intent on the experiment→hypothesis edge (default confirm).
    intent: TestsIntent = DEFAULT_TESTS_INTENT
    # R17 (DESIGN_M1 §12); None = not supplied → base counts() only.
    criterion_frozen_at_seq: int | None = None
    first_cited_evidence_seq: int | None = None
    confirming_evidence_ids: frozenset | None = None
    threshold_source_evidence_ids: frozenset | None = None
    validity_ok: bool = True
    claim_outcome: str | None = None
    severe: bool | None = None
    independence_key: str = ""
    experiment_id: str = ""
    refute_capable: bool | None = None
    p_value: float | None = None
    power: float | None = None
    sesoi: float | None = 0.5  # oracle default: fail-able threshold (sesoi)
    min_power: float | None = None
    check_role: str = "prediction"

    def __post_init__(self):
        if self.intent not in ("confirm", "refute"):
            raise ValueError(f"invalid tests.intent: {self.intent!r}")
        if not self.experiment_id:
            self.experiment_id = (
                self.attempt_id.rsplit("#", 1)[0] if self.attempt_id else ""
            )
        if not self.independence_key:
            self.independence_key = self.experiment_id or self.attempt_id
        if self.claim_outcome is None and self.validity_ok:
            self.claim_outcome = {
                "met": "consistent",
                "not_met": "inconsistent",
                "unassessed": "indeterminate",
            }.get(self.criterion_status)
        if self.severe is None:
            has_thr = self.sesoi is not None or (
                self.min_power is not None and float(self.min_power) > 0.0
            )
            self.severe = (
                self.claim_outcome == "consistent"
                and self.frozen
                and self.validity_ok
                and self.provenance == "real"
                and has_thr
            )
        if self.refute_capable is None:
            self.refute_capable = bool(
                self.frozen
                and self.check_role == "prediction"
                and (
                    self.sesoi is not None
                    or (self.min_power is not None and float(self.min_power) > 0.0)
                )
            )

    @property
    def stance(self):
        return claim_stance(self.criterion_status)
    @property
    def is_lit(self):
        return self.evidence_kind == "literature"
    def counts(self):
        # PR8b: real ∧ valid ∧ consistent|inconsistent ∧ not lit; stubs excluded.
        if not (self.accepted and self.validity_ok and self.frozen):
            return False
        if self.provenance != "real":
            return False
        if self.is_lit or self.evidence_kind not in ("computational", "experimental"):
            return False
        return self.claim_outcome in ("consistent", "inconsistent")

    def counts_under_r17(self):
        """Base counts() plus R17 when freeze/evidence metadata is present.

        §12 is confirmation-only: refuting claims skip the confirming-evidence gate.
        """
        if not self.counts():
            return False
        if self.stance == "refutes":
            return True
        if (
            self.criterion_frozen_at_seq is None
            and self.first_cited_evidence_seq is None
            and self.confirming_evidence_ids is None
        ):
            return True
        if self.criterion_frozen_at_seq is None or self.first_cited_evidence_seq is None:
            return False
        if not (self.criterion_frozen_at_seq < self.first_cited_evidence_seq):
            return False
        usable = set(self.confirming_evidence_ids or ()) - set(
            self.threshold_source_evidence_ids or ()
        )
        return bool(usable)

def _distinct_by_key(claims):
    seen = set()
    out = []
    for c in claims:
        key = c.independence_key or c.attempt_id
        if key in seen:
            continue
        seen.add(key)
        out.append(c)
    return out

def verdict(
    claims,
    any_experiment_done,
    k=1,
    *,
    refutations_needed=1,
    refutation_priority=False,
    supported_needed=2,
):
    del k  # PR8 confirmations_needed — not the severe-support gate
    about = list(claims)
    if not about:
        return "untested", False
    counted = [c for c in about if c.counts_under_r17()]
    inconsistent = _distinct_by_key(
        [c for c in counted if c.claim_outcome == "inconsistent"]
    )
    consistent_severe = _distinct_by_key(
        [c for c in counted if c.claim_outcome == "consistent" and c.severe]
    )
    k_ref = max(1, int(refutations_needed))
    k_sup = max(2, int(supported_needed))
    n_inc = len(inconsistent)
    n_con = len(consistent_severe)

    def _refute_ok(c):
        if c.intent != "refute":
            return False
        if c.refute_capable is False:
            return False
        if c.refute_capable is True:
            return True
        return bool(
            c.frozen
            and c.check_role == "prediction"
            and (
                c.sesoi is not None
                or (c.min_power is not None and float(c.min_power) > 0.0)
            )
        )

    if n_inc >= k_ref and refutation_priority:
        return "refuted", False
    if n_inc >= k_ref and not refutation_priority:
        return ("inconclusive" if consistent_severe else "refuted"), False
    if n_inc > 0:
        return "inconclusive", False
    if n_con >= k_sup and any(_refute_ok(c) for c in consistent_severe):
        return "supported", False
    if n_con >= 1:
        return "survived", False
    return "inconclusive", False

def study_fulfilled(question_verdicts, all_required_success):
    return all_required_success and all(v != "untested" for v in question_verdicts.values())


# --- PR 2 finalize oracle (independent of coscientist.core) -----------------

TERMINAL_BY_TRIGGER = {
    "cancel": "cancelled",
    "budget": "budget",
    "deadline": "deadline",
    "intake": "refused",
}


def pick_finalize_trigger(
    *, cancelled=False, budget=False, refused=False, deadline=False, all_done=False
):
    """Same-tick priority: cancelled > budget > intake > deadline > all_done."""
    if cancelled:
        return "cancel"
    if budget:
        return "budget"
    if refused:
        return "intake"
    if deadline:
        return "deadline"
    if all_done:
        return "all_done"
    return None


def terminal_reason_for(trigger, *, tasks=None, question_dispositions=None):
    if trigger in TERMINAL_BY_TRIGGER:
        return TERMINAL_BY_TRIGGER[trigger]
    # all_done: human_timeout > infeasible (not from not_attempted) > completed
    for t in (tasks or {}).values():
        if isinstance(t, dict):
            spec, outcome, reason = t.get("spec") or {}, t.get("outcome"), t.get("reason")
            required = spec.get("required", True) if isinstance(spec, dict) else True
        else:
            spec = getattr(t, "spec", None)
            required = getattr(spec, "required", True) if spec is not None else True
            outcome, reason = getattr(t, "outcome", None), getattr(t, "reason", None)
        if not required:
            continue
        ov = getattr(outcome, "value", outcome)
        rv = getattr(reason, "value", reason)
        if ov == "FAILED" and rv == "human_timeout":
            return "human_timeout"
    _ = question_dispositions  # not_attempted must not yield infeasible
    return "completed"


def study_phase(*, finalized=False, all_done=False, past_effective=False):
    if finalized:
        return "closed"
    if all_done or past_effective:
        return "finalizing"
    return "running"


def not_attempted_reason(covering_outcomes):
    """covering_outcomes: list of SUCCESS|PARTIAL|FAILED|SKIPPED|None."""
    if not covering_outcomes:
        return "no_task"
    done = [o for o in covering_outcomes if o is not None]
    if not done:
        return "no_task"
    if any(o in ("SUCCESS", "PARTIAL") for o in done):
        return "no_counting_claims"
    if all(o == "FAILED" for o in done):
        return "tasks_failed"
    return "tasks_skipped"


def reject_write_after_finalized(finalized, event_type):
    if finalized and event_type != "StudyFinalized":
        return "StudyClosedError"
    return None

class DelegatedBudget:
    def __init__(self, limit_rub):
        self.limit_rub, self.spent_rub, self.blocked = float(limit_rub), 0.0, False
    @property
    def remaining_rub(self):
        return max(self.limit_rub - self.spent_rub, 0.0)
    def refuse_reason(self, estimate_rub):
        if self.blocked: return "перерасход: делегирование заблокировано"
        return "превышает остаток" if estimate_rub > self.remaining_rub else None
    def debit(self, actual_rub, estimate_rub):
        self.spent_rub += actual_rub
        self.blocked = self.blocked or actual_rub > estimate_rub
        return actual_rub

def costly_gate(mode, answer, cost_estimate_rub=0.0, budget=None, critic_accepts=True):
    """Oracle costly_or_nonreal gate.

    ``critic_accepts`` is a **stub**: no real critic agent in the oracle; True/False
    simulates accept vs reject. Real critic wiring is M2 (see docs/DESIGN.md).
    """
    if answer in ("approve", "reject"):
        return ("RUN" if answer == "approve" else "SKIPPED(human_rejected)",
                {"decided_by": "human", "delegated_by": None, "reason": answer, "spent_rub": 0.0})
    if mode not in ("fail", "delegate_capped", "delegate"): raise ValueError(mode)
    by = "human" if answer == "delegate" else "timeout"
    if mode == "fail" and answer is None:
        return ("FAILED(human_timeout)",
                {"decided_by": None, "delegated_by": None, "reason": "human_timeout", "spent_rub": 0.0})
    if mode != "delegate":
        why = "нет бюджета" if budget is None else budget.refuse_reason(cost_estimate_rub)
        if why:
            ev = {"decided_by": None, "delegated_by": by, "spent_rub": 0.0, "human_message": why}
            if mode == "fail": return "WAIT", {**ev, "reason": "delegation_disabled"}
            return ("SKIPPED(budget)", {**ev, "decided_by": "critic", "reason": "delegated_costly_exhausted"})
    # stub: critic_accepts stands in for a real critic verdict (M2)
    return (("RUN" if critic_accepts else "SKIPPED(critic_rejected)"),
            {"decided_by": "critic", "delegated_by": by, "reason": f"critic:{mode}",
             "spent_rub": 0.0, "cost_estimate_rub": cost_estimate_rub})

def settle(event, budget, actual_rub):
    if budget is not None and event.get("decided_by") == "critic":
        budget.debit(actual_rub, event.get("cost_estimate_rub", 0.0))
    event["spent_rub"] = actual_rub; return event

def check_cardinality(gr):
    bad = []
    for e in S["edges"]:
        name, card = e["name"], e["card"]
        for sk in {a for a, _ in e["pairs"]}:
            lo, hi = card.get("out_by_src", {}).get(sk, card["out"]).split("..")
            hi = None if hi == "n" else int(hi)
            for nid in gr.of(sk):
                n = len(gr.out(name, nid))
                if n < int(lo) or (hi is not None and n > hi): bad.append(f"{name} out {nid}={n}")
        lo, hi = card["in"].split(".."); hi = None if hi == "n" else int(hi)
        for dk in {b for _, b in e["pairs"]}:
            for nid in gr.of(dk):
                n = len(gr.inc(name, nid))
                if n < int(lo) or (hi is not None and n > hi): bad.append(f"{name} in {nid}={n}")
    return bad
