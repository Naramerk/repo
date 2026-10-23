"""Generic Node/Edge registry loaded from packaged coscientist.schema."""

from __future__ import annotations

import pytest

from coscientist.core.graph import (
    GraphEdge,
    GraphNode,
    GraphStore,
    KindRegistry,
    KindSpec,
    default_execution_registry,
    registry_from_schema,
)


def test_schema_loads_thirteen_nodes_eighteen_edges():
    reg = registry_from_schema()  # packaged coscientist.schema
    assert len(reg.node_kinds()) == 13  # +hitl (M1 PR12)
    assert len(reg.edge_kinds()) == 18  # +decomposes (DESIGN_M1 §10)
    assert "hypothesis" in reg.node_kinds()
    assert "hitl" in reg.node_kinds()
    assert "asks" in reg.edge_kinds()
    assert "uses_claim" in reg.edge_kinds()
    assert "decomposes" in reg.edge_kinds()
    assert "goal" not in reg.node_kinds()
    assert "source" not in reg.node_kinds()
    assert "run" not in reg.node_kinds()


def test_registry_rejects_unknown_kind():
    reg = default_execution_registry()
    g = GraphStore(reg)
    with pytest.raises(KeyError, match="unknown node kind"):
        g.upsert_node(GraphNode(node_id="n1", kind="not_a_kind", study_id="st"))


def test_schema_kinds_usable_without_hardcoding():
    reg = default_execution_registry()
    g = GraphStore(reg)
    g.upsert_node(GraphNode(node_id="s1", kind="study", study_id="st"))
    g.upsert_node(GraphNode(node_id="q1", kind="question", study_id="st"))
    g.add_edge(
        GraphEdge(edge_id="e1", kind="asks", study_id="st", src_id="s1", dst_id="q1")
    )
    assert len(g.nodes_by_kind("question")) == 1
    assert len(g.edges_from("s1")) == 1


def test_duplicate_registration_errors():
    reg = KindRegistry()
    reg.register_node(KindSpec(kind="goal"))
    with pytest.raises(ValueError):
        reg.register_node(KindSpec(kind="goal"))
