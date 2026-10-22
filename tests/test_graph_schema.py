import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import gen_model_tables as g  # noqa: E402


def test_doc_tables_match_schema():
    doc = g.DOC.read_text(encoding="utf-8")
    assert g.apply(doc, g.render(g.load())) == doc, "drift: run scripts/gen_model_tables.py"


def test_rule_ids_are_reviewed():
    lines = (ROOT / "tests" / "reviewed_rule_ids.txt").read_text(encoding="utf-8").splitlines()
    known = {x.strip() for x in lines if x.strip() and not x.startswith("#")}
    missing = [r["rule_id"] for r in g.load()["rules"] if r["rule_id"] not in known]
    assert not missing, f"rule_id not in reviewed list: {missing}"


def test_edges_reference_known_kinds():
    s = g.load()
    kinds = {n["kind"] for n in s["nodes"]}
    for e in s["edges"]:
        assert set(e["from"]) | set(e["to"]) <= kinds, e["name"]


def test_design_nodekind_matches_schema():
    import re
    design = (ROOT / "docs" / "DESIGN.md").read_text(encoding="utf-8")
    block = design[design.index("class NodeKind(StrEnum):"):design.index("class Study(")]
    assert set(re.findall(r'= "(\w+)"', block)) == {n["kind"] for n in g.load()["nodes"]}
