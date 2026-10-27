"""Operator catalog v5 (heracleum-tox domain): loading, satisfiable surface,
derive substantive gate, and honest pointer evaluation on a real tool receipt.

Bench file: bench/lab/glm/checks_catalog_v5.json. v4 (checks_catalog.json) stays
loadable untouched — historical runs reference it by path.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from coscientist.agents.derive import PlanDeriveError, derive_plan
from coscientist.agents.schemas import QuestionIntent, plan_intent_model_for
from coscientist.checks.catalog import SUPPORTED_CATALOG_VERSIONS, load_catalog
from coscientist.checks.evaluate import evaluate_code_check, role_from_template
from coscientist.cli.run import _satisfiable_criteria_templates
from coscientist.core.claims.graph import default_execution_registry
from coscientist.core.events.errors import CatalogError
from coscientist.core.execution.artifacts import LocalArtifactStore, make_tool_ref
from coscientist.core.execution.domain import (
    AgentTaskOutput,
    ArtifactHandle,
    Need,
    RequirementTemplate,
)

_ROOT = Path(__file__).resolve().parents[2]
V4_CATALOG = _ROOT / "bench" / "lab" / "glm" / "checks_catalog.json"
V5_CATALOG = _ROOT / "bench" / "lab" / "glm" / "checks_catalog_v5.json"

# heracleum-tox tool ids as bound from bench/lab/mcp/heracleum_bundle.json
# (LiveCliToolSurface names are ``server:tool``; bor7 surface is automatic mode,
# so the allow-list is every bound tool).
HERACLEUM_SURFACE = frozenset(
    {
        "heracleum-tox:dataset_overview_heracleum_tox",
        "heracleum-tox:chemical_space_clustering",
        "heracleum-tox:predict_ld50",
        "heracleum-tox:predict_general_toxicity",
        "heracleum-tox:applicability_domain",
        "heracleum-tox:estimate_synthesis_cost",
        "heracleum-tox:predict_molecule_profile",
        "heracleum-tox:model_quality",
        "heracleum-tox:reproduce_all",
        "heracleum-tox:reproduce_claims",
    }
)

NEW_TEMPLATE_IDS = (
    "heracleum_dataset_registered",
    "heracleum_chem_clusters_declared",
    "heracleum_ld50_by_route_table",
    "heracleum_organ_endpoint_predictions",
    "heracleum_applicability_domain_flags",
    "heracleum_reproduction_report",
    "heracleum_synthesis_cost_estimate",
)


@pytest.fixture(scope="module")
def v5_catalog():
    return load_catalog(V5_CATALOG, kind_registry=default_execution_registry())


# ---------------------------------------------------------------------------
# Loading: v5 is current, v4 file still loads untouched
# ---------------------------------------------------------------------------


def test_v5_bench_file_loads_with_domain_templates(v5_catalog) -> None:
    assert v5_catalog.catalog_version == 5
    assert 5 in SUPPORTED_CATALOG_VERSIONS
    by_id = {t.id: t for t in v5_catalog.templates}
    for tid in NEW_TEMPLATE_IDS:
        tmpl = by_id[tid]
        assert tmpl.required is True
        assert tmpl.role == "validity"
        assert tmpl.applies_to == ["experiment"]
    # v4 carryovers keep their ids and pins (SPEC stage naming + coder payload).
    assert by_id["artifact_present"].check.output == "static:ping"
    assert by_id["research_result_payload"].check.output == "coder"
    assert v5_catalog.defaults == {"experiment": ["artifact_present"]}


def test_v4_bench_file_still_loads_untouched() -> None:
    cat = load_catalog(V4_CATALOG, kind_registry=default_execution_registry())
    assert cat.catalog_version == 4
    assert {t.id for t in cat.templates} == {"artifact_present", "research_result_payload"}


def test_new_templates_are_substantive_json_pointer_pins(v5_catalog) -> None:
    by_id = {t.id: t for t in v5_catalog.templates}
    for tid in NEW_TEMPLATE_IDS:
        check = by_id[tid].check
        assert check.kind == "json_pointer"
        assert check.output in HERACLEUM_SURFACE
        assert check.pointer is not None and check.pointer.startswith("/result/structuredContent")


# ---------------------------------------------------------------------------
# Satisfiable surface (PR254 filter) sees the domain templates
# ---------------------------------------------------------------------------


def test_satisfiable_filter_keeps_domain_templates_on_heracleum_surface(v5_catalog) -> None:
    kept = {t.id for t in _satisfiable_criteria_templates(v5_catalog, HERACLEUM_SURFACE)}
    assert set(NEW_TEMPLATE_IDS) <= kept
    # Dead pins stay filtered on the live surface: no static:ping tool, coder
    # not attached (remote-coder slice 2 pending).
    assert "artifact_present" not in kept
    assert "research_result_payload" not in kept


def test_satisfiable_filter_drops_domain_templates_without_heracleum_tools(v5_catalog) -> None:
    kept = {t.id for t in _satisfiable_criteria_templates(v5_catalog, frozenset({"papers-search:search_papers"}))}
    assert kept == set()  # empty set → criteria surface closes (schema forbids criteria)


def test_satisfiable_filter_admits_coder_pin_only_with_coder(v5_catalog) -> None:
    kept = {t.id for t in _satisfiable_criteria_templates(v5_catalog, HERACLEUM_SURFACE | {"coder"})}
    assert "research_result_payload" in kept
    # static:ping becomes satisfiable only on a cassette surface that binds it.
    static = {t.id for t in _satisfiable_criteria_templates(v5_catalog, frozenset({"static:ping"}))}
    assert static == {"artifact_present"}


# ---------------------------------------------------------------------------
# Derive substantive gate: a typical hogweed plan passes; the attempt-12 wall
# stays honest for plans without catalogue criteria
# ---------------------------------------------------------------------------


def _pins(v5_catalog) -> tuple[tuple[str, str], ...]:
    return tuple(
        sorted(
            (t.id, t.check.output)
            for t in v5_catalog.templates
            if t.check.output  # type: ignore[union-attr]
        )
    )


def _hogweed_intent_model(v5_catalog):
    kept = _satisfiable_criteria_templates(v5_catalog, HERACLEUM_SURFACE)
    return plan_intent_model_for(
        tuple(sorted(HERACLEUM_SURFACE)),
        tuple(sorted(t.id for t in kept)),
        _pins(v5_catalog),
    )


_NEED_OFF = Need(kind="need", needed=False, reason="off")


def _hogweed_intent(*criteria_by_tool: tuple[str, tuple[str, ...]]):
    """Task rows as plain dicts — the context-typed model derives new TaskIntent
    subclasses per surface (same style as tests/agents/test_derive.py)."""
    return tuple(
        {
            "goal": f"tox profile step {tool}",
            "route": "experiment",
            "tools_hint": (tool,),
            "criteria": criteria,
            "question_indices": (1,),
        }
        for tool, criteria in criteria_by_tool
    )


def test_derive_substantive_gate_passes_on_hogweed_plan(v5_catalog) -> None:
    """Typical borshhevik plan: scientific questions addressed by tasks citing
    the domain catalogue checks for the tools they actually call."""
    model = _hogweed_intent_model(v5_catalog)
    question = QuestionIntent(
        text="Каков in-silico токсикологический профиль метаболитов Heracleum sosnowskyi?",
        final_type="verdict",
    )
    intent = model(
        tasks=_hogweed_intent(
            ("heracleum-tox:dataset_overview_heracleum_tox", ("heracleum_dataset_registered",)),
            ("heracleum-tox:chemical_space_clustering", ("heracleum_chem_clusters_declared",)),
            ("heracleum-tox:predict_ld50", ("heracleum_ld50_by_route_table",)),
            ("heracleum-tox:predict_general_toxicity", ("heracleum_organ_endpoint_predictions",)),
            ("heracleum-tox:applicability_domain", ("heracleum_applicability_domain_flags",)),
            ("heracleum-tox:reproduce_all", ("heracleum_reproduction_report",)),
        ),
        questions=(question,),
        hypotheses=_NEED_OFF,
        literature=_NEED_OFF,
    )
    plan = derive_plan(
        intent,
        catalog_templates=v5_catalog.templates,
        tool_ids=sorted(HERACLEUM_SURFACE),
    )
    assert len(plan.tasks) == 6
    for spec in plan.tasks:
        (output,) = spec.expected_outputs
        assert output.name in HERACLEUM_SURFACE
        assert len(spec.criteria) == 1


def test_derive_substantive_gate_refuses_plan_without_criteria(v5_catalog) -> None:
    """The attempt-12 refusal must stay: a scientific verdict task with no
    catalogue criterion cannot pass derivation (no gate weakening).
    The bar is per question now — with no substantive task at all the verdict
    question stays uncovered and derive refuses, naming the question."""
    tasks = _hogweed_intent(
        ("heracleum-tox:predict_ld50", ()),
        ("heracleum-tox:applicability_domain", ()),
    )
    intent = plan_intent_model_for(tuple(sorted(HERACLEUM_SURFACE)), (), ())(
        tasks=tasks,
        questions=(QuestionIntent(text="LD50-профиль?", final_type="verdict"),),
        hypotheses=_NEED_OFF,
        literature=_NEED_OFF,
    )
    with pytest.raises(PlanDeriveError, match="question 'q1' .*substantive catalogue check"):
        derive_plan(
            intent,
            catalog_templates=v5_catalog.templates,
            tool_ids=sorted(HERACLEUM_SURFACE),
        )


def test_derive_evidence_only_task_may_coaddress_verdict_question(v5_catalog) -> None:
    """Attempts 15/16 walls (legacy port): a task without criteria (literature
    grounding, synthesis-cost scan) may co-address a verdict question as an
    evidence deliverable while one covering task carries the substantive
    catalogue criterion — per-question coverage passes derivation."""
    model = _hogweed_intent_model(v5_catalog)
    intent = model(
        tasks=_hogweed_intent(
            ("heracleum-tox:predict_ld50", ("heracleum_ld50_by_route_table",)),
            ("heracleum-tox:estimate_synthesis_cost", ()),
        ),
        questions=(QuestionIntent(text="LD50-профиль?", final_type="verdict"),),
        hypotheses=_NEED_OFF,
        literature=_NEED_OFF,
    )
    plan = derive_plan(
        intent,
        catalog_templates=v5_catalog.templates,
        tool_ids=sorted(HERACLEUM_SURFACE),
    )
    assert [list(t.criteria) for t in plan.tasks] == [["heracleum_ld50_by_route_table"], []]


def test_derive_synthesis_cost_criterion_covers_verdict_question(v5_catalog) -> None:
    """The synthesis-cost template is itself substantive (json_pointer pin):
    a cost task citing it covers the verdict question, and a second
    evidence-only co-addressing task needs no criteria."""
    model = _hogweed_intent_model(v5_catalog)
    intent = model(
        tasks=_hogweed_intent(
            ("heracleum-tox:estimate_synthesis_cost", ("heracleum_synthesis_cost_estimate",)),
            ("heracleum-tox:reproduce_all", ()),
        ),
        questions=(QuestionIntent(text="Стоимость синтеза?", final_type="both"),),
        hypotheses=_NEED_OFF,
        literature=_NEED_OFF,
    )
    plan = derive_plan(
        intent,
        catalog_templates=v5_catalog.templates,
        tool_ids=sorted(HERACLEUM_SURFACE),
    )
    assert [list(t.criteria) for t in plan.tasks] == [["heracleum_synthesis_cost_estimate"], []]


def test_planner_schema_refuses_mismatched_criterion_pin(v5_catalog) -> None:
    """Citing the LD50 check on a task that never declares the pinned output
    fails at schema parse (cheap repair), same contract derive enforces."""
    model = _hogweed_intent_model(v5_catalog)
    with pytest.raises(ValidationError, match="requires output"):
        model(
            tasks=_hogweed_intent(
                ("heracleum-tox:applicability_domain", ("heracleum_ld50_by_route_table",)),
            ),
            questions=(QuestionIntent(text="AD-профиль?", final_type="verdict"),),
            hypotheses=_NEED_OFF,
            literature=_NEED_OFF,
        )


# ---------------------------------------------------------------------------
# Honest executability: json_pointer against a realistic MCP tool receipt
# ---------------------------------------------------------------------------


def _receipt_body(answer: dict[str, object] | None) -> bytes:
    """Artifact body exactly as cli/tool_execution.py stores it:
    ``json.dumps(output.data)`` with ``data = {result, content_digest, files}``
    and ``result`` the raw MCP CallToolResult wire object."""
    result: dict[str, object] = {"content": [{"type": "text", "text": "..."}], "isError": False}
    if answer is not None:
        reference = "Rassabina & Fedorov, Plants 2025, 14, 3253"
        result["structuredContent"] = {"answer": answer, "metadata": {"reference": reference}}
    return json.dumps(
        {"result": result, "content_digest": "0" * 64, "files": []},
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")


def _evaluate_pinned(v5_catalog, tid: str, body: bytes, tmp_path: Path):
    tmpl: RequirementTemplate = next(t for t in v5_catalog.templates if t.id == tid)
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
        outputs={str(tmpl.check.output): ArtifactHandle(artifact_id=art.artifact_id, name=str(tmpl.check.output))},
    )
    return evaluate_code_check(
        tmpl.check,  # type: ignore[arg-type]
        template_id=tid,
        check_id=f"{tid}#0",
        out=out,
        artifacts=arts,
        values={},
        role=role_from_template(tmpl),
        attempt_id=attempt_id,
    )


# Real predict_ld50 answer shape (legacy cossession 20261013, heracleum-tox).
_LD50_ANSWER: dict[str, object] = {
    "cluster_ranking_most_to_least_toxic": [
        {"cluster": "E", "median_ld50_mgkg": 628.0, "n": 22},
        {"cluster": "D", "median_ld50_mgkg": 1475.0, "n": 21},
    ],
    "median_ld50_mgkg_by_cluster_and_route": {
        "oral": {"E": 627.9},
        "iv": {"E": 627.9},
        "ip": {"E": 627.9},
        "sc": {"E": 627.9},
        "skin": {"E": 627.9},
        "im": {"E": 627.9},
    },
    "most_toxic_cluster": "E",
    "finding": "cluster E (furanocoumarins) is the most toxic",
}


def test_ld50_route_table_pointer_mets_on_real_receipt(tmp_path: Path, v5_catalog) -> None:
    result = _evaluate_pinned(v5_catalog, "heracleum_ld50_by_route_table", _receipt_body(_LD50_ANSWER), tmp_path)
    assert result.status.name == "MET"
    assert result.evidence and result.evidence[0].json_pointer == (
        "/result/structuredContent/answer/median_ld50_mgkg_by_cluster_and_route"
    )


def test_missing_structured_content_fails_indeterminate(tmp_path: Path, v5_catalog) -> None:
    """A text-only / error receipt never counts as evidence: pointer_empty →
    FAILED with no numeric effect (honest indeterminate, fail-closed)."""
    result = _evaluate_pinned(v5_catalog, "heracleum_ld50_by_route_table", _receipt_body(None), tmp_path)
    assert result.status.name == "FAILED"
    assert result.detail == "pointer_empty"
    assert result.effect is None


def test_output_not_submitted_stays_honest(tmp_path: Path, v5_catalog) -> None:
    """No tool call, no evidence: the pinned output is absent → FAILED."""
    tmpl = next(t for t in v5_catalog.templates if t.id == "heracleum_ld50_by_route_table")
    arts = LocalArtifactStore(tmp_path / "arts")
    out = AgentTaskOutput(summary="x", outputs={})
    result = evaluate_code_check(
        tmpl.check,  # type: ignore[arg-type]
        template_id="heracleum_ld50_by_route_table",
        check_id="heracleum_ld50_by_route_table#0",
        out=out,
        artifacts=arts,
        values={},
        role=role_from_template(tmpl),
    )
    assert result.status.name == "FAILED"
    assert result.detail == "output_not_submitted"
    assert result.effect is None


# The estimate_synthesis_cost answer shape is not confirmed upstream; the
# template honestly checks presence of machine-readable evidence only
# (non-empty structuredContent.answer), not specific fields or cost truth.


def test_synthesis_cost_pointer_mets_on_machine_readable_answer(tmp_path: Path, v5_catalog) -> None:
    result = _evaluate_pinned(
        v5_catalog,
        "heracleum_synthesis_cost_estimate",
        _receipt_body({"any_machine_readable_payload": {"route": "unconfirmed-shape"}}),
        tmp_path,
    )
    assert result.status.name == "MET"
    assert result.evidence and result.evidence[0].json_pointer == "/result/structuredContent/answer"


def test_synthesis_cost_without_structured_answer_fails_indeterminate(tmp_path: Path, v5_catalog) -> None:
    """A text-only / error receipt never counts as evidence: pointer_empty →
    FAILED with no numeric effect (honest indeterminate, fail-closed)."""
    result = _evaluate_pinned(v5_catalog, "heracleum_synthesis_cost_estimate", _receipt_body(None), tmp_path)
    assert result.status.name == "FAILED"
    assert result.detail == "pointer_empty"
    assert result.effect is None


def test_v5_catalog_refuses_only_out_of_chain_versions() -> None:
    """The v5 loader keeps refusing generations outside the chain."""
    with pytest.raises(CatalogError, match="unsupported_catalog_version:99"):
        load_catalog(
            {"catalog_version": 99, "edition": "x"},
            kind_registry=default_execution_registry(),
        )
