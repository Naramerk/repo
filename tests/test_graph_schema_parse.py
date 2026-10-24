"""Strict graph_schema.yaml parsing via public registry_from_mapping."""
from __future__ import annotations

from datetime import datetime

import pytest

from coscientist.core.claims.graph import (
    AttrDefault,
    GraphSchemaError,
    KindRegistry,
    KindSpec,
    parse_attr_type,
    parse_aware_datetime,
    registry_from_mapping,
    registry_from_schema,
    schema_mapping_from_text,
    validate_attr_value,
)
from coscientist.schema import GRAPH_SCHEMA_VERSION

_QF = [{"name": "formulation", "type": "str", "required": True}]
_SF = [{"name": "request", "type": "str", "required": True}]
_Q = {"kind": "question", "prefix": "Q", "text_ru": "q", "fields": _QF}
_ST = {"kind": "study", "prefix": "ST", "text_ru": "s", "fields": _SF}
_ASKS = {"name": "asks", "pairs": [["study", "question"]], "card": {"out": "1..n", "in": "1..1"}, "attrs": []}
def _s(**kw):
    nodes, edges = kw.pop("nodes", [_ST, _Q]), kw.pop("edges", [_ASKS])
    return {"version": kw.pop("version", GRAPH_SCHEMA_VERSION), "nodes": nodes, "edges": edges, **kw}
def _f(**kw): return {"name": "a", "type": "str", "required": True, **kw}
def _nf(*fields): return _s(nodes=[{**_Q, "fields": list(fields)}, _ST])
def _pt(s): return parse_attr_type(s, ctx="t")
def test_schema_question_default_verdict():
    assert registry_from_schema().require_node_kind("question").fields[-1].default == AttrDefault(value="verdict")
def test_null_default_allowed():
    gap = {"name": "gap", "type": "str", "required": False, "default": None}
    assert registry_from_mapping(_nf(gap)).require_node_kind("question").fields[0].default == AttrDefault(value=None)
def test_duplicate_edge_registration():
    spec = KindSpec(kind="e", description="e", fields=())
    reg = KindRegistry()
    reg.register_edge(spec)
    with pytest.raises(ValueError, match="already registered"):
        reg.register_edge(spec)
def test_schema_root_must_be_mapping():
    with pytest.raises(GraphSchemaError, match="root must be a mapping"):
        schema_mapping_from_text("[]")
_R = [
    ("root-key", _s(typo=1), "unknown keys"), ("node-map", _s(nodes=["x", _ST]), "must be a mapping"),
    ("empty-kind", _s(nodes=[{**_Q, "kind": ""}, _ST]), "must be a non-empty str"),
    ("enum-empty", _nf(_f(type="enum(|x)")), "enum values must be non-empty"),
    ("pipe-empty", _nf(_f(type="enum(a||b)")), "enum values must be non-empty"),
    ("list-empty", _nf(_f(type="list()")), r"list\(\) empty"), ("unsupported", _nf(_f(type="nope")), "unsupported"),
    ("ref-kind", _nf(_f(type="ref(missing)")), "unknown ref kind"),
    ("fields-list", _s(nodes=[{**_Q, "fields": "x"}, _ST]), "fields/attrs must be a list"),
    ("field-map", _nf("x"), "must be a mapping"), ("dup-field", _nf(_f(), _f()), "duplicate field name"),
    ("req-bool", _nf(_f(required="yes")), "required must be a bool"),
    ("default", _nf(_f(type="enum(a|b)", default="z")), "default incompatible"),
    ("list-null-elem", _nf(_f(type="list(str)", required=False, default=[None])), "default incompatible"),
    ("dt-naive", _nf(_f(type="datetime", default="2026-01-01T00:00:00")), "default incompatible"),
    ("dt-cal", _nf(_f(type="datetime", default="2026-13-45T99:99:99Z")), "default incompatible"),
    ("attrs-req", _s(edges=[{k: v for k, v in _ASKS.items() if k != "attrs"}]), "missing required key 'attrs'"),
    ("card-map", _s(edges=[{**_ASKS, "card": "1"}]), "card must be a mapping"),
    ("pairs-empty", _s(edges=[{**_ASKS, "pairs": []}]), "pairs must be a non-empty list"),
    ("pair-shape", _s(edges=[{**_ASKS, "pairs": [["study"]]}]), r"must be a \[src, dst\] pair"),
    ("pair-str", _s(edges=[{**_ASKS, "pairs": [["study", ""]]}]), "entries must be non-empty str"),
    ("ver", _s(version="0.4"), "version must be"), ("nodes-empty", _s(nodes=[]), "nodes must be a non-empty list"),
    ("edges-empty", _s(edges=[]), "edges must be a non-empty list"), ("edge-map", _s(edges=["x"]), "must be a mapping"),
]
@pytest.mark.parametrize("data,match", [(d, m) for _, d, m in _R], ids=[i for i, _, _ in _R])
def test_refuse(data, match):
    with pytest.raises(GraphSchemaError, match=match):
        registry_from_mapping(data)
@pytest.mark.parametrize("call,match", [
    (lambda: parse_aware_datetime(datetime(2026, 1, 1), ctx="c"), "expected aware datetime"),
    (lambda: parse_aware_datetime(1, ctx="c"), "got int"),
    (lambda: parse_aware_datetime("2026-13-45T99:99:99Z", ctx="c"), "invalid aware datetime"),
    (lambda: validate_attr_value(_pt("str"), None, null_ok=False, ctx="c"), "null not allowed"),
    (lambda: validate_attr_value(_pt("list(str)"), "x", null_ok=False, ctx="c"), "expected list"),
    (lambda: validate_attr_value(_pt("int"), "x", null_ok=False, ctx="c"), "incompatible"),
])
def test_parse_validate_raises(call, match):
    with pytest.raises(GraphSchemaError, match=match):
        call()
