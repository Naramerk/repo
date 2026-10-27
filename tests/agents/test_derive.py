"""Derivation: PlanIntent → PlanOut (naming, auto-rubric, empty criteria, refusal)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from coscientist.agents.derive import PlanDeriveError, derive_plan, derive_task_specs
from coscientist.agents.schemas import PlanIntent, QuestionIntent, TaskIntent, plan_intent_model_for
from coscientist.cli.run import _to_task_spec
from coscientist.core.execution.domain import Need, QuestionSpec, TaskSpec
from tests.checks.minimal_test_catalog import minimal_test_catalog
from tests.helpers import close_runtime, open_runtime

_NEED_OFF = Need(kind="need", needed=False, reason="off")

_TOOLS = ("static:dataset_overview", "static:chemical_space_clustering")

_CATALOG = minimal_test_catalog()
_DEFAULT_QUESTIONS = (QuestionIntent(text="two tasks?", final_type="artifact"),)


def _intent(
    tasks: tuple[TaskIntent, ...],
    questions: tuple[QuestionIntent, ...] = _DEFAULT_QUESTIONS,
) -> PlanIntent:
    return PlanIntent(
        tasks=tuple(t.model_copy(update={"question_indices": (1,)}) for t in tasks),
        questions=questions,
        hypotheses=_NEED_OFF,
        literature=_NEED_OFF,
    )


def test_output_named_after_tool_id_with_auto_rubric() -> None:
    specs = derive_task_specs(
        _intent((TaskIntent(goal="overview", route="experiment", tools_hint=("static:dataset_overview",)),)),
        catalog_templates=_CATALOG.templates,
        tool_ids=_TOOLS,
    )
    assert len(specs) == 1
    spec = specs[0]
    assert spec.task_key == "t1"
    assert spec.title == "t1"
    (out,) = spec.expected_outputs
    assert out.name == "static:dataset_overview"
    assert out.media_type == "application/json"
    assert out.required is True
    assert out.rubric is not None
    (item,) = out.rubric.items
    assert item.check == "code"
    assert item.code_kind == "artifact_exists"
    assert out.rubric.aggregate == "all_required"


def test_criteria_defaulted_empty() -> None:
    specs = derive_task_specs(
        _intent((TaskIntent(goal="g", route="experiment", tools_hint=_TOOLS),)),
        catalog_templates=_CATALOG.templates,
        tool_ids=_TOOLS,
    )
    assert len(specs[0].expected_outputs) == 2
    assert [o.name for o in specs[0].expected_outputs] == list(_TOOLS)
    assert specs[0].criteria == ()


def test_depends_on_wiring_and_planner_keys() -> None:
    specs = derive_task_specs(
        _intent(
            (
                TaskIntent(goal="g1", route="experiment", tools_hint=("static:dataset_overview",), key="prep"),
                TaskIntent(
                    goal="g2",
                    route="experiment",
                    tools_hint=("static:chemical_space_clustering",),
                    depends_on=("prep",),
                ),
            )
        ),
        catalog_templates=_CATALOG.templates,
        tool_ids=_TOOLS,
    )
    assert [s.task_key for s in specs] == ["prep", "t2"]
    assert specs[0].depends_on == ()
    assert specs[1].depends_on == ("prep",)


def test_does_not_invent_coverage_for_unbound_question() -> None:
    with pytest.raises(PlanDeriveError, match="without explicitly addressing"):
        derive_plan(
            _intent(
                (TaskIntent(goal="g", route="experiment", tools_hint=("static:dataset_overview",)),),
                questions=(
                    QuestionIntent(text="a?", final_type="artifact"),
                    QuestionIntent(text="b?", final_type="artifact"),
                ),
            ),
            catalog_templates=_CATALOG.templates,
            tool_ids=_TOOLS,
        )


def test_unknown_tool_refuses() -> None:
    with pytest.raises(PlanDeriveError, match="not in the study tool surface"):
        derive_task_specs(
            _intent((TaskIntent(goal="g", route="experiment", tools_hint=("static:nope",)),)),
            catalog_templates=_CATALOG.templates,
            tool_ids=_TOOLS,
        )


def test_unknown_dep_refuses() -> None:
    with pytest.raises(PlanDeriveError, match="depends_on"):
        derive_task_specs(
            _intent((TaskIntent(goal="g", route="experiment", depends_on=("ghost",)),)),
            catalog_templates=_CATALOG.templates,
            tool_ids=_TOOLS,
        )


def test_self_dep_refuses() -> None:
    with pytest.raises(PlanDeriveError, match="depends_on itself"):
        derive_task_specs(
            _intent((TaskIntent(goal="g", route="experiment", key="a", depends_on=("a",)),)),
            catalog_templates=_CATALOG.templates,
            tool_ids=_TOOLS,
        )


def test_duplicate_keys_refuse() -> None:
    with pytest.raises(PlanDeriveError, match="unique"):
        derive_task_specs(
            _intent(
                (
                    TaskIntent(goal="g", route="experiment", key="x"),
                    TaskIntent(goal="g", route="experiment", key="x"),
                )
            ),
            catalog_templates=_CATALOG.templates,
            tool_ids=_TOOLS,
        )


def test_bad_key_shape_refuses() -> None:
    with pytest.raises(PlanDeriveError, match="must match"):
        derive_task_specs(
            _intent((TaskIntent(goal="g", route="experiment", key="bad key!"),)),
            catalog_templates=_CATALOG.templates,
            tool_ids=_TOOLS,
        )


def test_empty_tasks_refuse() -> None:
    with pytest.raises(PlanDeriveError, match="no tasks"):
        derive_task_specs(_intent(()), catalog_templates=_CATALOG.templates, tool_ids=_TOOLS)


def test_question_answer_form_is_preserved() -> None:
    from coscientist.agents.derive import derive_question_drafts

    intent = _intent(
        (), questions=tuple(QuestionIntent(text="q", final_type=ft) for ft in ("verdict", "artifact", "both"))
    )
    assert [q.final_type for q in derive_question_drafts(intent)] == ["verdict", "artifact", "both"]


def test_needs_carried_verbatim() -> None:
    on = Need(kind="need", needed=True, reason="required")
    plan = derive_plan(
        PlanIntent(
            tasks=(TaskIntent(goal="g", route="experiment", question_indices=(1,)),),
            questions=(QuestionIntent(text="q?", final_type="artifact"),),
            hypotheses=on,
            literature=_NEED_OFF,
        ),
        catalog_templates=_CATALOG.templates,
        tool_ids=_TOOLS,
    )
    assert plan.hypotheses == on
    assert plan.literature == _NEED_OFF


@pytest.mark.asyncio
async def test_derived_plan_passes_accept_plan_unchanged(tmp_path) -> None:
    plan = derive_plan(
        _intent(
            (
                TaskIntent(
                    goal="g1",
                    route="experiment",
                    tools_hint=("static:dataset_overview",),
                    key="EXP-0",
                    title="overview",
                ),
                TaskIntent(
                    goal="g2",
                    route="experiment",
                    tools_hint=("static:chemical_space_clustering",),
                    key="EXP-1",
                    depends_on=("EXP-0",),
                ),
            )
        ),
        catalog_templates=_CATALOG.templates,
        tool_ids=_TOOLS,
    )
    rt = await open_runtime(tmp_path, "derive-accept")
    try:
        await rt.create_study(
            profile="t",
            request="q",
            catalog_version=_CATALOG.catalog_version,
            require_hypotheses=False,
            confirmations_needed=1,
            refutations_needed=1,
            refutation_priority=False,
            supported_needed=2,
            max_expansions=2,
            max_attempts=3,
            inputs=[],
        )
        plan_id = await rt.accept_plan(
            [_to_task_spec(t) for t in plan.tasks],
            questions=[QuestionSpec(id=q.id, text=q.text, final_type=q.final_type) for q in plan.questions],
            hypotheses=plan.hypotheses,
            literature=plan.literature,
            subquestions=[],
            expand_after=[],
            loops=[],
            spec_output=None,
        )
        assert plan_id  # all existing accept gates are the backstop — unchanged
        assert isinstance(_to_task_spec(plan.tasks[0]), TaskSpec)
    finally:
        await close_runtime(rt)


def test_coder_hint_single_derives_output_named_coder() -> None:
    """tools_hint ('coder',) is a valid delivery marker: one output named 'coder'."""
    specs = derive_task_specs(
        _intent((TaskIntent(goal="run analysis", route="experiment", tools_hint=("coder",)),)),
        catalog_templates=_CATALOG.templates,
        tool_ids=("coder",),
    )
    (spec,) = specs
    (out,) = spec.expected_outputs
    assert out.name == "coder"
    assert out.required is True
    assert out.rubric is not None
    assert spec.tools_hint == ("coder",)


def test_coder_hint_mixed_with_tools_refuses() -> None:
    """Code-execution delivery is exclusive — mixed hints are a typed refuse."""
    with pytest.raises(PlanDeriveError, match="exclusive"):
        derive_task_specs(
            _intent(
                (
                    TaskIntent(
                        goal="mixed",
                        route="experiment",
                        tools_hint=("coder", "static:dataset_overview"),
                    ),
                )
            ),
            catalog_templates=_CATALOG.templates,
            tool_ids=("coder",) + _TOOLS,
        )


def test_coder_hint_unknown_surface_refuses() -> None:
    """'coder' off the study surface is the normal unknown-tool refuse."""
    with pytest.raises(PlanDeriveError, match="not in the study tool surface"):
        derive_task_specs(
            _intent((TaskIntent(goal="g", route="experiment", tools_hint=("coder",)),)),
            catalog_templates=_CATALOG.templates,
            tool_ids=_TOOLS,
        )


def test_intent_question_indices_outside_declared_refuse_at_parse() -> None:
    """Out-of-range references fail schema validation (targeted LLM repair),
    never reach derive — 1-based positions into THIS intent's questions."""
    with pytest.raises(ValidationError, match="outside declared questions 1..2"):
        PlanIntent(
            tasks=(
                TaskIntent(goal="g", route="experiment", question_indices=(1,)),
                TaskIntent(goal="h", route="experiment", question_indices=(3,)),
            ),
            questions=tuple(QuestionIntent(text=f"q{i}", final_type="artifact") for i in (1, 2)),
            hypotheses=_NEED_OFF,
            literature=_NEED_OFF,
        )


def test_intent_question_indices_zero_refuses_at_field_bound() -> None:
    """0-based indices refuse at the field bound (ge=1), which also publishes
    minimum: 1 in the response_format JSON schema — schema guidance, not
    just a post-hoc message."""
    with pytest.raises(ValidationError, match="greater_than_equal"):
        PlanIntent(
            tasks=(TaskIntent(goal="g", route="experiment", question_indices=(0, -1)),),
            questions=(QuestionIntent(text="q", final_type="verdict"),),
            hypotheses=_NEED_OFF,
            literature=_NEED_OFF,
        )
    schema = PlanIntent.model_json_schema()["$defs"]["TaskIntent"]["properties"]["question_indices"]
    assert schema["items"]["minimum"] == 1


def test_intent_question_indices_empty_questions_any_reference_refuses() -> None:
    """A task may not reference questions the intent never declared."""
    with pytest.raises(ValidationError, match="outside declared questions 1..0"):
        PlanIntent(
            tasks=(TaskIntent(goal="g", route="experiment", question_indices=(1,)),),
            questions=(),
            hypotheses=_NEED_OFF,
            literature=_NEED_OFF,
        )


def test_pinned_criterion_requires_declared_output_at_parse() -> None:
    """Catalogue templates whose check pins an output refuse at schema parse when
    the task does not declare that output (derive enforces the same contract on
    the derived plan; parse-level failure feeds the targeted repair round)."""
    model = plan_intent_model_for(
        ("static:dataset_overview", "static:ping"),
        ("artifact_present",),
        (("artifact_present", "static:ping"),),
    )
    base = dict(
        questions=(QuestionIntent(text="q", final_type="artifact"),),
        hypotheses=_NEED_OFF,
        literature=_NEED_OFF,
    )
    with pytest.raises(ValidationError, match="criterion 'artifact_present' requires output 'static:ping'"):
        model(
            tasks=(
                dict(
                    goal="g",
                    route="experiment",
                    tools_hint=("static:dataset_overview",),
                    criteria=("artifact_present",),
                ),
            ),
            **base,
        )
    by_hint = model(
        tasks=(dict(goal="g", route="experiment", tools_hint=("static:ping",), criteria=("artifact_present",)),),
        **base,
    )
    assert by_hint.tasks[0].criteria == ("artifact_present",)
    by_name = plan_intent_model_for((), ("artifact_present",), (("artifact_present", "static:ping"),))(
        tasks=(
            dict(
                goal="g",
                route="experiment",
                output_names=("static:ping",),
                criteria=("artifact_present",),
            ),
        ),
        **base,
    )
    assert by_name.tasks[0].criteria == ("artifact_present",)


def test_unpinned_criteria_and_absent_pins_stay_valid() -> None:
    model = plan_intent_model_for(("static:ping",), ("artifact_present",))
    intent = model(
        tasks=(dict(goal="g", route="experiment", tools_hint=("static:ping",), criteria=("artifact_present",)),),
        questions=(QuestionIntent(text="q", final_type="artifact"),),
        hypotheses=_NEED_OFF,
        literature=_NEED_OFF,
    )
    assert intent.tasks[0].criteria == ("artifact_present",)
