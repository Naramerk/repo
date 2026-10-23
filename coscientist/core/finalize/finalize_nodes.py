"""Deterministic conclusion/report node ids derived from StudyFinalized payload."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from datetime import datetime
from typing import Any

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
    study_id: str, question_dispositions: Mapping[str, Any]
) -> list[str]:
    ids = [
        conclusion_id_for_question(study_id, qid)
        for qid in sorted(question_dispositions)
    ]
    ids.append(conclusion_id_for_study(study_id))
    return ids


def _refute_unresolved_not_tested(
    payload: Mapping[str, Any],
) -> tuple[set[str], bool]:
    """Question ids (and study-level flag) marked not-tested via refute_unresolved."""
    hv = payload.get("hypothesis_verdicts") or {}
    qids: set[str] = set()
    any_nt = False
    if isinstance(hv, Mapping):
        for row in hv.values():
            if not isinstance(row, Mapping):
                continue
            rule = str(row.get("rule") or "")
            if rule == "refute_unresolved" or row.get("not_tested") is True:
                any_nt = True
                qid = row.get("question_id")
                if qid:
                    qids.add(str(qid))
    return qids, any_nt


def graph_rows_from_finalized(
    study_id: str,
    payload: Mapping[str, Any],
    *,
    created_at: datetime | str,
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    """Build graph_nodes / graph_edges rows from a StudyFinalized payload."""
    nodes: dict[str, dict[str, Any]] = {}
    edges: dict[str, dict[str, Any]] = {}
    qdisp = payload.get("question_dispositions") or {}
    term = str(payload.get("terminal_reason") or "completed")
    ts = created_at if isinstance(created_at, str) else created_at.isoformat()
    not_tested_qids, any_refute_unresolved = _refute_unresolved_not_tested(payload)

    for qid, d in sorted(qdisp.items()):
        if not isinstance(d, dict):
            d = {}
        q_node = question_node_id(study_id, qid)
        nodes[q_node] = {
            "node_id": q_node,
            "kind": "question",
            "study_id": study_id,
            "ref_id": qid,
            "attrs": {"formulation": d.get("formulation", "")},
            "created_at": ts,
        }
        cid = conclusion_id_for_question(study_id, qid)
        # Explicit not-tested marker when rule=refute_unresolved (not just
        # verdict=inconclusive) so conclusion consumers see the gap.
        if qid in not_tested_qids or d.get("rule") == "refute_unresolved":
            synthesis = "not_tested:refute_unresolved"
            c_attrs: dict[str, Any] = {
                "synthesis": synthesis,
                "validity_bounds": "core PR2: no counting claims",
                "question_id": qid,
                "not_tested": True,
                "rule": "refute_unresolved",
            }
        else:
            c_attrs = {
                "synthesis": f"not_attempted:{d.get('reason')}",
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
    refusal = payload.get("refusal")
    synthesis = f"terminal:{term}"
    if refusal:
        synthesis = f"terminal:{term}; refused:{refusal}"
    study_attrs: dict[str, Any] = {"synthesis": synthesis, "validity_bounds": "study"}
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

    art = payload.get("report_artifact_id")
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
