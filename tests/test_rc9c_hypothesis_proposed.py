"""RC9c (#105): HypothesisProposed via G7 writer."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from coscientist.core.claims.graph import GraphNode, registry_from_schema
from coscientist.core.claims.graph_writer import GraphWriterError, graph_node_upserted
from coscientist.core.claims.hypothesis_propose import hypothesis_proposed_events
from coscientist.core.events.ids import hypothesis_node_id
from coscientist.core.events.payload_schemas import (
    HypothesisProposedPayload,
    validate_event_payload,
)
from coscientist.core.events.schema_versions import HYPOTHESIS_PROPOSED_SCHEMA_VERSION


def _ts() -> datetime:
    return datetime(2026, 10, 25, 14, 0, 0, tzinfo=UTC)


def _payload(**kw: object) -> HypothesisProposedPayload:
    base: dict[str, object] = {
        "hypothesis_id": "H1",
        "statement": "compound X inhibits Y",
        "prediction": "assay Z drops ≥20%",
        "rank": 1,
        "selected": True,
        "source_task": None,
        "rationale": None,
    }
    base.update(kw)
    return HypothesisProposedPayload.model_validate(base)


def test_emits_hypothesis_proposed() -> None:
    """DoD: emit HypothesisProposed + G7 GraphNodeUpserted for hypothesis."""
    sid = "st_rc9c"
    payload = _payload(source_task="HYP-1", rationale="lit gap")
    events = hypothesis_proposed_events(
        study_id=sid, payload=payload, ts=_ts(), registry=registry_from_schema()
    )
    assert len(events) == 2
    proposed, graph_ev = events
    assert proposed.type == "HypothesisProposed"
    assert proposed.entity_key == "H1"
    assert proposed.idem_key == "HypothesisProposed:H1"
    assert proposed.schema_version == HYPOTHESIS_PROPOSED_SCHEMA_VERSION
    assert proposed.payload["hypothesis_id"] == "H1"
    assert proposed.payload["statement"] == "compound X inhibits Y"
    assert proposed.payload["prediction"] == "assay Z drops ≥20%"
    assert proposed.payload["rank"] == 1
    assert proposed.payload["selected"] is True
    assert proposed.payload["source_task"] == "HYP-1"
    assert proposed.payload["rationale"] == "lit gap"
    validate_event_payload("HypothesisProposed", dict(proposed.payload))

    nid = hypothesis_node_id(sid, "H1")
    assert graph_ev.type == "GraphNodeUpserted"
    assert graph_ev.entity_key == nid
    assert graph_ev.payload["kind"] == "hypothesis"
    assert graph_ev.payload["node_id"] == nid
    assert graph_ev.payload["ref_id"] == "H1"
    attrs = graph_ev.payload["attrs"]
    assert attrs["statement"] == "compound X inhibits Y"
    assert attrs["prediction"] == "assay Z drops ≥20%"
    assert attrs["rank"] == 1
    assert attrs["selected"] is True
    assert attrs["source_task"] == "HYP-1"
    assert attrs["rationale"] == "lit gap"

    rebuilt = graph_node_upserted(
        node=GraphNode.model_validate(graph_ev.payload),
        ts=graph_ev.ts,
        registry=registry_from_schema(),
        idem_extra=("hypothesis_proposed",),
    )
    assert graph_ev.idem_key == rebuilt.idem_key
    assert graph_ev.schema_version == rebuilt.schema_version


def test_payload_omits_optional_none_on_wire() -> None:
    events = hypothesis_proposed_events(
        study_id="st", payload=_payload(), ts=_ts()
    )
    wire = events[0].payload
    assert "source_task" not in wire
    assert "rationale" not in wire
    validate_event_payload("HypothesisProposed", dict(wire))


@pytest.mark.parametrize(
    "bad",
    [
        {"hypothesis_id": ""},
        {"hypothesis_id": " H1"},
        {"statement": ""},
        {"prediction": "  "},
        {"selected": 1},
        {"rank": True},
        {"source_task": ""},
        {"rationale": " "},
    ],
)
def test_payload_refuses_invalid(bad: dict[str, object]) -> None:
    base: dict[str, object] = {
        "hypothesis_id": "H1",
        "statement": "s",
        "prediction": "p",
        "rank": 0,
        "selected": False,
        "source_task": None,
        "rationale": None,
    }
    base.update(bad)
    with pytest.raises((ValidationError, ValueError)):
        HypothesisProposedPayload.model_validate(base)


def test_refuse_blank_study_id() -> None:
    with pytest.raises(GraphWriterError, match="study_id"):
        hypothesis_proposed_events(study_id="", payload=_payload(), ts=_ts())
