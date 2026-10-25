"""Derivation: PlanIntent → PlanOut (naming, auto-rubric, empty criteria, refusal)."""

from __future__ import annotations

import pytest

from coscientist.agents.derive import PlanDeriveError, derive_plan, derive_task_specs
from coscientist.agents.schemas import PlanIntent, QuestionIntent, TaskIntent
from coscientist.cli.run import _to_task_spec
from coscientist.core.execution.domain import Need, QuestionSpec, TaskSpec
from tests.checks.minimal_test_catalog import minimal_test_catalog
from tests.helpers import close_runtime, open_runtime

_NEED_OFF = Need(kind="need", needed=False, reason="off")

_TOOLS = ("static:dataset_overview", "static:chemical_space_clustering")

_CATALOG = minimal_test_catalog()
_DEFAULT_QUESTIONS = (QuestionIntent(text="two tasks?"),)


def _intent(
    tasks: tuple[TaskIntent, ...],
    questions: tuple[QuestionIntent, ...] = _DEFAULT_QUESTIONS,
) -> PlanIntent:
    return PlanIntent(
        tasks=tasks,
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
                TaskIntent(
                    goal="g1", route="experiment", tools_hint=("static:dataset_overview",), key="prep"
                ),
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


def test_addresses_every_question_system_ids() -> None:
    plan = derive_plan(
        _intent(
            (TaskIntent(goal="g", route="experiment", tools_hint=("static:dataset_overview",)),),
            questions=(
                QuestionIntent(text="a?"),
                QuestionIntent(text="b?"),
            ),
        ),
        catalog_templates=_CATALOG.templates,
        tool_ids=_TOOLS,
    )
    assert [q.id for q in plan.questions] == ["q1", "q2"]
    assert [q.text for q in plan.questions] == ["a?", "b?"]
    assert all(s.addresses == ("q1", "q2") for s in plan.tasks)


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


def test_final_type_is_always_artifact_regardless_of_catalog() -> None:
    """Intent carries question text only; verdict/both is not expressible (PI1a).

    A live GLM run picked final_type='both' naturally, so the field was removed
    from the schema entirely — the LLM cannot emit what the system cannot
    deliver from typed intent. Measured-criterion linkage returns as a separate
    typed slice.
    """
    for templates in ([], list(_CATALOG.templates)):
        plan = derive_plan(
            _intent((TaskIntent(goal="g", route="experiment", tools_hint=("static:dataset_overview",)),)),
            catalog_templates=templates,
            tool_ids=_TOOLS,
        )
        assert [q.final_type for q in plan.questions] == ["artifact"]


def test_needs_carried_verbatim() -> None:
    on = Need(kind="need", needed=True, reason="required")
    plan = derive_plan(
        PlanIntent(
            tasks=(TaskIntent(goal="g", route="experiment"),),
            questions=(QuestionIntent(text="q?"),),
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
