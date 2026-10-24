"""Tests for G7 graph writers (digest, attrs, endpoint pair-check)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from coscientist.core.claims.graph import (
    GraphEdge,
    GraphNode,
    KindRegistry,
    KindSpec,
    registry_from_mapping,
    registry_from_schema,
)
from coscientist.core.claims.graph_writer import GraphWriterError, graph_edge_added, graph_node_upserted
from coscientist.core.events.upcasters import writer_schema_version
from coscientist.schema import GRAPH_SCHEMA_VERSION

TS = datetime(2026, 1, 1, tzinfo=UTC)
_ST = {
    "kind": "study",
    "prefix": "ST",
    "text_ru": "s",
    "fields": [{"name": "request", "type": "str", "required": True}],
}
_ASKS = {"name": "asks", "pairs": [["study", "typed"]], "card": {"out": "1..1", "in": "1..1"}, "attrs": []}


def _typed_reg(*, field: dict):
    node = {"kind": "typed", "prefix": "T", "text_ru": "t", "fields": [field]}
    return registry_from_mapping({"version": GRAPH_SCHEMA_VERSION, "nodes": [_ST, node], "edges": [_ASKS]})


@pytest.fixture
def reg():
    return registry_from_schema()


def _q(formulation: str = "why?", *, at=TS, study_id: str = "st1") -> GraphNode:
    return GraphNode(
        node_id="st1:question:q1",
        kind="question",
        study_id=study_id,
        attrs={"formulation": formulation},
        created_at=at,
    )


def _st(*, study_id: str = "st1") -> GraphNode:
    return GraphNode(
        node_id="st1:study",
        kind="study",
        study_id=study_id,
        created_at=TS,
        attrs={
            "request": "r",
            "goal": {"statement": "g", "research_form": "x", "trl": 1},
            "profile": "research",
            "catalog_version": "1",
        },
    )


def _nkw(reg, **kw):
    return {"node": _q(), "ts": TS, "registry": reg, "idem_extra": (), **kw}


def _ekw(reg, *, edge, src, dst, **kw):
    return {"edge": edge, "ts": TS, "src": src, "dst": dst, "registry": reg, "idem_extra": (), **kw}


def _asks_kw(reg, **kw):
    src, dst = _st(), _q()
    edge = GraphEdge(edge_id="a1", kind="asks", study_id="st1", src_id=src.node_id, dst_id=dst.node_id, created_at=TS)
    return _ekw(reg, edge=edge, src=src, dst=dst, **kw)


def test_node_schema_version(reg):
    assert graph_node_upserted(**_nkw(reg)).schema_version == writer_schema_version("GraphNodeUpserted")


def test_node_digest_ignores_created_at(reg):
    ev = graph_node_upserted(**_nkw(reg))
    later = graph_node_upserted(**_nkw(reg, node=_q(at=TS + timedelta(seconds=9))))
    assert later.idem_key == ev.idem_key


def test_node_payload_includes_created_at(reg):
    assert "created_at" in graph_node_upserted(**_nkw(reg)).payload


def test_node_digest_changes_with_attrs(reg):
    ev = graph_node_upserted(**_nkw(reg))
    assert graph_node_upserted(**_nkw(reg, node=_q("x"))).idem_key != ev.idem_key


_NODE_REFUSE = [
    ("empty-study", {"node": _q(study_id="")}, r"node\.study_id must be non-empty"),
    (
        "missing-attrs",
        {"node": GraphNode(node_id="q", kind="question", study_id="st1", attrs={})},
        r"missing required attrs",
    ),
    (
        "unknown-attrs",
        {"node": GraphNode(node_id="q", kind="question", study_id="st1", attrs={"formulation": "x", "nope": 1})},
        r"unknown attrs",
    ),
    (
        "unknown-kind",
        {"node": GraphNode(node_id="q", kind="nope", study_id="st1", attrs={})},
        r"unknown node kind",
    ),
]


@pytest.mark.parametrize("kwargs,match", [(k, m) for _, k, m in _NODE_REFUSE], ids=[i for i, _, _ in _NODE_REFUSE])
def test_node_refuses(reg, kwargs, match):
    with pytest.raises(GraphWriterError, match=match):
        graph_node_upserted(**_nkw(reg, **kwargs))


def test_attrs_on_fieldless_kind_refused():
    reg = KindRegistry()
    reg.register_node(KindSpec(kind="bare", layer="research", schema_ref=None, description="b", prefix="B", fields=()))
    node = GraphNode(node_id="n", kind="bare", study_id="st1", attrs={"x": 1})
    with pytest.raises(GraphWriterError, match="has no schema fields but attrs"):
        graph_node_upserted(**_nkw(reg, node=node))


_TYPE_REFUSE = [
    ("float", 1, r"v: expected type float, got int"),
    ("datetime", "2026-01-01T00:00:00", r"v: expected type datetime, got str"),
    ("datetime", "2026-13-45T99:99:99Z", r"v: expected type datetime, got str"),
    ("bool", 1, r"v: expected type bool, got int"),
    ("digest", 1, r"v: expected type digest, got int"),
    ("obj(Goal)", ["x"], r"v: expected type obj\(Goal\), got list"),
    ("ref(study)", "", r"v: expected type ref\(study\), got str"),
    ("int", True, r"v: expected type int, got bool"),
]


@pytest.mark.parametrize(
    "type_s,bad,match",
    _TYPE_REFUSE,
    ids=["float", "dt-naive", "dt-cal", "bool", "digest", "obj", "ref", "int"],
)
def test_attr_types_refuse(type_s, bad, match):
    reg = _typed_reg(field={"name": "v", "type": type_s, "required": True})
    node = GraphNode(node_id="n1", kind="typed", study_id="st1", attrs={"v": bad})
    with pytest.raises(GraphWriterError, match=match):
        graph_node_upserted(**_nkw(reg, node=node))


_TYPE_OK = [
    ("float", 1.5),
    ("datetime", "2026-01-01T00:00:00Z"),
    ("bool", True),
    ("digest", "abc"),
    ("obj(Goal)", {"statement": "g"}),
    ("ref(study)", "st1:study"),
    ("int", 2),
]


@pytest.mark.parametrize("type_s,good", _TYPE_OK, ids=[t for t, _ in _TYPE_OK])
def test_attr_types_accept(type_s, good):
    reg = _typed_reg(field={"name": "v", "type": type_s, "required": True})
    node = GraphNode(node_id="n1", kind="typed", study_id="st1", attrs={"v": good})
    assert graph_node_upserted(**_nkw(reg, node=node)).payload["attrs"]["v"] == good


def test_attr_default_applied():
    reg = _typed_reg(field={"name": "v", "type": "enum(a|b)", "required": False, "default": "a"})
    node = GraphNode(node_id="n1", kind="typed", study_id="st1", attrs={})
    assert graph_node_upserted(**_nkw(reg, node=node)).payload["attrs"]["v"] == "a"


def test_null_default_written():
    reg = _typed_reg(field={"name": "v", "type": "str", "required": False, "default": None})
    node = GraphNode(node_id="n1", kind="typed", study_id="st1", attrs={})
    assert graph_node_upserted(**_nkw(reg, node=node)).payload["attrs"]["v"] is None


def test_null_default_explicit_null_accepted():
    reg = _typed_reg(field={"name": "v", "type": "str", "required": True, "default": None})
    node = GraphNode(node_id="n1", kind="typed", study_id="st1", attrs={"v": None})
    assert graph_node_upserted(**_nkw(reg, node=node)).payload["attrs"]["v"] is None


def test_null_without_null_default_refused():
    reg = _typed_reg(field={"name": "v", "type": "str", "required": False})
    node = GraphNode(node_id="n1", kind="typed", study_id="st1", attrs={"v": None})
    with pytest.raises(GraphWriterError, match=r"v: expected type str, got NoneType"):
        graph_node_upserted(**_nkw(reg, node=node))


def test_required_null_without_null_default_refused():
    reg = _typed_reg(field={"name": "v", "type": "str", "required": True})
    node = GraphNode(node_id="n1", kind="typed", study_id="st1", attrs={"v": None})
    with pytest.raises(GraphWriterError, match=r"v: expected type str, got NoneType"):
        graph_node_upserted(**_nkw(reg, node=node))


def test_edge_digest_ignores_created_at(reg):
    kw = _asks_kw(reg)
    ev = graph_edge_added(**kw)
    edge2 = kw["edge"].model_copy(update={"created_at": TS + timedelta(hours=1)})
    assert graph_edge_added(**{**kw, "edge": edge2}).idem_key == ev.idem_key


def test_edge_idem_extra(reg):
    kw = _asks_kw(reg)
    ev = graph_edge_added(**kw)
    assert graph_edge_added(**{**kw, "idem_extra": ("r",)}).idem_key == f"{ev.idem_key}:r"


def test_edge_refuses_pair_not_allowed(reg):
    dst = _q()
    claim = GraphNode(node_id="c1", kind="claim", study_id="st1", attrs={"target": "q", "checked_against": "cr"})
    bad = GraphEdge(edge_id="bad", kind="asks", study_id="st1", src_id=claim.node_id, dst_id=dst.node_id)
    with pytest.raises(GraphWriterError, match=r"not allowed"):
        graph_edge_added(**_ekw(reg, edge=bad, src=claim, dst=dst))


_SRC, _DST = _st(), _q()
_EBASE = {"edge_id": "e", "kind": "asks", "study_id": "st1", "src_id": _SRC.node_id, "dst_id": _DST.node_id}
_EDGE_REFUSE = [
    ("unknown-kind", GraphEdge(**{**_EBASE, "kind": "nope"}), _SRC, _DST, r"unknown edge kind"),
    ("empty-src", GraphEdge(**{**_EBASE, "src_id": ""}), _SRC, _DST, r"edge\.src_id must be non-empty"),
    ("empty-dst-kind", GraphEdge(**_EBASE), _SRC, _DST.model_copy(update={"kind": ""}), r"dst\.kind must be non-empty"),
    ("src-id-mismatch", GraphEdge(**{**_EBASE, "src_id": "other"}), _SRC, _DST, r"src\.node_id"),
    ("dst-id-mismatch", GraphEdge(**{**_EBASE, "dst_id": "other"}), _SRC, _DST, r"dst\.node_id"),
    ("src-study", GraphEdge(**_EBASE), _SRC.model_copy(update={"study_id": "other"}), _DST, r"src\.study_id"),
    ("dst-study", GraphEdge(**_EBASE), _SRC, _DST.model_copy(update={"study_id": "other"}), r"dst\.study_id"),
    ("unknown-src-kind", GraphEdge(**_EBASE), _SRC.model_copy(update={"kind": "nope"}), _DST, r"unknown node kind"),
    ("unknown-dst-kind", GraphEdge(**_EBASE), _SRC, _DST.model_copy(update={"kind": "nope"}), r"unknown node kind"),
]


@pytest.mark.parametrize(
    "edge,src,dst,match",
    [(e, s, d, m) for _, e, s, d, m in _EDGE_REFUSE],
    ids=[i for i, *_ in _EDGE_REFUSE],
)
def test_edge_refuses(reg, edge, src, dst, match):
    with pytest.raises(GraphWriterError, match=match):
        graph_edge_added(**_ekw(reg, edge=edge, src=src, dst=dst))
