"""Minimal InputBinding / dependency resolution (DESIGN §3.4 + M1 PR7/PR8)."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from coscientist.core.claims import (
    producer_attempt_failed,
    resolve_about_target_id,
    whitelist_claim_attrs,
)
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


def verdict_binding_digest(*, ref: str, snapshot: dict[str, Any]) -> str:
    """P2 digest of a persisted verdict snapshot (``InputSpec.kind=verdict``)."""
    return sha256_hex(
        canonical_json(
            {
                "kind": "verdict",
                "ref": ref,
                "verdict": snapshot.get("verdict"),
                "by_literature": bool(snapshot.get("by_literature")),
                "provenance": snapshot.get("provenance", "real"),
                "refutations_needed": snapshot.get("refutations_needed", 1),
                "refutation_priority": bool(snapshot.get("refutation_priority")),
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
    verdict_snapshots: dict[str, dict[str, Any]] | None = None,
) -> tuple[list[InputBinding], Provenance]:
    """
    Resolve TaskSpec.inputs (artifact | claim | verdict).

    Raises InputUnavailable when mode=forbid and data missing / dep FAILED, when
    claim ``ref`` is missing / unknown / produced by a FAILED attempt, or when
    a verdict snapshot for ``ref`` is unavailable.
    """
    bindings: list[InputBinding] = []
    provs: list[Provenance] = ["real"]
    claims = known_claim_ids or set()
    claim_prov = claim_provenance or {}
    snapshots = verdict_snapshots or {}
    for spec in task.spec.inputs:
        kind = spec.kind
        if kind == "verdict":
            if not spec.ref:
                raise InputUnavailable("verdict_ref_missing")
            snap = snapshots.get(spec.ref)
            if snap is None:
                raise InputUnavailable("verdict_unavailable")
            source_raw = snap.get("provenance", "real")
            source: Provenance = (
                source_raw if source_raw in VALID_PROVENANCE else "stub"
            )  # type: ignore[assignment]
            digest = verdict_binding_digest(ref=spec.ref, snapshot=snap)
            bindings.append(
                InputBinding(
                    input=spec.ref,
                    artifact_id="",
                    source=source,
                    kind="verdict",
                    ref=spec.ref,
                    digest=digest,
                )
            )
            provs.append(source)
            continue
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
    graph: Any | None = None,
    attempt_outcome: str = "SUCCESS",
) -> list[EventEnvelope]:
    """Emit ``GraphNodeUpserted`` (+ ``about`` edges) for agent claims.

    Production path (Executor.record): every claim node carries
    ``attrs.provenance`` from the producing attempt (H-b). On re-upsert of the
    same agent-chosen id, provenance is the **worst** of existing and new
    (stub claim cannot be laundered to real). Agent fields are whitelisted.
    FAILED attempts should pass ``claims=[]`` (caller); ``attempt_outcome`` is
    still recorded when claims are emitted.
    """
    if attempt_outcome == "FAILED":
        return []
    if provenance not in VALID_PROVENANCE:
        provenance = "stub"  # type: ignore[assignment]
    events: list[EventEnvelope] = []
    for i, raw in enumerate(claims):
        if not isinstance(raw, dict):
            continue
        key = str(raw.get("id") or raw.get("claim_id") or f"{attempt_id}:{i}")
        # Allow full node ids or short keys.
        cid = key if key.startswith(f"{study_id}:claim:") else claim_node_id(study_id, key)
        attrs = whitelist_claim_attrs(raw)
        # Core-owned fields (never agent-controlled wholesale).
        new_prov: Provenance = provenance
        if graph is not None:
            existing = graph.get_node(cid)
            if existing is not None:
                old = (existing.attrs or {}).get("provenance")
                if old in VALID_PROVENANCE:
                    new_prov = worst_provenance(old, provenance)  # type: ignore[arg-type]
        attrs["provenance"] = new_prov
        attrs["attempt_id"] = attempt_id
        attrs["attempt_outcome"] = attempt_outcome
        # Drop agent-supplied stance if it slipped past whitelist (defense).
        attrs.pop("stance", None)
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
        # Required about edge (claim → hypothesis|question), out 1..1.
        target = attrs.get("target")
        if target:
            target_id, target_kind = resolve_about_target_id(
                study_id, str(target), graph
            )
            have_about = False
            if graph is not None:
                have_about = any(
                    e.kind == "about" for e in graph.edges_from(cid)
                )
                # Also treat edges we are about to emit in this batch.
            if any(
                ev.type == "GraphEdgeAdded"
                and (ev.payload or {}).get("src_id") == cid
                and (ev.payload or {}).get("kind") == "about"
                for ev in events
            ):
                have_about = True
            if not have_about:
                if graph is None or graph.get_node(target_id) is None:
                    # Ensure endpoint exists so GraphEdgeAdded does not fail.
                    already = any(
                        ev.type == "GraphNodeUpserted" and ev.entity_key == target_id
                        for ev in events
                    )
                    if not already:
                        tnode = GraphNode(
                            node_id=target_id,
                            kind=target_kind,
                            study_id=study_id,
                            ref_id=str(target),
                            attrs={},
                            created_at=now,
                        )
                        events.append(
                            EventEnvelope(
                                study_id=study_id,
                                type="GraphNodeUpserted",
                                entity_key=target_id,
                                idem_key=make_idem(
                                    "GraphNodeUpserted", target_id, attempt_id
                                ),
                                payload=tnode.model_dump(mode="json"),
                                ts=now,
                                schema_version=1,
                            )
                        )
                eid = f"about:{cid}:{target_id}"
                edge = GraphEdge(
                    edge_id=eid,
                    kind="about",
                    study_id=study_id,
                    src_id=cid,
                    dst_id=target_id,
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
    """Reject invalid InputSpec at accept_plan (kind-specific fields)."""
    for t in tasks:
        for inp in t.inputs:
            kind = inp.kind
            if kind == "verdict":
                if not inp.ref:
                    raise ValueError(
                        f"InputSpec.kind=verdict requires ref "
                        f"(task {t.task_key!r})"
                    )
            elif kind == "claim":
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


def collect_bindable_claims(
    graph: Any,
    *,
    results: dict[str, dict[str, Any]] | None = None,
) -> tuple[set[str], dict[str, Provenance]]:
    """Claim ids eligible as ``kind=claim`` inputs + their provenance.

    Skips claims whose producing attempt is FAILED (carry-over #2).
    """
    known: set[str] = set()
    claim_provenance: dict[str, Provenance] = {}
    if graph is None:
        return known, claim_provenance
    try:
        nodes = graph.nodes_by_kind("claim")
    except KeyError:
        return known, claim_provenance
    for node in nodes:
        attrs = node.attrs or {}
        aid = attrs.get("attempt_id")
        if producer_attempt_failed(
            str(aid) if aid else None,
            results=results,
            attrs_outcome=str(attrs.get("attempt_outcome") or "") or None,
        ):
            continue
        known.add(node.node_id)
        p = attrs.get("provenance")
        if p in VALID_PROVENANCE:
            claim_provenance[node.node_id] = p  # type: ignore[assignment]
    return known, claim_provenance
