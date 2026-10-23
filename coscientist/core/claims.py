"""Runtime claim projection for verdict() / dispositions (M1 PR8)."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from coscientist.core.domain import (
    VALID_PROVENANCE,
    Provenance,
    Study,
    worst_provenance,
)
from coscientist.core.graph import GraphStore
from coscientist.core.ids import hypothesis_node_id, question_node_id
from coscientist.core.rules import (
    DEFAULT_TESTS_INTENT,
    TestsIntent,
    claim_stance,
    verdict,
)

# Agent-supplied fields only. Counting controls (criterion_status, frozen,
# evidence_kind, lit_own_criterion, intent, R17, accepted) are core-derived.
CLAIM_ATTR_WHITELIST: frozenset[str] = frozenset(
    {
        "target",
        "checked_against",
        "reason",
        "premise_idx",
    }
)

# Core-owned attrs written by Executor (never agent whitelist).
_CORE_CLAIM_ATTRS: frozenset[str] = frozenset(
    {
        "provenance",
        "attempt_id",
        "attempt_outcome",
        "criterion_status",
        "evidence_kind",
        "frozen",
        "lit_own_criterion",
        "accepted",
        "artifact_id",
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

    criterion_status: str = "unassessed"
    accepted: bool = False
    evidence_kind: str = "computational"
    provenance: str = "stub"
    frozen: bool = False
    attempt_outcome: str = "SUCCESS"
    attempt_id: str = ""
    artifact_id: str = ""
    lit_own_criterion: bool = False
    intent: TestsIntent = DEFAULT_TESTS_INTENT
    criterion_frozen_at_seq: int | None = None
    first_cited_evidence_seq: int | None = None
    confirming_evidence_ids: set[str] | frozenset[str] | list[str] | None = None
    threshold_source_evidence_ids: set[str] | frozenset[str] | list[str] | None = None
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
    """Copy only allowed agent fields (never stance / counting controls)."""
    return {k: v for k, v in raw.items() if k in CLAIM_ATTR_WHITELIST}


def producer_attempt_failed(
    attempt_id: str | None,
    *,
    results: Mapping[str, Mapping[str, Any]] | None,
) -> bool:
    """True when the producing attempt is known FAILED (bind-time guard)."""
    if not attempt_id or not results:
        return False
    row = results.get(attempt_id)
    if not row:
        return False
    return str(row.get("outcome") or "") == "FAILED"


def _coerce_str(val: Any, *, allow_empty: bool = False) -> str | None:
    if val is None:
        return "" if allow_empty else None
    if isinstance(val, bool):
        return None
    if isinstance(val, (int, float)) and not isinstance(val, bool):
        return None
    s = str(val).strip()
    if not s and not allow_empty:
        return None
    return s


def _coerce_bool(val: Any) -> bool | None:
    if isinstance(val, bool):
        return val
    return None


def _coerce_optional_int(val: Any) -> int | None | Literal[False]:
    """None if absent; int if valid; False if present but garbage."""
    if val is None:
        return None
    if isinstance(val, bool):
        return False
    if isinstance(val, int):
        return val
    if isinstance(val, str) and val.strip().lstrip("-").isdigit():
        return int(val.strip())
    return False


def _coerce_id_set(val: Any) -> frozenset[str] | None | Literal[False]:
    """None if absent; frozenset if valid; False if present but garbage."""
    if val is None:
        return None
    if isinstance(val, (str, bytes, bytearray, Mapping)):
        return False
    if isinstance(val, (int, float, bool)):
        return False
    try:
        return frozenset(str(x) for x in val)
    except TypeError:
        return False


def intent_from_tests_edge(
    graph: GraphStore | None,
    *,
    study_id: str,
    target_logical: str,
    target_kind: str,
) -> TestsIntent:
    """Read ``tests.intent`` from experiment→hypothesis edge (#12 / DESIGN)."""
    if graph is None or target_kind != "hypothesis" or not target_logical:
        return DEFAULT_TESTS_INTENT
    hid = (
        target_logical
        if target_logical.startswith(f"{study_id}:hypothesis:")
        else hypothesis_node_id(study_id, target_logical)
    )
    for e in graph.all_edges():
        if e.kind != "tests" or e.dst_id != hid:
            continue
        raw = (e.attrs or {}).get("intent", DEFAULT_TESTS_INTENT)
        if raw in ("confirm", "refute"):
            return raw  # type: ignore[return-value]
    return DEFAULT_TESTS_INTENT


def resolve_existing_target(
    study_id: str,
    target: str,
    graph: GraphStore | None,
) -> tuple[str, Literal["question", "hypothesis"]] | None:
    """Map ``target`` to an **existing** graph node; never create / never H* guess."""
    if not target or graph is None:
        return None
    if target.startswith(f"{study_id}:question:"):
        if graph.get_node(target) is not None:
            return target, "question"
        return None
    if target.startswith(f"{study_id}:hypothesis:"):
        if graph.get_node(target) is not None:
            return target, "hypothesis"
        return None
    qid = question_node_id(study_id, target)
    if graph.get_node(qid) is not None:
        return qid, "question"
    hid = hypothesis_node_id(study_id, target)
    if graph.get_node(hid) is not None:
        return hid, "hypothesis"
    return None


def namespaced_claim_id(study_id: str, attempt_id: str, agent_key: str) -> str | None:
    """Claim ids are namespaced per attempt; foreign full ids are rejected."""
    from coscientist.core.ids import claim_node_id

    key = str(agent_key)
    prefix = f"{study_id}:claim:"
    ns = f"{prefix}{attempt_id}:"
    if key.startswith(prefix):
        if key.startswith(ns):
            return key
        return None  # foreign / unscoped agent-chosen full id
    # Short key → namespace under this attempt.
    safe = key.replace("/", "_")
    return claim_node_id(study_id, f"{attempt_id}:{safe}")


def runtime_claim_from_node(
    node: Any,
    *,
    study_id: str,
    results: Mapping[str, Mapping[str, Any]] | None = None,
    graph: GraphStore | None = None,
) -> tuple[RuntimeClaim | None, str | None]:
    """Build a RuntimeClaim; on garbage attrs return ``(None, reason)`` — never raise."""
    try:
        attrs = dict(node.attrs or {})
        aid = _coerce_str(attrs.get("attempt_id"), allow_empty=True) or ""
        outcome = _coerce_str(attrs.get("attempt_outcome"), allow_empty=True) or ""
        if results and aid and aid in results:
            outcome = str(results[aid].get("outcome") or outcome)
        if outcome == "FAILED" or producer_attempt_failed(aid, results=results):
            return None, "producer_failed"
        if not outcome:
            outcome = "SUCCESS"

        prov = attrs.get("provenance")
        if prov not in VALID_PROVENANCE:
            prov = "stub"

        # Core-derived counting fields (not agent whitelist).
        status = _coerce_str(attrs.get("criterion_status"))
        if status is None or status not in ("met", "not_met", "unassessed"):
            return None, "bad_criterion_status"
        accepted = _coerce_bool(attrs.get("accepted"))
        if accepted is None:
            return None, "bad_accepted"
        frozen = _coerce_bool(attrs.get("frozen"))
        if frozen is None:
            return None, "bad_frozen"
        ek = _coerce_str(attrs.get("evidence_kind"))
        if ek is None or ek not in (
            "computational",
            "experimental",
            "literature",
            "expert",
        ):
            return None, "bad_evidence_kind"
        lit_own = _coerce_bool(attrs.get("lit_own_criterion"))
        if lit_own is None:
            lit_own = False

        conf = _coerce_id_set(attrs.get("confirming_evidence_ids"))
        if conf is False:
            return None, "bad_confirming_evidence_ids"
        thr = _coerce_id_set(attrs.get("threshold_source_evidence_ids"))
        if thr is False:
            return None, "bad_threshold_source_evidence_ids"
        frozen_seq = _coerce_optional_int(attrs.get("criterion_frozen_at_seq"))
        if frozen_seq is False:
            return None, "bad_criterion_frozen_at_seq"
        first_ev = _coerce_optional_int(attrs.get("first_cited_evidence_seq"))
        if first_ev is False:
            return None, "bad_first_cited_evidence_seq"

        target = _coerce_str(attrs.get("target"), allow_empty=True) or ""
        about = []
        target_kind = "question"
        if graph is not None:
            about = [e for e in graph.edges_from(node.node_id) if e.kind == "about"]
            if len(about) != 1:
                return None, "about_cardinality"
            dst = about[0].dst_id
            dst_node = graph.get_node(dst)
            if dst_node is None:
                return None, "about_dst_missing"
            if dst_node.kind == "question":
                target_kind = "question"
                target = _logical_id_from_node(dst, study_id, "question")
            elif dst_node.kind == "hypothesis":
                target_kind = "hypothesis"
                target = _logical_id_from_node(dst, study_id, "hypothesis")
            else:
                return None, "about_dst_kind"

        intent = intent_from_tests_edge(
            graph,
            study_id=study_id,
            target_logical=target,
            target_kind=target_kind,
        )
        art = _coerce_str(attrs.get("artifact_id"), allow_empty=True) or node.node_id
        return (
            RuntimeClaim(
                criterion_status=status,
                accepted=accepted,
                evidence_kind=ek,
                provenance=str(prov),
                frozen=frozen,
                attempt_outcome=outcome,
                attempt_id=aid or node.node_id,
                artifact_id=art,
                lit_own_criterion=lit_own,
                intent=intent,
                criterion_frozen_at_seq=frozen_seq,
                first_cited_evidence_seq=first_ev,
                confirming_evidence_ids=conf,
                threshold_source_evidence_ids=thr,
                target=target,
                node_id=node.node_id,
            ),
            None,
        )
    except Exception:  # noqa: BLE001 — totality: never raise into finalize/view
        return None, "coerce_error"


def _logical_id_from_node(node_id: str, study_id: str, kind: str) -> str:
    prefix = f"{study_id}:{kind}:"
    if node_id.startswith(prefix):
        return node_id[len(prefix) :]
    return node_id


def filtered_claims_by_target(
    graph: GraphStore,
    *,
    study_id: str,
    results: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, list[RuntimeClaim]]:
    """Group claims by target; about cardinality + coerce applied uniformly.

    Used by both ``dispositions`` and ``compute_verdict_snapshots`` so the
    P2 snapshot and the report cannot diverge.
    """
    grouped: dict[str, list[RuntimeClaim]] = defaultdict(list)
    for node in graph.nodes_by_kind("claim"):
        rc, _reason = runtime_claim_from_node(
            node, study_id=study_id, results=results, graph=graph
        )
        if rc is None or not rc.target:
            continue
        grouped[rc.target].append(rc)
    return dict(grouped)


def claims_missing_about(graph: GraphStore) -> list[str]:
    """Claim node ids that lack the required out-1 ``about`` edge."""
    missing: list[str] = []
    for node in graph.nodes_by_kind("claim"):
        about = [e for e in graph.edges_from(node.node_id) if e.kind == "about"]
        if len(about) != 1:
            missing.append(node.node_id)
    return missing


def compute_verdict_snapshots(
    graph: GraphStore,
    *,
    study_id: str,
    study: Study | None = None,
    results: Mapping[str, Mapping[str, Any]] | None = None,
    k: int = 1,
    any_experiment_done: bool = False,
    refs: Sequence[str] | None = None,
) -> dict[str, dict[str, Any]]:
    """ref → snapshot dict used for ``InputSpec.kind=verdict`` P2 digests.

    When ``refs`` is set, only those logical ids are computed (scoped bind).
    """
    k_ref = study.refutations_needed if study is not None else 1
    priority = study.refutation_priority if study is not None else False
    by_target = filtered_claims_by_target(
        graph, study_id=study_id, results=results
    )
    if refs is not None:
        want = set(refs)
        by_target = {r: by_target[r] for r in want if r in by_target}
    out: dict[str, dict[str, Any]] = {}
    for ref, claims in by_target.items():
        v, by_lit = verdict(
            claims,
            any_experiment_done,
            k,
            refutations_needed=k_ref,
            refutation_priority=priority,
        )
        provs: list[Provenance] = [
            c.provenance  # type: ignore[misc]
            for c in claims
            if c.counts() and c.provenance in VALID_PROVENANCE
        ]
        source = worst_provenance(*provs) if provs else "stub"
        out[ref] = {
            "ref": ref,
            "verdict": v,
            "by_literature": by_lit,
            "provenance": source,
            "refutations_needed": k_ref,
            "refutation_priority": priority,
            "k": k,
        }
    return out


def disposition_from_verdict(v: str) -> str | None:
    """Map RM verdict → question Disposition value; None if untested."""
    if v == "untested":
        return None
    return _VERDICT_TO_DISPOSITION.get(v)


def hypothesis_verdicts(
    graph: GraphStore,
    *,
    study_id: str,
    study: Study | None = None,
    results: Mapping[str, Mapping[str, Any]] | None = None,
    k: int = 1,
    any_experiment_done: bool = False,
) -> dict[str, dict[str, Any]]:
    """Verdicts for hypothesis targets (for report payload)."""
    snaps = compute_verdict_snapshots(
        graph,
        study_id=study_id,
        study=study,
        results=results,
        k=k,
        any_experiment_done=any_experiment_done,
    )
    out: dict[str, dict[str, Any]] = {}
    for ref, snap in snaps.items():
        hid = hypothesis_node_id(study_id, ref)
        if graph.get_node(hid) is not None or ref.startswith(f"{study_id}:hypothesis:"):
            out[ref] = snap
        else:
            # Only include if any claim about this ref points at a hypothesis node.
            for c in filtered_claims_by_target(
                graph, study_id=study_id, results=results
            ).get(ref, []):
                about = [
                    e for e in graph.edges_from(c.node_id) if e.kind == "about"
                ]
                if about and (n := graph.get_node(about[0].dst_id)) and n.kind == "hypothesis":
                    out[ref] = snap
                    break
    return out
