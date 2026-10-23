"""Minimal InputBinding / dependency resolution (DESIGN §3.4 + M1 PR7)."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from coscientist.core.digest import canonical_json, sha256_hex
from coscientist.core.domain import (
    InputBinding,
    InputSpec,
    Provenance,
    Task,
    TaskOutcome,
    TaskState,
)
from coscientist.core.graph import GraphEdge, GraphNode

PROVENANCE_RANK = {"real": 0, "partial": 1, "stub": 2}


class InputMode(StrEnum):
    USE_PARTIAL = "use_partial"
    SIMULATE = "simulate"
    FORBID = "forbid"


def worst_provenance(*values: Provenance) -> Provenance:
    """Least favorable under real < partial < stub."""
    return max(values, key=lambda p: PROVENANCE_RANK[p])


def normalize_input_mode(spec: InputSpec) -> InputMode:
    return InputMode(spec.mode)


class InputUnavailable(Exception):
    def __init__(self, reason: str = "input_unavailable") -> None:
        self.reason = reason
        super().__init__(reason)


def experiment_node_id(study_id: str, task_key: str) -> str:
    return f"{study_id}:experiment:{task_key}"


def verdict_snapshot_digest(ref: str, verdict: str) -> str:
    """Canonical digest of a verdict value at P2 (DESIGN_M1 §3)."""
    return sha256_hex(
        canonical_json({"ref": ref, "verdict": verdict, "at": "P2"})
    )


def bind_inputs(
    task: Task,
    *,
    dep_tasks: dict[str, Task],
    dep_outputs: dict[str, dict[str, str]],
    dep_provenance: dict[str, Provenance],
    known_claim_ids: set[str] | None = None,
    verdicts: dict[str, str] | None = None,
) -> tuple[list[InputBinding], Provenance]:
    """
    Resolve TaskSpec.inputs (artifact | claim | verdict).

    Raises InputUnavailable when mode=forbid and data missing / dep FAILED,
    or when claim/verdict ``ref`` is missing / unknown.
    """
    bindings: list[InputBinding] = []
    provs: list[Provenance] = ["real"]
    claims = known_claim_ids or set()
    snap = verdicts or {}
    for spec in task.spec.inputs:
        kind = spec.kind
        if kind == "claim":
            if not spec.ref:
                raise InputUnavailable("claim_ref_missing")
            if spec.ref not in claims:
                raise InputUnavailable("claim_unavailable")
            bindings.append(
                InputBinding(
                    input=spec.ref,
                    artifact_id="",
                    source="real",
                    kind="claim",
                    ref=spec.ref,
                )
            )
            continue
        if kind == "verdict":
            if not spec.ref:
                raise InputUnavailable("verdict_ref_missing")
            if spec.ref not in snap:
                raise InputUnavailable("verdict_unavailable")
            digest = verdict_snapshot_digest(spec.ref, snap[spec.ref])
            bindings.append(
                InputBinding(
                    input=spec.ref,
                    artifact_id="",
                    source="real",
                    kind="verdict",
                    ref=spec.ref,
                    digest=digest,
                )
            )
            continue

        # kind=artifact (default)
        if not spec.from_task or not spec.output:
            raise InputUnavailable("artifact_spec_incomplete")
        mode = normalize_input_mode(spec)
        dep = dep_tasks.get(spec.from_task)
        if dep is None or dep.state != TaskState.DONE:
            raise InputUnavailable()
        arts = dep_outputs.get(spec.from_task, {})
        aid = arts.get(spec.output)
        dep_prov: Provenance = dep_provenance.get(spec.from_task, "real")

        if dep.outcome == TaskOutcome.FAILED and mode == InputMode.FORBID:
            raise InputUnavailable()

        if aid and dep.outcome == TaskOutcome.SUCCESS:
            source: Provenance = "real"
        elif aid and mode in {InputMode.USE_PARTIAL, InputMode.SIMULATE}:
            source = "partial"
        elif mode == InputMode.SIMULATE:
            source = "stub"
            aid = f"stub:{spec.from_task}:{spec.output}"
        else:
            raise InputUnavailable()

        if source != "stub":
            source = worst_provenance(source, dep_prov)
        bindings.append(
            InputBinding(
                input=spec.output,
                artifact_id=aid or "",
                source=source,
                kind="artifact",
            )
        )
        provs.append(source)
    return bindings, worst_provenance(*provs)


def uses_claim_graph_events(
    *,
    study_id: str,
    task: Task,
    bindings: list[InputBinding],
    now: datetime,
    graph: Any | None = None,
) -> list[Any]:
    """Build GraphNodeUpserted (experiment) + GraphEdgeAdded (uses_claim) events.

    Claim nodes must already exist in ``graph`` (or be upserted by the caller).
    """
    from coscientist.core.events import EventEnvelope, make_idem

    claim_bindings = [b for b in bindings if b.kind == "claim" and b.ref]
    if not claim_bindings:
        return []
    exp_id = experiment_node_id(study_id, task.spec.task_key)
    ts = now.isoformat() if hasattr(now, "isoformat") else str(now)
    events: list[EventEnvelope] = []
    # Ensure experiment node exists for the edge endpoints.
    if graph is None or graph.get_node(exp_id) is None:
        node = GraphNode(
            node_id=exp_id,
            kind="experiment",
            study_id=study_id,
            ref_id=task.spec.task_key,
            attrs={},
            created_at=now if isinstance(now, datetime) else None,
        )
        events.append(
            EventEnvelope(
                study_id=study_id,
                type="GraphNodeUpserted",
                entity_key=exp_id,
                idem_key=make_idem("GraphNodeUpserted", exp_id),
                payload=node.model_dump(mode="json"),
                ts=now,
                schema_version=1,
            )
        )
    for b in claim_bindings:
        assert b.ref is not None
        if graph is not None and graph.get_node(b.ref) is None:
            raise InputUnavailable("claim_node_missing")
        eid = f"uses_claim:{exp_id}:{b.ref}"
        edge = GraphEdge(
            edge_id=eid,
            kind="uses_claim",
            study_id=study_id,
            src_id=exp_id,
            dst_id=b.ref,
            attrs={},
            created_at=now if isinstance(now, datetime) else None,
        )
        payload = edge.model_dump(mode="json")
        if payload.get("created_at") is None:
            payload["created_at"] = ts
        events.append(
            EventEnvelope(
                study_id=study_id,
                type="GraphEdgeAdded",
                entity_key=eid,
                idem_key=make_idem("GraphEdgeAdded", eid),
                payload=payload,
                ts=now,
                schema_version=1,
            )
        )
    return events
