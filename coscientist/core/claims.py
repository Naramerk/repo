"""Runtime claim projection for verdict() / dispositions (M1 PR8)."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

from coscientist.core.domain import VALID_PROVENANCE, Provenance, Study
from coscientist.core.graph import GraphStore
from coscientist.core.ids import hypothesis_node_id, question_node_id
from coscientist.core.rules import (
    DEFAULT_TESTS_INTENT,
    TestsIntent,
    claim_stance,
    verdict,
)

# Agent-supplied fields allowed onto claim node attrs (not stance — computed).
CLAIM_ATTR_WHITELIST: frozenset[str] = frozenset(
    {
        "target",
        "checked_against",
        "reason",
        "criterion_status",
        "intent",
        "evidence_kind",
        "frozen",
        "lit_own_criterion",
        "premise_idx",
        "artifact_id",
        "criterion_frozen_at_seq",
        "first_cited_evidence_seq",
        "confirming_evidence_ids",
        "threshold_source_evidence_ids",
    }
)

_VERDICT_TO_DISPOSITION = {
    "supported": "answered",
    "refuted": "refuted",
    "survived": "survived",
    "inconclusive": "inconclusive",
}


@dataclass
class RuntimeClaim:
    """Minimal claim surface for ``rules.verdict`` / R17."""

    criterion_status: str = "met"
    accepted: bool = True
    evidence_kind: str = "computational"
    provenance: str = "real"
    frozen: bool = True
    attempt_outcome: str = "SUCCESS"
    attempt_id: str = ""
    artifact_id: str = ""
    lit_own_criterion: bool = False
    intent: TestsIntent = DEFAULT_TESTS_INTENT
    criterion_frozen_at_seq: int | None = None
    first_cited_evidence_seq: int | None = None
    confirming_evidence_ids: frozenset[str] | None = None
    threshold_source_evidence_ids: frozenset[str] | None = None
    target: str = ""
    node_id: str = ""

    def __post_init__(self) -> None:
        if self.intent not in ("confirm", "refute"):
            self.intent = DEFAULT_TESTS_INTENT

    @property
    def stance(self) -> str:
        try:
            return claim_stance(self.criterion_status)
        except KeyError:
            return "inconclusive"

    @property
    def is_lit(self) -> bool:
        return self.evidence_kind == "literature"

    def counts(self) -> bool:
        ok = self.evidence_kind in ("computational", "experimental") or (
            self.is_lit and self.lit_own_criterion
        )
        return (
            self.accepted
            and ok
            and self.frozen
            and self.attempt_outcome == "SUCCESS"
        )


def whitelist_claim_attrs(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Copy only allowed agent fields (never stance / provenance / attempt_id)."""
    return {k: v for k, v in raw.items() if k in CLAIM_ATTR_WHITELIST}


def producer_attempt_failed(
    attempt_id: str | None,
    *,
    results: Mapping[str, Mapping[str, Any]] | None,
    attrs_outcome: str | None = None,
) -> bool:
    """True when the producing attempt is known FAILED (bind-time guard)."""
    if attrs_outcome == "FAILED":
        return True
    if not attempt_id or not results:
        return False
    row = results.get(attempt_id)
    if not row:
        return False
    return str(row.get("outcome") or "") == "FAILED"


def runtime_claim_from_node(
    node: Any,
    *,
    results: Mapping[str, Mapping[str, Any]] | None = None,
) -> RuntimeClaim | None:
    """Build a RuntimeClaim from a graph claim node; None if unusable."""
    attrs = dict(node.attrs or {})
    aid = str(attrs.get("attempt_id") or "")
    outcome = str(attrs.get("attempt_outcome") or "")
    if results and aid and aid in results:
        outcome = str(results[aid].get("outcome") or outcome)
    if outcome == "FAILED" or producer_attempt_failed(
        aid, results=results, attrs_outcome=outcome or None
    ):
        return None
    if not outcome:
        outcome = "SUCCESS"
    prov = attrs.get("provenance")
    if prov not in VALID_PROVENANCE:
        prov = "stub"
    intent_raw = attrs.get("intent") or DEFAULT_TESTS_INTENT
    intent: TestsIntent = (
        intent_raw if intent_raw in ("confirm", "refute") else DEFAULT_TESTS_INTENT
    )
    frozen = attrs.get("frozen", True)
    if not isinstance(frozen, bool):
        frozen = True
    conf = attrs.get("confirming_evidence_ids")
    thr = attrs.get("threshold_source_evidence_ids")
    return RuntimeClaim(
        criterion_status=str(attrs.get("criterion_status") or "met"),
        accepted=bool(attrs.get("accepted", True)),
        evidence_kind=str(attrs.get("evidence_kind") or "computational"),
        provenance=str(prov),
        frozen=frozen,
        attempt_outcome=outcome,
        attempt_id=aid or node.node_id,
        artifact_id=str(attrs.get("artifact_id") or node.node_id),
        lit_own_criterion=bool(attrs.get("lit_own_criterion", False)),
        intent=intent,
        criterion_frozen_at_seq=attrs.get("criterion_frozen_at_seq"),
        first_cited_evidence_seq=attrs.get("first_cited_evidence_seq"),
        confirming_evidence_ids=frozenset(conf) if conf is not None else None,
        threshold_source_evidence_ids=frozenset(thr) if thr is not None else None,
        target=str(attrs.get("target") or ""),
        node_id=node.node_id,
    )


def _logical_id_from_node(node_id: str, study_id: str, kind: str) -> str:
    prefix = f"{study_id}:{kind}:"
    if node_id.startswith(prefix):
        return node_id[len(prefix) :]
    return node_id


def claims_by_target(
    graph: GraphStore,
    *,
    study_id: str,
    results: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, list[RuntimeClaim]]:
    """Group counting-ready claims by logical target id (question or hypothesis)."""
    grouped: dict[str, list[RuntimeClaim]] = defaultdict(list)
    for node in graph.nodes_by_kind("claim"):
        rc = runtime_claim_from_node(node, results=results)
        if rc is None:
            continue
        target_logical = ""
        about = [e for e in graph.edges_from(node.node_id) if e.kind == "about"]
        if about:
            dst = about[0].dst_id
            dst_node = graph.get_node(dst)
            if dst_node is not None:
                if dst_node.kind == "question":
                    target_logical = _logical_id_from_node(dst, study_id, "question")
                elif dst_node.kind == "hypothesis":
                    target_logical = _logical_id_from_node(dst, study_id, "hypothesis")
            if not target_logical:
                target_logical = dst
        if not target_logical and rc.target:
            target_logical = rc.target
            # Strip full node id prefixes if agent stored them.
            for kind in ("question", "hypothesis"):
                prefix = f"{study_id}:{kind}:"
                if target_logical.startswith(prefix):
                    target_logical = target_logical[len(prefix) :]
                    break
        if target_logical:
            grouped[target_logical].append(rc)
    return dict(grouped)


def claims_missing_about(graph: GraphStore) -> list[str]:
    """Claim node ids that lack the required out-1 ``about`` edge."""
    missing: list[str] = []
    for node in graph.nodes_by_kind("claim"):
        about = [e for e in graph.edges_from(node.node_id) if e.kind == "about"]
        if len(about) != 1:
            missing.append(node.node_id)
    return missing


def resolve_about_target_id(
    study_id: str,
    target: str,
    graph: GraphStore | None,
) -> tuple[str, Literal["question", "hypothesis"]]:
    """Map agent ``target`` ref to a graph node id + kind."""
    if target.startswith(f"{study_id}:question:"):
        return target, "question"
    if target.startswith(f"{study_id}:hypothesis:"):
        return target, "hypothesis"
    qid = question_node_id(study_id, target)
    hid = hypothesis_node_id(study_id, target)
    if graph is not None:
        if graph.get_node(qid) is not None:
            return qid, "question"
        if graph.get_node(hid) is not None:
            return hid, "hypothesis"
        # Prefer hypothesis when target looks like H*; else question.
    if target.upper().startswith("H") and not target.upper().startswith("Q"):
        return hid, "hypothesis"
    return qid, "question"


def compute_verdict_snapshots(
    graph: GraphStore,
    *,
    study_id: str,
    study: Study | None = None,
    results: Mapping[str, Mapping[str, Any]] | None = None,
    k: int = 1,
    any_experiment_done: bool = True,
) -> dict[str, dict[str, Any]]:
    """ref → snapshot dict used for ``InputSpec.kind=verdict`` P2 digests."""
    k_ref = study.refutations_needed if study is not None else 1
    priority = study.refutation_priority if study is not None else False
    by_target = claims_by_target(graph, study_id=study_id, results=results)
    out: dict[str, dict[str, Any]] = {}
    for ref, claims in by_target.items():
        v, by_lit = verdict(
            claims,
            any_experiment_done,
            k,
            refutations_needed=k_ref,
            refutation_priority=priority,
        )
        provs = [c.provenance for c in claims if c.counts()]
        rank = {"real": 0, "partial": 1, "stub": 2}
        source: Provenance = (
            max(provs, key=lambda p: rank.get(p, 2))  # type: ignore[arg-type]
            if provs
            else "real"
        )
        out[ref] = {
            "ref": ref,
            "verdict": v,
            "by_literature": by_lit,
            "provenance": source,
            "refutations_needed": k_ref,
            "refutation_priority": priority,
        }
    return out


def disposition_from_verdict(v: str) -> str | None:
    """Map RM verdict → question Disposition value; None if untested."""
    if v == "untested":
        return None
    return _VERDICT_TO_DISPOSITION.get(v)
