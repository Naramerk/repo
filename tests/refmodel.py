"""Independent RESEARCH_MODEL oracle (no coscientist.* imports). Compared via impl=[refmodel,core]."""
from dataclasses import dataclass, field
from pathlib import Path
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
    @property
    def stance(self):
        return claim_stance(self.criterion_status)
    @property
    def is_lit(self):
        return self.evidence_kind == "literature"
    def counts(self):
        ok = self.evidence_kind in ("computational", "experimental") or (self.is_lit and self.lit_own_criterion)
        return self.accepted and ok and self.frozen and self.attempt_outcome == "SUCCESS"

def verdict(claims, any_experiment_done, k=1):
    acc = [c for c in claims if c.accepted]
    if not acc and not any_experiment_done:
        return "untested", False
    q = [c for c in acc if c.counts()]
    sup, ref = [c for c in q if c.stance == "supports"], [c for c in q if c.stance == "refutes"]
    dist = lambda cs: min(len({c.attempt_id for c in cs}), len({c.artifact_id for c in cs})) if cs else 0
    if sup and ref: v = "inconclusive"
    elif dist(sup) >= k: v = "supported"
    elif dist(ref) >= k: v = "refuted"
    else: v = "inconclusive"
    if v in ("supported", "refuted") and any(c.provenance != "real" for c in q):
        v = "inconclusive"
    d = sup + ref
    return v, bool(d) and all(c.is_lit for c in d)

def study_fulfilled(question_verdicts, all_required_success):
    return all_required_success and all(v != "untested" for v in question_verdicts.values())

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
