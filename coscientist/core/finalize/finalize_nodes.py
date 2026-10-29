"""Deterministic conclusion/report node ids derived from StudyFinalized payload."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from datetime import datetime

from coscientist.core.events.ids import question_node_id, study_node_id

__all__ = [
    "conclusion_id_for_question",
    "conclusion_id_for_study",
    "report_node_id_for",
    "question_node_id",
    "study_node_id",
    "conclusion_ids_for",
    "graph_rows_from_finalized",
]


def conclusion_id_for_question(study_id: str, question_id: str) -> str:
    digest = hashlib.sha256(f"{study_id}:q:{question_id}".encode()).hexdigest()[:20]
    return f"cn_{digest}"


def conclusion_id_for_study(study_id: str) -> str:
    digest = hashlib.sha256(f"{study_id}:study".encode()).hexdigest()[:20]
    return f"cn_{digest}"


def report_node_id_for(study_id: str) -> str:
    digest = hashlib.sha256(f"{study_id}:report".encode()).hexdigest()[:20]
    return f"rp_{digest}"


def conclusion_ids_for(
    study_id: str, question_dispositions: Mapping[str, object]
) -> list[str]:
    ids = [
        conclusion_id_for_question(study_id, qid)
        for qid in sorted(question_dispositions)
    ]
    ids.append(conclusion_id_for_study(study_id))
    return ids


def _mapping_field(payload: Mapping[str, object], key: str) -> Mapping[str, object]:
    """Read a mapping field; absent/non-mapping → empty (legacy ``.get(k) or {}``)."""
    if key not in payload:
        return {}
    raw = payload[key]
    if isinstance(raw, Mapping):
        return raw
    return {}


def _refute_unresolved_not_tested(
    payload: Mapping[str, object],
) -> tuple[set[str], bool]:
    """Question ids (and study-level flag) marked not-tested via refute_unresolved."""
    hv = _mapping_field(payload, "hypothesis_verdicts")
    qids: set[str] = set()
    any_nt = False
    for row in hv.values():
        if not isinstance(row, Mapping):
            continue
        rule_raw = row["rule"] if "rule" in row else ""
        rule = str(rule_raw) if rule_raw else ""
        not_tested = row["not_tested"] is True if "not_tested" in row else False
        if rule == "refute_unresolved" or not_tested:
            any_nt = True
            if "question_id" in row and row["question_id"]:
                qids.add(str(row["question_id"]))
    return qids, any_nt


def graph_rows_from_finalized(
    study_id: str,
    payload: Mapping[str, object],
    *,
    created_at: datetime | str,
) -> tuple[dict[str, dict[str, object]], dict[str, dict[str, object]]]:
    """Build graph_nodes / graph_edges rows from a StudyFinalized payload."""
    nodes: dict[str, dict[str, object]] = {}
    edges: dict[str, dict[str, object]] = {}
    qdisp = _mapping_field(payload, "question_dispositions")
    if "terminal_reason" in payload and payload["terminal_reason"]:
        term = str(payload["terminal_reason"])
    else:
        term = "completed"
    ts = created_at if isinstance(created_at, str) else created_at.isoformat()
    not_tested_qids, any_refute_unresolved = _refute_unresolved_not_tested(payload)

    for qid, d in sorted(qdisp.items()):
        # Legacy required ``dict`` specifically (other Mapping → empty attrs).
        row: Mapping[str, object] = d if isinstance(d, dict) else {}
        q_node = question_node_id(study_id, qid)
        formulation = row["formulation"] if "formulation" in row else ""
        nodes[q_node] = {
            "node_id": q_node,
            "kind": "question",
            "study_id": study_id,
            "ref_id": qid,
            "attrs": {"formulation": formulation},
            "created_at": ts,
        }
        cid = conclusion_id_for_question(study_id, qid)
        # Explicit not-tested marker when rule=refute_unresolved (not just
        # verdict=inconclusive) so conclusion consumers see the gap.
        rule = row["rule"] if "rule" in row else None
        if qid in not_tested_qids or rule == "refute_unresolved":
            synthesis = "not_tested:refute_unresolved"
            c_attrs: dict[str, object] = {
                "synthesis": synthesis,
                "validity_bounds": "core PR2: no counting claims",
                "question_id": qid,
                "not_tested": True,
                "rule": "refute_unresolved",
            }
        else:
            reason = row["reason"] if "reason" in row else None
            c_attrs = {
                "synthesis": f"not_attempted:{reason}",
                "validity_bounds": "core PR2: no counting claims",
                "question_id": qid,
            }
        nodes[cid] = {
            "node_id": cid,
            "kind": "conclusion",
            "study_id": study_id,
            "ref_id": cid,
            "attrs": c_attrs,
            "created_at": ts,
        }
        eid = f"answers:{cid}:{qid}"
        edges[eid] = {
            "edge_id": eid,
            "kind": "answers",
            "study_id": study_id,
            "src_id": cid,
            "dst_id": q_node,
            "created_at": ts,
        }

    scid = conclusion_id_for_study(study_id)
    refusal = payload["refusal"] if "refusal" in payload else None
    synthesis = f"terminal:{term}"
    if refusal:
        synthesis = f"terminal:{term}; refused:{refusal}"
    study_attrs: dict[str, object] = {"synthesis": synthesis, "validity_bounds": "study"}
    if any_refute_unresolved:
        study_attrs["synthesis"] = f"{synthesis};not_tested:refute_unresolved"
        study_attrs["not_tested"] = True
        study_attrs["rule"] = "refute_unresolved"
    nodes[scid] = {
        "node_id": scid,
        "kind": "conclusion",
        "study_id": study_id,
        "ref_id": scid,
        "attrs": study_attrs,
        "created_at": ts,
    }
    study_n = study_node_id(study_id)
    eid = f"answers:{scid}:study"
    edges[eid] = {
        "edge_id": eid,
        "kind": "answers",
        "study_id": study_id,
        "src_id": scid,
        "dst_id": study_n,
        "created_at": ts,
    }

    art = payload["report_artifact_id"] if "report_artifact_id" in payload else None
    if art:
        rid = report_node_id_for(study_id)
        nodes[rid] = {
            "node_id": rid,
            "kind": "report",
            "study_id": study_id,
            "ref_id": rid,
            "attrs": {"artifact_id": art, "format": "text/markdown"},
            "created_at": ts,
        }
    return nodes, edges
