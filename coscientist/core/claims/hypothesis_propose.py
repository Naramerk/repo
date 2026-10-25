"""RC9c: HypothesisProposed + hypothesis graph node via G7 (#105).

Owner calls G7 directly (plan graph.md: no separate fold). Emits the journal
event and ``GraphNodeUpserted`` in one batch — hyp nodes have no second write path.
"""

from __future__ import annotations

from datetime import datetime

from coscientist.core.claims.graph import GraphNode, KindRegistry, registry_from_schema
from coscientist.core.claims.graph_writer import GraphWriterError, graph_node_upserted
from coscientist.core.events.events import UnstampedEvent, make_idem
from coscientist.core.events.ids import hypothesis_node_id
from coscientist.core.events.payload_schemas import HypothesisProposedPayload
from coscientist.core.events.schema_versions import HYPOTHESIS_PROPOSED_SCHEMA_VERSION


def _registry(registry: KindRegistry | None) -> KindRegistry:
    if registry is not None:
        return registry
    return registry_from_schema()


def _require_nonempty(label: str, value: str) -> str:
    if type(value) is not str or not value or value.strip() != value:
        raise GraphWriterError(
            f"{label} must be non-empty without surrounding whitespace"
        )
    return value


def hypothesis_proposed_events(
    *,
    study_id: str,
    payload: HypothesisProposedPayload,
    ts: datetime,
    registry: KindRegistry | None = None,
) -> list[UnstampedEvent]:
    """Build ``HypothesisProposed`` then G7 ``GraphNodeUpserted`` for the hyp node."""
    sid = _require_nonempty("study_id", study_id)
    hid = _require_nonempty("hypothesis_id", payload.hypothesis_id)
    attrs: dict[str, object] = {
        "statement": payload.statement,
        "prediction": payload.prediction,
        "rank": payload.rank,
        "selected": payload.selected,
    }
    if payload.source_task is not None:
        attrs["source_task"] = _require_nonempty("source_task", payload.source_task)
    if payload.rationale is not None:
        attrs["rationale"] = _require_nonempty("rationale", payload.rationale)

    nid = hypothesis_node_id(sid, hid)
    node = GraphNode(
        node_id=nid,
        kind="hypothesis",
        study_id=sid,
        ref_id=hid,
        attrs=attrs,
        created_at=ts,
    )
    reg = _registry(registry)
    wire = payload.model_dump(mode="json", exclude_none=True)
    proposed = UnstampedEvent(
        study_id=sid,
        type="HypothesisProposed",
        entity_key=hid,
        idem_key=make_idem("HypothesisProposed", hid),
        payload=wire,
        ts=ts,
        schema_version=HYPOTHESIS_PROPOSED_SCHEMA_VERSION,
    )
    graph_ev = graph_node_upserted(
        node=node,
        ts=ts,
        registry=reg,
        idem_extra=("hypothesis_proposed",),
    )
    return [proposed, graph_ev]
