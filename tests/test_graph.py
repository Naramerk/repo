"""Generic Node/Edge kind registry — scientific ontology plugs in later."""

from __future__ import annotations

import pytest

from coscientist.core.graph import (
    GraphEdge,
    GraphNode,
    GraphStore,
    KindRegistry,
    KindSpec,
    default_execution_registry,
)


def test_registry_rejects_unknown_kind():
    reg = default_execution_registry()
    g = GraphStore(reg)
    with pytest.raises(KeyError, match="unknown node kind"):
        g.upsert_node(
            GraphNode(node_id="n1", kind="hypothesis", study_id="st")
        )


def test_plug_in_scientific_kind_without_core_change():
    reg = default_execution_registry()
    reg.register_node(KindSpec(kind="hypothesis", layer="research", description="RQ hypothesis"))
    reg.register_edge(KindSpec(kind="supports", layer="research"))
    g = GraphStore(reg)
    g.upsert_node(GraphNode(node_id="h1", kind="hypothesis", study_id="st"))
    g.upsert_node(GraphNode(node_id="e1", kind="artifact", study_id="st"))
    g.add_edge(
        GraphEdge(edge_id="x1", kind="supports", study_id="st", src_id="e1", dst_id="h1")
    )
    assert len(g.nodes_by_kind("hypothesis")) == 1


def test_duplicate_registration_errors():
    reg = KindRegistry()
    reg.register_node(KindSpec(kind="goal"))
    with pytest.raises(ValueError):
        reg.register_node(KindSpec(kind="goal"))
