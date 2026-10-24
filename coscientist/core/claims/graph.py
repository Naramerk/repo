"""Generic typed Node/Edge graph keyed by a kind registry.

Kinds load from packaged coscientist/schema/graph_schema.yaml (RESEARCH_MODEL.md).
"""

from __future__ import annotations

import re
from datetime import datetime
from functools import lru_cache
from importlib import resources
from typing import Annotated, Any, Literal, TypeGuard, Union, get_args

import yaml
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from coscientist.core.events.errors import CoreError
from coscientist.schema import GRAPH_SCHEMA_VERSION

_FIELD_KEYS = frozenset({"name", "type", "required", "default", "note", "computed"})
_ROOT_KEYS = frozenset({"version", "nodes", "edges", "provenance", "code_check_kinds", "rules", "volume_b"})
_NODE_KEYS = frozenset({"kind", "prefix", "text_ru", "fields", "created_by", "from_output"})
_EDGE_KEYS = frozenset({"name", "pairs", "card", "attrs", "by", "note"})
_CARD_KEYS = frozenset({"out", "in", "out_by_src"})
SimpleTag = Literal["str", "int", "bool", "float", "datetime", "digest"]
_SIMPLE_TAGS = frozenset(get_args(SimpleTag))

class Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

class AttrSimple(Frozen):
    text: str
    tag: SimpleTag
class AttrEnum(Frozen):
    text: str
    tag: Literal["enum"]
    enum_values: tuple[str, ...]
class AttrRef(Frozen):
    text: str
    tag: Literal["ref"]
    ref_kinds: tuple[str, ...]
class AttrObj(Frozen):
    text: str
    tag: Literal["obj"]
    obj_names: tuple[str, ...]
class AttrList(Frozen):
    text: str
    tag: Literal["list"]
    list_elem: AttrType
AttrType = Annotated[Union[AttrSimple, AttrEnum, AttrRef, AttrObj, AttrList], Field(discriminator="tag")]
class AttrDefault(Frozen):
    value: Any
class AttrFieldSpec(Frozen):
    name: str
    type: AttrType
    required: bool
    default: AttrDefault | None
class KindSpec(Frozen):
    kind: str
    layer: str = "research"
    schema_ref: str | None = None
    description: str = ""
    prefix: str | None = None
    fields: tuple[AttrFieldSpec, ...]


class GraphSchemaError(CoreError):
    pass
class KindRegistry:
    def __init__(self) -> None:
        self._nodes: dict[str, KindSpec] = {}
        self._edges: dict[str, KindSpec] = {}
        self._pairs: dict[str, frozenset[tuple[str, str]]] = {}

    def register_node(self, spec: KindSpec) -> None:
        if spec.kind in self._nodes:
            raise ValueError(f"node kind already registered: {spec.kind}")
        self._nodes[spec.kind] = spec

    def register_edge(
        self,
        spec: KindSpec,
        pairs: frozenset[tuple[str, str]] | None = None,
    ) -> None:
        if spec.kind in self._edges:
            raise ValueError(f"edge kind already registered: {spec.kind}")
        self._edges[spec.kind] = spec
        if pairs is not None:
            self._pairs[spec.kind] = pairs

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

    def edge_allowed(self, name: str, src_kind: str, dst_kind: str) -> bool:
        allowed = self._pairs.get(name)
        if allowed is None:
            return name in self._edges
        return (src_kind, dst_kind) in allowed

    def node_kinds(self) -> frozenset[str]:
        return frozenset(self._nodes)

    def edge_kinds(self) -> frozenset[str]:
        return frozenset(self._edges)

    def copy(self) -> KindRegistry:
        other = KindRegistry()
        other._nodes = dict(self._nodes)
        other._edges = dict(self._edges)
        other._pairs = dict(self._pairs)
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
        src = self._nodes[edge.src_id]
        dst = self._nodes[edge.dst_id]
        if not self.registry.edge_allowed(edge.kind, src.kind, dst.kind):
            raise ValueError(
                f"edge {edge.kind}: {src.kind} -> {dst.kind} not allowed"
            )
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

def schema_mapping_from_text(raw: str) -> dict[str, Any]:
    data = yaml.safe_load(raw)
    if not isinstance(data, dict):
        raise GraphSchemaError("graph_schema.yaml root must be a mapping")
    return data
def load_schema() -> dict[str, Any]:
    text = resources.files("coscientist.schema").joinpath("graph_schema.yaml").read_text(encoding="utf-8")
    return schema_mapping_from_text(text)
def _req(obj: dict[str, Any], key: str, *, ctx: str) -> Any:
    if key not in obj:
        raise GraphSchemaError(f"{ctx} missing required key {key!r}")
    return obj[key]
def _ns(v: Any, *, ctx: str) -> str:
    if not isinstance(v, str) or not v:
        raise GraphSchemaError(f"{ctx} must be a non-empty str")
    return v
def _forbid_extra(obj: dict[str, Any], allowed: frozenset[str], *, ctx: str) -> None:
    if set(obj) - allowed:
        raise GraphSchemaError(f"{ctx} unknown keys: {sorted(set(obj) - allowed)}")
_PIPE_BODY = re.compile(r"[^|]+(?:\|[^|]+)*")
_LIST_PREFIX = "list("
def _split_pipe(body: str, *, ctx: str, label: str) -> tuple[str, ...]:
    if not _PIPE_BODY.fullmatch(body):
        raise GraphSchemaError(f"{ctx}.type {label} must be non-empty")
    return tuple(re.findall(r"[^|]+", body))
def _is_simple_tag(raw: str) -> TypeGuard[SimpleTag]:
    return raw in _SIMPLE_TAGS
def parse_attr_type(raw: str, *, ctx: str) -> AttrType:
    if _is_simple_tag(raw):
        return AttrSimple(text=raw, tag=raw)
    for prefix, label, make in (
        ("enum(", "enum values", lambda p: AttrEnum(text=raw, tag="enum", enum_values=p)),
        ("ref(", "ref kinds", lambda p: AttrRef(text=raw, tag="ref", ref_kinds=p)),
        ("obj(", "obj names", lambda p: AttrObj(text=raw, tag="obj", obj_names=p)),
    ):
        if raw.startswith(prefix) and raw.endswith(")"):
            return make(_split_pipe(raw[len(prefix):-1], ctx=ctx, label=label))
    if raw.startswith(_LIST_PREFIX) and raw.endswith(")"):
        body = raw[len(_LIST_PREFIX):-1]
        if not body:
            raise GraphSchemaError(f"{ctx}.type list() empty")
        return AttrList(text=raw, tag="list", list_elem=parse_attr_type(body, ctx=ctx))
    raise GraphSchemaError(f"{ctx}.type unsupported: {raw!r}")
def check_ref_kinds(t: AttrType, known: frozenset[str], *, ctx: str) -> None:
    if isinstance(t, AttrRef):
        missing = sorted(k for k in t.ref_kinds if k not in known)
        if missing:
            raise GraphSchemaError(f"{ctx}: unknown ref kind(s) {missing}")
    elif isinstance(t, AttrList):
        check_ref_kinds(t.list_elem, known, ctx=ctx)
_AWARE_DT = TypeAdapter(AwareDatetime)
def parse_aware_datetime(value: Any, *, ctx: str) -> datetime:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise GraphSchemaError(f"{ctx}: expected aware datetime")
        return value
    if not isinstance(value, str):
        raise GraphSchemaError(f"{ctx}: expected aware datetime, got {type(value).__name__}")
    try:
        return _AWARE_DT.validate_python(value)
    except ValidationError as e:
        raise GraphSchemaError(f"{ctx}: invalid aware datetime: {value!r}") from e
def _attr_check(t: AttrType, value: Any, *, null_ok: bool, ctx: str) -> None:
    if value is None:
        if not null_ok:
            raise GraphSchemaError(f"{ctx}: null not allowed")
        return
    if isinstance(t, AttrSimple):
        tag = t.tag
        if tag == "datetime":
            parse_aware_datetime(value, ctx=ctx)
            return
        if tag in ("str", "digest"):
            if isinstance(value, str):
                return
            raise GraphSchemaError(f"{ctx}: incompatible with type")
        if tag == "int":
            if type(value) is int:
                return
            raise GraphSchemaError(f"{ctx}: incompatible with type")
        if tag == "bool":
            if type(value) is bool:
                return
            raise GraphSchemaError(f"{ctx}: incompatible with type")
        if tag == "float":
            if type(value) is float:
                return
            raise GraphSchemaError(f"{ctx}: incompatible with type")
        raise GraphSchemaError(f"{ctx}: incompatible with type")
    if isinstance(t, AttrEnum):
        if isinstance(value, str) and value in t.enum_values:
            return
        raise GraphSchemaError(f"{ctx}: incompatible with type")
    if isinstance(t, AttrRef):
        if isinstance(value, str) and value != "":
            return
        raise GraphSchemaError(f"{ctx}: incompatible with type")
    if isinstance(t, AttrObj):
        if type(value) is dict and all(isinstance(k, str) for k in value):
            return
        raise GraphSchemaError(f"{ctx}: incompatible with type")
    if isinstance(t, AttrList):
        if not isinstance(value, list):
            raise GraphSchemaError(f"{ctx}: expected list")
        for i, el in enumerate(value):
            _attr_check(t.list_elem, el, null_ok=False, ctx=f"{ctx}[{i}]")
        return
    raise GraphSchemaError(f"{ctx}: incompatible with type")
def validate_attr_value(t: AttrType, value: Any, *, null_ok: bool, ctx: str) -> None:
    _attr_check(t, value, null_ok=null_ok, ctx=ctx)
def _attr_fields(raw: Any, *, ctx: str) -> tuple[AttrFieldSpec, ...]:
    if not isinstance(raw, list):
        raise GraphSchemaError(f"{ctx}: fields/attrs must be a list")
    out: list[AttrFieldSpec] = []
    seen: set[str] = set()
    for i, f in enumerate(raw):
        ic = f"{ctx}[{i}]"
        if not isinstance(f, dict):
            raise GraphSchemaError(f"{ic} must be a mapping")
        _forbid_extra(f, _FIELD_KEYS, ctx=ic)
        name = _ns(_req(f, "name", ctx=ic), ctx=f"{ic}.name")
        if name in seen:
            raise GraphSchemaError(f"{ctx} duplicate field name {name!r}")
        seen.add(name)
        type_raw = _ns(_req(f, "type", ctx=ic), ctx=f"{ic}.type")
        required_raw = _req(f, "required", ctx=ic)
        if not isinstance(required_raw, bool):
            raise GraphSchemaError(f"{ic}.required must be a bool")
        at = parse_attr_type(type_raw, ctx=ic)
        default = None
        if "default" in f:
            try:
                validate_attr_value(at, f["default"], null_ok=True, ctx=f"{ic}.default")
            except GraphSchemaError as e:
                raise GraphSchemaError(f"{ic}.default incompatible with type {type_raw!r}") from e
            default = AttrDefault(value=f["default"])
        out.append(AttrFieldSpec(name=name, type=at, required=required_raw, default=default))
    return tuple(out)
def _card_desc(card: Any, *, ctx: str) -> str:
    if not isinstance(card, dict):
        raise GraphSchemaError(f"{ctx}.card must be a mapping")
    _forbid_extra(card, _CARD_KEYS, ctx=f"{ctx}.card")
    o = _ns(_req(card, "out", ctx=f"{ctx}.card"), ctx=f"{ctx}.card.out")
    return f"{o}/{_ns(_req(card, 'in', ctx=f'{ctx}.card'), ctx=f'{ctx}.card.in')}"
def _edge_pairs(e: dict[str, Any], *, ctx: str) -> frozenset[tuple[str, str]]:
    pairs_raw = _req(e, "pairs", ctx=ctx)
    if not isinstance(pairs_raw, list) or not pairs_raw:
        raise GraphSchemaError(f"{ctx}.pairs must be a non-empty list")
    pairs: set[tuple[str, str]] = set()
    for i, item in enumerate(pairs_raw):
        if not isinstance(item, (list, tuple)) or len(item) != 2:
            raise GraphSchemaError(f"{ctx}.pairs[{i}] must be a [src, dst] pair")
        a, b = item[0], item[1]
        if type(a) is not str or type(b) is not str or not a or not b:
            raise GraphSchemaError(f"{ctx}.pairs[{i}] entries must be non-empty str")
        pairs.add((a, b))
    return frozenset(pairs)
def registry_from_mapping(data: dict[str, Any]) -> KindRegistry:
    _forbid_extra(data, _ROOT_KEYS, ctx="graph_schema")
    ver = _req(data, "version", ctx="graph_schema")
    if ver != GRAPH_SCHEMA_VERSION:
        raise GraphSchemaError(f"graph_schema.version must be {GRAPH_SCHEMA_VERSION!r}, got {ver!r}")
    reg = KindRegistry()
    nodes_raw = _req(data, "nodes", ctx="graph_schema")
    if not isinstance(nodes_raw, list) or not nodes_raw:
        raise GraphSchemaError("graph_schema.nodes must be a non-empty list")
    for i, n in enumerate(nodes_raw):
        ctx = f"nodes[{i}]"
        if not isinstance(n, dict):
            raise GraphSchemaError(f"{ctx} must be a mapping")
        _forbid_extra(n, _NODE_KEYS, ctx=ctx)
        kind = _ns(_req(n, "kind", ctx=ctx), ctx=f"{ctx}.kind")
        fields = _attr_fields(_req(n, "fields", ctx=ctx), ctx=f"{ctx}.fields")
        reg.register_node(KindSpec(
            kind=kind, layer="research", schema_ref=None, prefix=_ns(_req(n, "prefix", ctx=ctx), ctx=f"{ctx}.prefix"),
            description=_ns(_req(n, "text_ru", ctx=ctx), ctx=f"{ctx}.text_ru"), fields=fields,
        ))
    known = reg.node_kinds()
    for kind in known:
        for f in reg.require_node_kind(kind).fields:
            check_ref_kinds(f.type, known, ctx=f"node {kind!r} field {f.name!r}")
    edges_raw = _req(data, "edges", ctx="graph_schema")
    if not isinstance(edges_raw, list) or not edges_raw:
        raise GraphSchemaError("graph_schema.edges must be a non-empty list")
    for i, e in enumerate(edges_raw):
        ctx = f"edges[{i}]"
        if not isinstance(e, dict):
            raise GraphSchemaError(f"{ctx} must be a mapping")
        _forbid_extra(e, _EDGE_KEYS, ctx=ctx)
        name = _ns(_req(e, "name", ctx=ctx), ctx=f"{ctx}.name")
        pairs = _edge_pairs(e, ctx=ctx)
        edge_fields = _attr_fields(_req(e, "attrs", ctx=ctx), ctx=f"{ctx}.attrs")
        for f in edge_fields:
            check_ref_kinds(f.type, known, ctx=f"edge {name!r} attr {f.name!r}")
        reg.register_edge(
            KindSpec(
                kind=name, layer="research", schema_ref=None, prefix=None, fields=edge_fields,
                description=_card_desc(_req(e, "card", ctx=ctx), ctx=ctx),
            ),
            pairs=pairs,
        )
    return reg
@lru_cache(maxsize=1)
def registry_from_schema() -> KindRegistry:
    return registry_from_mapping(load_schema())
def default_execution_registry() -> KindRegistry:
    return registry_from_schema().copy()
AttrList.model_rebuild()
