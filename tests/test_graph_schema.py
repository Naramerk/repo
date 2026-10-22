import hashlib
import json
import re

import gen_model_tables as g
from conftest import ROOT

VENDOR = ROOT / "vendor" / "ai4research"
S = g.load()


def _vendored(name):
    sums = dict(reversed(line.split()) for line in (VENDOR / "SHA256SUMS").read_text().splitlines() if line)
    data = (VENDOR / name).read_bytes()
    assert hashlib.sha256(data).hexdigest() == sums[name], f"{name} changed; re-vendor with SHA256SUMS"
    return json.loads(data)


def _design_literal(cls, attr):
    design = (ROOT / "docs" / "DESIGN.md").read_text(encoding="utf-8")
    m = re.search(rf"class {cls}\(.*?{attr}: Literal\[(.*?)\]", design, re.S)
    return set(re.findall(r'"(\w+)"', m.group(1)))


def test_doc_tables_match_schema():
    doc = g.DOC.read_text(encoding="utf-8")
    assert g.apply(doc, g.render(S)) == doc, "drift: run scripts/gen_model_tables.py"


def test_rule_ids_are_reviewed():
    known = {r["rule_id"] for r in _vendored("reviewed-rules.json")["rules"]}
    assert {r["rule_id"] for r in S["rules"]} <= known


def test_volume_b_ids_match_metamodel_sources():
    ids = {x["id"] for x in _vendored("research_metamodel_sources.json")}
    assert len(S["volume_b"]) == 14 and {x["id"] for x in S["volume_b"]} == ids


def test_volume_b_check_kinds_and_nodes():
    kinds = {n["kind"] for n in S["nodes"]}
    assert set(S["code_check_kinds"]) == _design_literal("CodeCheck", "kind")
    for x in S["volume_b"]:
        assert set(x["check_kinds"]) <= set(S["code_check_kinds"]), x["id"]
        assert set(x["nodes"]) <= kinds, x["id"]
    for r in S["rules"]:
        assert {n.split("(")[0].split(".")[0] for n in r["nodes"]} <= kinds | {"NodeRequirement", "все"}, r["rule_id"]


def test_edge_pairs_reference_known_kinds():
    kinds = {n["kind"] for n in S["nodes"]}
    card = re.compile(r"^\d+\.\.(\d+|n)$")
    for e in S["edges"]:
        assert all(a in kinds and b in kinds for a, b in e["pairs"]), e["name"]
        assert card.match(e["card"]["out"]) and card.match(e["card"]["in"]), e["name"]


def test_design_nodekind_matches_schema():
    design = (ROOT / "docs" / "DESIGN.md").read_text(encoding="utf-8")
    block = design[design.index("class NodeKind(StrEnum):"):design.index("class Study(")]
    assert set(re.findall(r'= "(\w+)"', block)) == {n["kind"] for n in S["nodes"]}


def test_pydantic_models_generate():
    m = g.models(S)
    assert set(m) == {n["kind"] for n in S["nodes"]}
    m["claim"](target="H1", checked_against="CR1", stance="supports")
