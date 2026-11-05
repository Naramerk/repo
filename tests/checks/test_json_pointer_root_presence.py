"""Root-presence semantics for json_pointer checks with pointer ``/``.

Live №25 → №29 chain: the v6 catalog turned ``research_result_payload`` into
a presence check («указатель на корень артефакта», pointer ``/``), but the
evaluator treated ``/`` (and empty/None) as an instant ``pointer_empty``
refusal — RFC 6901 reads ``/`` as the empty-string key, which no receipt
object carries, so the required validity check failed on every run regardless
of content (task PARTIAL, artifact questions ``rubric_not_met``). The
presence contract is "output registered and parses to a non-null document";
the pointer result guard below already judges exactly that.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from coscientist.checks.catalog import load_catalog
from coscientist.checks.evaluate import evaluate_code_check, role_from_template
from coscientist.core.claims.graph import default_execution_registry
from coscientist.core.execution.artifacts import LocalArtifactStore, make_tool_ref
from coscientist.core.execution.domain import AgentTaskOutput, ArtifactHandle

_ROOT = Path(__file__).resolve().parents[2]
V6_CATALOG = _ROOT / "bench" / "lab" / "glm" / "checks_catalog_v6.json"


@pytest.fixture(scope="module")
def v6_catalog():
    return load_catalog(V6_CATALOG, kind_registry=default_execution_registry())


def _receipt_body(answer: dict[str, object] | None) -> bytes:
    """Artifact body exactly as cli/tool_execution.py stores it."""
    result: dict[str, object] = {"content": [{"type": "text", "text": "..."}], "isError": False}
    if answer is not None:
        result["structuredContent"] = {
            "answer": answer,
            "metadata": {"reference": "Rassabina & Fedorov, Plants 2025, 14, 3253"},
        }
    return json.dumps(
        {"result": result, "content_digest": "0" * 64, "files": []},
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")


def _evaluate(tmpl, body: bytes, tmp_path: Path):
    arts = LocalArtifactStore(tmp_path / "arts")
    attempt_id = "aid1"
    art = arts.put(
        body,
        make_tool_ref(f"{attempt_id}:{tmpl.check.output}", tool=str(tmpl.check.output)),
        name=str(tmpl.check.output),
        media_type="application/json",
        provenance="tool",
    )
    out = AgentTaskOutput(
        summary="x",
        outputs={
            str(tmpl.check.output): ArtifactHandle(
                artifact_id=art.artifact_id, name=str(tmpl.check.output)
            )
        },
    )
    return evaluate_code_check(
        tmpl.check,
        template_id=tmpl.id,
        check_id=f"{tmpl.id}#0",
        out=out,
        artifacts=arts,
        values={},
        role=role_from_template(tmpl),
        attempt_id=attempt_id,
    )


def test_v6_payload_template_is_root_presence(v6_catalog) -> None:
    tmpl = next(t for t in v6_catalog.templates if t.id == "research_result_payload")
    assert tmpl.check.pointer == "/"
    assert tmpl.check.output == "coder"


def test_root_presence_mets_on_registered_receipt(v6_catalog, tmp_path: Path) -> None:
    """A registered, parseable receipt document IS the presence contract —
    the whole document (evidence pointer None), not the RFC 6901
    empty-string key (live №29)."""
    tmpl = next(t for t in v6_catalog.templates if t.id == "research_result_payload")
    result = _evaluate(tmpl, _receipt_body({"any": "payload"}), tmp_path)
    assert result.status.name == "MET"
    assert result.evidence and result.evidence[0].json_pointer is None
    assert result.detail is None


def test_root_presence_fails_on_null_document(v6_catalog, tmp_path: Path) -> None:
    tmpl = next(t for t in v6_catalog.templates if t.id == "research_result_payload")
    result = _evaluate(tmpl, b"null", tmp_path)
    assert result.status.name == "FAILED"
    assert result.detail == "pointer_empty"
    assert result.effect is None
