"""Generic typed Node/Edge graph keyed by a kind registry.

Kinds load from packaged schema/graph_schema.yaml (RESEARCH_MODEL.md).
"""

from __future__ import annotations

from datetime import datetime
from functools import lru_cache
from importlib import resources
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field


class Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class KindSpec(Frozen):
    kind: str
    layer: str = "research"
    schema_ref: str | None = None
    description: str = ""
    prefix: str | None = None


class KindRegistry:
    def __init__(self) -> None:
        self._nodes: dict[str, KindSpec] = {}
        self._edges: dict[str, KindSpec] = {}

    def register_node(self, spec: KindSpec) -> None:
        if spec.kind in self._nodes:
            raise ValueError(f"node kind already registered: {spec.kind}")
        self._nodes[spec.kind] = spec

    def register_edge(self, spec: KindSpec) -> None:
        if spec.kind in self._edges:
            raise ValueError(f"edge kind already registered: {spec.kind}")
        self._edges[spec.kind] = spec

    def require_node_kind(self, kind: str) -> KindSpec:
        try:
            return self._nodes[kind]
        except KeyError as e:
            raise KeyError(f"unknown node kind: {kind}") from e

    def require_edge_kind(self, kind: str) -> KindSpec:
        try:
            return self._edges[kind]
        except KeyError as e:
            raise KeyError(f"unknown edge kind: {kind}") from e

    def node_kinds(self) -> frozenset[str]:
        return frozenset(self._nodes)

    def edge_kinds(self) -> frozenset[str]:
        return frozenset(self._edges)

    def copy(self) -> KindRegistry:
        other = KindRegistry()
        other._nodes = dict(self._nodes)
        other._edges = dict(self._edges)
        return other


class GraphNode(Frozen):
    node_id: str
    kind: str
    study_id: str
    parent_id: str | None = None
    ref_id: str | None = None
    attrs: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime | None = None


class GraphEdge(Frozen):
    edge_id: str
    kind: str
    study_id: str
    src_id: str
    dst_id: str
    attrs: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime | None = None


class GraphStore:
    def __init__(self, registry: KindRegistry) -> None:
        self.registry = registry
        self._nodes: dict[str, GraphNode] = {}
        self._edges: dict[str, GraphEdge] = {}

    def upsert_node(self, node: GraphNode) -> GraphNode:
        self.registry.require_node_kind(node.kind)
        self._nodes[node.node_id] = node
        return node

    def add_edge(self, edge: GraphEdge) -> GraphEdge:
        self.registry.require_edge_kind(edge.kind)
        if edge.src_id not in self._nodes:
            raise KeyError(f"edge src missing: {edge.src_id}")
        if edge.dst_id not in self._nodes:
            raise KeyError(f"edge dst missing: {edge.dst_id}")
        self._edges[edge.edge_id] = edge
        return edge

    def get_node(self, node_id: str) -> GraphNode | None:
        return self._nodes.get(node_id)

    def nodes_by_kind(self, kind: str) -> list[GraphNode]:
        self.registry.require_node_kind(kind)
        return [n for n in self._nodes.values() if n.kind == kind]

    def edges_from(self, src_id: str) -> list[GraphEdge]:
        return [e for e in self._edges.values() if e.src_id == src_id]

    def all_nodes(self) -> list[GraphNode]:
        return list(self._nodes.values())

    def all_edges(self) -> list[GraphEdge]:
        return list(self._edges.values())


def load_schema() -> dict[str, Any]:
    pkg = resources.files("coscientist.schema")
    text = (pkg / "graph_schema.yaml").read_text(encoding="utf-8")
    return yaml.safe_load(text)


@lru_cache(maxsize=1)
def registry_from_schema() -> KindRegistry:
    data = load_schema()
    reg = KindRegistry()
    for n in data.get("nodes", []):
        reg.register_node(
            KindSpec(
                kind=n["kind"],
                layer="research",
                description=str(n.get("text_ru", "")),
                prefix=n.get("prefix"),
            )
        )
    for e in data.get("edges", []):
        reg.register_edge(
            KindSpec(kind=e["name"], layer="research", description=str(e.get("card", "")))
        )
    return reg


def default_execution_registry() -> KindRegistry:
    return registry_from_schema().copy()
