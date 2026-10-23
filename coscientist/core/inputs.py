"""Minimal InputBinding / dependency resolution (DESIGN §3.4 + M1 PR7)."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from coscientist.core.digest import canonical_json, sha256_hex
from coscientist.core.domain import (
    VALID_PROVENANCE,
    InputBinding,
    InputSpec,
    Provenance,
    Task,
    TaskOutcome,
    TaskSpec,
    TaskState,
)
from coscientist.core.events import EventEnvelope, make_idem
from coscientist.core.graph import GraphEdge, GraphNode
from coscientist.core.ids import claim_node_id, experiment_node_id

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


def input_binding_digest(
    *,
    kind: str,
    input_name: str,
    artifact_id: str,
    source: str,
    ref: str | None,
) -> str:
    """P2 snapshot digest of **binding metadata** (journaled on AttemptStarted).

    Covers which input was bound and at what provenance — not claim/artifact
    body bytes. Claim content lives on the graph node; content changes are
    separate ``GraphNodeUpserted`` revisions (DESIGN_M1 §3).
    """
    return sha256_hex(
        canonical_json(
            {
                "kind": kind,
                "input": input_name,
                "artifact_id": artifact_id or "",
                "source": source,
                "ref": ref,
                "at": "P2",
            }
        )
    )


def _claim_source(
    ref: str,
    *,
    claim_provenance: dict[str, Provenance],
) -> Provenance:
    """Provenance of the producing attempt (H-b).

    Unknown / missing attrs → ``stub`` (never treat as real).
    """
    prov = claim_provenance.get(ref)
    if prov not in VALID_PROVENANCE:
        return "stub"
    return prov  # type: ignore[return-value]


def bind_inputs(
    task: Task,
    *,
    dep_tasks: dict[str, Task],
    dep_outputs: dict[str, dict[str, str]],
    dep_provenance: dict[str, Provenance],
    known_claim_ids: set[str] | None = None,
    claim_provenance: dict[str, Provenance] | None = None,
) -> tuple[list[InputBinding], Provenance]:
    """
    Resolve TaskSpec.inputs (artifact | claim).

    ``kind=verdict`` is rejected at ``accept_plan`` (PR 8). Raises
    InputUnavailable when mode=forbid and data missing / dep FAILED, or when
    claim ``ref`` is missing / unknown.
    """
    bindings: list[InputBinding] = []
    provs: list[Provenance] = ["real"]
    claims = known_claim_ids or set()
    claim_prov = claim_provenance or {}
    for spec in task.spec.inputs:
        kind = spec.kind
        if kind == "verdict":
            raise InputUnavailable("verdict_kind_deferred_pr8")
        if kind == "claim":
            if not spec.ref:
                raise InputUnavailable("claim_ref_missing")
            if spec.ref not in claims:
                raise InputUnavailable("claim_unavailable")
            source = _claim_source(spec.ref, claim_provenance=claim_prov)
            digest = input_binding_digest(
                kind="claim",
                input_name=spec.ref,
                artifact_id="",
                source=source,
                ref=spec.ref,
            )
            bindings.append(
                InputBinding(
                    input=spec.ref,
                    artifact_id="",
                    source=source,
                    kind="claim",
                    ref=spec.ref,
                    digest=digest,
                )
            )
            provs.append(source)
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
            source = "real"
        elif aid and mode in {InputMode.USE_PARTIAL, InputMode.SIMULATE}:
            source = "partial"
        elif mode == InputMode.SIMULATE:
            source = "stub"
            aid = f"stub:{spec.from_task}:{spec.output}"
        else:
            raise InputUnavailable()

        if source != "stub":
            source = worst_provenance(source, dep_prov)
        digest = input_binding_digest(
            kind="artifact",
            input_name=spec.output,
            artifact_id=aid or "",
            source=source,
            ref=None,
        )
        bindings.append(
            InputBinding(
                input=spec.output,
                artifact_id=aid or "",
                source=source,
                kind="artifact",
                digest=digest,
            )
        )
        provs.append(source)
    return bindings, worst_provenance(*provs)


def claim_nodes_from_attempt(
    *,
    study_id: str,
    attempt_id: str,
    provenance: Provenance,
    claims: list[dict[str, Any]],
    now: datetime,
) -> list[EventEnvelope]:
    """Emit ``GraphNodeUpserted`` for agent claims; provenance = producing attempt.

    Production path (Executor.record): every claim node carries
    ``attrs.provenance`` from the attempt that produced it (H-b).
    """
    if provenance not in VALID_PROVENANCE:
        provenance = "stub"  # type: ignore[assignment]
    events: list[EventEnvelope] = []
    for i, raw in enumerate(claims):
        if not isinstance(raw, dict):
            continue
        key = str(raw.get("id") or raw.get("claim_id") or f"{attempt_id}:{i}")
        # Allow full node ids or short keys.
        cid = key if key.startswith(f"{study_id}:claim:") else claim_node_id(study_id, key)
        attrs = {k: v for k, v in raw.items() if k not in ("id", "claim_id")}
        attrs["provenance"] = provenance
        attrs["attempt_id"] = attempt_id
        node = GraphNode(
            node_id=cid,
            kind="claim",
            study_id=study_id,
            ref_id=cid,
            attrs=attrs,
            created_at=now,
        )
        events.append(
            EventEnvelope(
                study_id=study_id,
                type="GraphNodeUpserted",
                entity_key=cid,
                idem_key=make_idem("GraphNodeUpserted", cid, attempt_id),
                payload=node.model_dump(mode="json"),
                ts=now,
                schema_version=1,
            )
        )
    return events


def uses_claim_graph_events(
    *,
    study_id: str,
    task: Task,
    bindings: list[InputBinding],
    now: datetime,
    graph: Any | None = None,
) -> list[EventEnvelope]:
    """Build GraphNodeUpserted (experiment) + GraphEdgeAdded (uses_claim) events.

    Claim nodes must already exist in ``graph`` (or be upserted by the caller).
    """
    claim_bindings = [b for b in bindings if b.kind == "claim" and b.ref]
    if not claim_bindings:
        return []
    exp_id = experiment_node_id(study_id, task.spec.task_key)
    events: list[EventEnvelope] = []
    if graph is None or graph.get_node(exp_id) is None:
        node = GraphNode(
            node_id=exp_id,
            kind="experiment",
            study_id=study_id,
            ref_id=task.spec.task_key,
            attrs={},
            created_at=now,
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
            created_at=now,
        )
        events.append(
            EventEnvelope(
                study_id=study_id,
                type="GraphEdgeAdded",
                entity_key=eid,
                idem_key=make_idem("GraphEdgeAdded", eid),
                payload=edge.model_dump(mode="json"),
                ts=now,
                schema_version=1,
            )
        )
    return events


def validate_input_specs(tasks: list[TaskSpec]) -> None:
    """Reject invalid InputSpec at accept_plan (kind-specific fields; verdict→PR8)."""
    for t in tasks:
        for inp in t.inputs:
            kind = inp.kind
            if kind == "verdict":
                raise ValueError(
                    "InputSpec.kind=verdict is deferred to PR 8 "
                    "(runtime verdict() wiring); use artifact or claim"
                )
            if kind == "claim":
                if not inp.ref:
                    raise ValueError(
                        f"InputSpec.kind=claim requires ref "
                        f"(task {t.task_key!r})"
                    )
            elif kind == "artifact":
                if not inp.from_task or not inp.output:
                    raise ValueError(
                        f"InputSpec.kind=artifact requires from_task and output "
                        f"(task {t.task_key!r})"
                    )
            else:
                raise ValueError(f"unknown InputSpec.kind: {kind!r}")
