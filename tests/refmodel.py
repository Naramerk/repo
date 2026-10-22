"""Executable reference of the RESEARCH_MODEL.md rules (validator level, no runtime).

The M0 core (PR #1) must agree with this module; tests here pin the semantics.
"""
from dataclasses import dataclass, field

import gen_model_tables as g

SCHEMA = g.load()
PAIRS = {e["name"]: {tuple(p) for p in e["pairs"]} for e in SCHEMA["edges"]}


def edge_allowed(name, src_kind, dst_kind):
    return (src_kind, dst_kind) in PAIRS.get(name, set())


@dataclass
class Graph:
    nodes: dict = field(default_factory=dict)       # id -> {"kind": ..., **fields}
    edges: list = field(default_factory=list)       # (name, src, dst, attrs)

    def add(self, nid, kind, **f):
        self.nodes[nid] = {"kind": kind, **f}
        return nid

    def link(self, name, src, dst, **attrs):
        if not edge_allowed(name, self.nodes[src]["kind"], self.nodes[dst]["kind"]):
            raise ValueError(f"edge {name}: {self.nodes[src]['kind']} -> {self.nodes[dst]['kind']} not allowed")
        self.edges.append((name, src, dst, attrs))

    def out(self, name, src):
        return [d for n, s, d, _ in self.edges if n == name and s == src]

    def inc(self, name, dst):
        return [s for n, s, d, _ in self.edges if n == name and d == dst]

    def of(self, kind):
        return [k for k, v in self.nodes.items() if v["kind"] == kind]

    def add_paper(self, producer, doi_or_url, attempt_n=1):
        """Dedupe identity of artifact(role=paper) is DOI/URL (normalized)."""
        key = doi_or_url.strip().lower().removeprefix("https://doi.org/")
        for nid, v in self.nodes.items():
            if v["kind"] == "artifact" and v.get("role") == "paper" and v["doi_or_url"] == key:
                return nid                                  # same node, producer edge kept (in = 1..1)
        nid = self.add(f"A-{key}", "artifact", role="paper", doi_or_url=key)
        self.link("produced", producer, nid, attempt_n=attempt_n)
        return nid


class PlanInvalid(ValueError):
    pass


def validate_plan(gr, plan_id):
    """PlanValidator: RT1 (each experiment in exactly one plan) + question coverage."""
    exps = gr.out("plans", plan_id)
    for x in gr.of("experiment"):
        if len(gr.inc("plans", x)) != 1:
            raise PlanInvalid(f"RT1: {x} linked to {len(gr.inc('plans', x))} plans")
    for x in exps:
        if not gr.out("tests", x) and not gr.out("addresses", x):
            raise PlanInvalid(f"{x}: needs tests or addresses")
    covered = set()
    for x in exps:
        covered |= set(gr.out("addresses", x))
        for h in gr.out("tests", x):
            covered |= set(gr.out("addresses", h))
    missing = set(gr.of("question")) - covered
    if missing:
        raise PlanInvalid(f"questions not covered by LIT/EXP: {sorted(missing)}")


STANCE = {"met": "supports", "not_met": "refutes", "unassessed": "inconclusive"}


def claim_stance(criterion_status):
    return STANCE[criterion_status]


@dataclass
class Claim:
    criterion_status: str                 # met | not_met | unassessed (frozen criterion)
    accepted: bool = True
    evidence_kind: str = "computational"
    provenance: str = "real"
    frozen: bool = True
    attempt_outcome: str = "SUCCESS"
    attempt_id: str = "X1#1"
    artifact_id: str = "A1"
    lit_own_criterion: bool = False       # evidence from literature_search with its own criterion

    @property
    def stance(self):
        return claim_stance(self.criterion_status)

    @property
    def is_lit(self):
        return self.evidence_kind == "literature"

    def counts(self):
        ok_kind = self.evidence_kind in ("computational", "experimental") or (self.is_lit and self.lit_own_criterion)
        return self.accepted and ok_kind and self.frozen and self.attempt_outcome == "SUCCESS"


def verdict(claims, any_experiment_done, k=1):
    """Hypothesis verdict, or question answer when there are no hypotheses. Returns (verdict, by_literature)."""
    acc = [c for c in claims if c.accepted]
    if not acc and not any_experiment_done:
        return "untested", False
    q = [c for c in acc if c.counts()]
    sup = [c for c in q if c.stance == "supports"]
    ref = [c for c in q if c.stance == "refutes"]

    def distinct(cs):
        return min(len({c.attempt_id for c in cs}), len({c.artifact_id for c in cs})) if cs else 0

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


def study_fulfilled(question_verdicts, all_required_success):
    return all_required_success and all(v != "untested" for v in question_verdicts.values())


def costly_gate(mode, answer, cost_rub=0.0, budget_left_rub=0.0, critic_accepts=True):
    """HITL gate costly_or_nonreal. answer: approve | reject | delegate | None (timeout).

    Returns (task_effect, event) where event always carries decided_by, reason and spent_rub.
    """
    if answer in ("approve", "reject"):
        eff = "RUN" if answer == "approve" else "SKIPPED(human_rejected)"
        return eff, {"decided_by": "human", "reason": answer, "spent_rub": 0.0}
    delegated_by = "human" if answer == "delegate" else "timeout"
    if mode == "fail":
        if answer == "delegate":                    # delegation disabled in this mode: gate stays open
            return "WAIT", {"decided_by": None, "reason": "delegation_disabled", "spent_rub": 0.0}
        return "FAILED(human_timeout)", {"decided_by": None, "reason": "human_timeout", "spent_rub": 0.0}
    if mode == "delegate_capped" and cost_rub > budget_left_rub:
        return "SKIPPED(budget)", {"decided_by": "critic", "delegated_by": delegated_by,
                                   "reason": "delegated_costly_exhausted", "spent_rub": 0.0}
    if mode not in ("delegate_capped", "delegate"):
        raise ValueError(mode)
    eff = "RUN" if critic_accepts else "SKIPPED(critic_rejected)"
    spent = cost_rub if critic_accepts else 0.0
    return eff, {"decided_by": "critic", "delegated_by": delegated_by, "reason": f"critic:{mode}", "spent_rub": spent}
