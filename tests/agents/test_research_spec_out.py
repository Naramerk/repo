"""ResearchSpecOut + SpecQuestion (graph-facing SPEC output)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from coscientist.agents.research_spec import (
    Constraint,
    Goal,
    ResearchSpecOut,
    SpecQuestion,
    SuccessMetric,
    spec_out_question_dicts,
)
from coscientist.core.plan.question_tasks import questions_from_spec_output


def _out(**over: object) -> ResearchSpecOut:
    base: dict[str, object] = {
        "object": "catalyst X",
        "goal": {"statement": "measure X"},
        "scope": "lit 2015+",
        "constraints": [{"subtype": "resource", "content": "budget"}],
        "success_metrics": [
            {"metric": "sources", "op": ">=", "target": "5", "check": "code"}
        ],
        "questions": [{"formulation": "Does X improve Y?", "id": "Q1"}],
    }
    base.update(over)
    return ResearchSpecOut.model_validate(base)


def test_research_spec_out_requires_at_least_one_question() -> None:
    with pytest.raises(ValidationError):
        _out(questions=())


def test_research_spec_out_rejects_empty_questions_list() -> None:
    with pytest.raises(ValidationError):
        _out(questions=[])


def test_research_spec_out_accepts_questions() -> None:
    out = _out(
        questions=[
            {
                "formulation": "Does X improve Y?",
                "id": "Q1",
                "gap": "no meta-analysis",
                "scope": "RCTs",
                "final_type": "verdict",
            }
        ]
    )
    assert len(out.questions) == 1
    assert out.questions[0].gap == "no meta-analysis"
    assert out.object == "catalyst X"


def test_spec_question_rejects_blank_formulation() -> None:
    with pytest.raises(ValidationError):
        SpecQuestion.model_validate({"formulation": ""})


def test_spec_question_rejects_unknown_final_type() -> None:
    with pytest.raises(ValidationError):
        SpecQuestion.model_validate({"formulation": "q?", "final_type": "essay"})


def test_spec_out_question_dicts_maps_for_core_adapter() -> None:
    out = _out(
        questions=[
            SpecQuestion(
                formulation="Does X improve Y?",
                id="Q1",
                final_type="both",
                parent_id=None,
            ),
            SpecQuestion(formulation="What is the mechanism?"),
        ]
    )
    rows = spec_out_question_dicts(out)
    assert rows[0]["id"] == "Q1"
    assert rows[0]["final_type"] == "both"
    assert "id" not in rows[1]
    adapted = questions_from_spec_output({"questions": rows})
    assert adapted[0]["id"] == "Q1"
    assert adapted[0]["text"] == "Does X improve Y?"
    assert adapted[0]["final_type"] == "both"
    assert adapted[1]["id"] == "Q2"
    assert adapted[1]["text"] == "What is the mechanism?"


def test_research_spec_out_round_trip_json() -> None:
    out = _out()
    restored = ResearchSpecOut.model_validate(out.model_dump(mode="json"))
    assert restored == out


def test_research_spec_out_inherits_cap1_fields() -> None:
    out = _out()
    assert isinstance(out.goal, Goal)
    assert isinstance(out.constraints[0], Constraint)
    assert isinstance(out.success_metrics[0], SuccessMetric)
