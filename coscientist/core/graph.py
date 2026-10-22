"""Generic typed Node/Edge graph keyed by a kind registry.

Scientific kinds load from schema/graph_schema.yaml (RESEARCH_MODEL.md);
the core does not hard-code the ontology beyond that file.
"""

from __future__ import annotations

from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field

_SCHEMA_PATH = Path(__file__).resolve().parents[2] / "schema" / "graph_schema.yaml"


class Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class KindSpec(Frozen):
    """Registration entry for a node or edge kind."""

    kind: str
    layer: str = "research"  # research | execution | system
    schema_ref: str | None = None
    description: str = ""
    prefix: str | None = None
    allowed_attr_keys: frozenset[str] | None = None


class KindRegistry:
    """Mutable registry of node/edge kinds. Unknown kind → KeyError at write time."""

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
    """In-memory / projection-backed typed graph. Validates kinds via registry."""

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

    def clear(self) -> None:
        self._nodes.clear()
        self._edges.clear()


def load_schema(path: Path | None = None) -> dict[str, Any]:
    p = path or _SCHEMA_PATH
    return yaml.safe_load(p.read_text(encoding="utf-8"))


@lru_cache(maxsize=4)
def registry_from_schema(path: str | None = None) -> KindRegistry:
    """Load node/edge kinds from graph_schema.yaml (single source of truth)."""
    data = load_schema(Path(path) if path else _SCHEMA_PATH)
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
            KindSpec(
                kind=e["name"],
                layer="research",
                description=str(e.get("card", "")),
            )
        )
    return reg


def default_execution_registry() -> KindRegistry:
    """Registry for runtimes: schema kinds (copy so callers may extend)."""
    return registry_from_schema().copy()
