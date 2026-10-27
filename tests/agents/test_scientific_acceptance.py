"""Content acceptance must survive derivation and question rollup."""

from datetime import timedelta

import pytest
from pydantic import ValidationError

from coscientist.agents.derive import PlanDeriveError, derive_plan
from coscientist.agents.schemas import PlanIntent, QuestionIntent, TaskIntent, plan_intent_model_for
from coscientist.checks.evaluate import evaluate_code_check, evaluate_task_checks
from coscientist.core.claims.rubric import freeze_rubric, task_rubric_status
from coscientist.core.claims.rubric_types import Rubric, RubricItem
from coscientist.core.execution.artifacts import LocalArtifactStore, make_tool_ref
from coscientist.core.execution.domain import (
    AgentTaskOutput,
    ArtifactHandle,
    CodeCheck,
    Need,
    OutputSpec,
    RequirementRef,
    Task,
    TaskKind,
    TaskOutcome,
    TaskSpec,
    TaskState,
)
from tests.checks.minimal_test_catalog import minimal_test_catalog


def _intent(*, criteria=(), indices=(1,), final_type="verdict"):
    return PlanIntent(
        tasks=(
            TaskIntent(
                goal="Measure dissolved oxygen",
                route="experiment",
                tools_hint=("coder",),
                question_indices=indices,
                criteria=criteria,
            ),
        ),
        questions=(QuestionIntent(text="What oxygen level was observed?", final_type=final_type),),
        hypotheses=Need(kind="need", needed=False, reason="direct measurement"),
        literature=Need(kind="need", needed=False, reason="direct measurement"),
    )


def _content_template():
    base = minimal_test_catalog().templates[0]
    return base.model_copy(
        update={
            "id": "oxygen_present",
            "check": CodeCheck(
                kind="json_pointer",
                output="coder",
                pointer="/oxygen_mg_l",
                allow_network=False,
                allow_cgroup_degraded=False,
            ),
        }
    )


def test_scientific_criteria_and_bindings_survive_derivation():
    plan = derive_plan(
        _intent(criteria=("oxygen_present",)), catalog_templates=[_content_template()], tool_ids=["coder"]
    )
    assert plan.tasks[0].criteria == ("oxygen_present",)
    assert plan.tasks[0].addresses == ("q1",)
    assert plan.questions[0].final_type == "verdict"


def test_criterion_catalogue_is_exposed_in_the_model_schema():
    schema = plan_intent_model_for(("coder",), ("oxygen_present", "measurement_valid")).model_json_schema()
    task_schema = schema["$defs"]["PlanIntentTaskTools"]
    assert task_schema["properties"]["criteria"]["items"]["enum"] == ["oxygen_present", "measurement_valid"]
    assert "question_indices" in task_schema["properties"]


def test_criterion_cannot_validate_an_unrelated_output():
    template = _content_template()
    template = template.model_copy(update={"check": template.check.model_copy(update={"output": "unrelated"})})
    with pytest.raises(PlanDeriveError, match="undeclared output"):
        derive_plan(_intent(criteria=(template.id,)), catalog_templates=[template], tool_ids=["coder"])


@pytest.mark.parametrize("kind", ["artifact_exists", "schema_valid", "source_link"])
def test_presence_or_unimplemented_content_checks_cannot_accept_science(kind):
    template = _content_template().model_copy(
        update={
            "check": CodeCheck(
                kind=kind,
                output="coder",
                allow_network=False,
                allow_cgroup_degraded=False,
            )
        }
    )
    with pytest.raises(PlanDeriveError, match="substantive"):
        derive_plan(_intent(criteria=(template.id,)), catalog_templates=[template], tool_ids=["coder"])


@pytest.mark.parametrize("indices", [(), (1, 1)])
def test_missing_or_duplicate_question_binding_refuses_at_derive(indices):
    with pytest.raises(PlanDeriveError):
        derive_plan(
            _intent(criteria=("oxygen_present",), indices=indices),
            catalog_templates=[_content_template()],
            tool_ids=["coder"],
        )


@pytest.mark.parametrize("indices", [(0,), (2,)])
def test_out_of_range_question_index_refuses_at_schema(indices):
    """Range-invalid references never reach derive: PlanIntent validation fails
    at parse, so the adapter repair round gets the exact offsets instead of a
    full derive→critic→replan cycle ending in the same refusal."""
    with pytest.raises(ValidationError):
        _intent(criteria=("oxygen_present",), indices=indices)


def test_explicit_file_request_is_not_forced_to_scientific_verdict():
    plan = derive_plan(_intent(final_type="artifact"), catalog_templates=[], tool_ids=["coder"])
    assert plan.questions[0].final_type == "artifact"


# ---------------------------------------------------------------------------
# Per-question coverage (legacy semantics port, docs/plan/
# acceptance-question-coverage.md): the substantive bar lives on the QUESTION,
# not on every addressing task. Co-addressing tasks deliver evidence through
# their auto-registered output rubrics.
# ---------------------------------------------------------------------------

_LIT_TOOL = "papers-search:search_papers"


def _lit_plus_computation_intent(
    *,
    final_type="verdict",
    lit_criteria=(),
    computation_criteria=("oxygen_present",),
    questions=None,
):
    """Attempt-15 shape: a literature/evidence task and a computation task
    co-address the same question."""
    return PlanIntent(
        tasks=(
            TaskIntent(
                goal="Ground the question in literature",
                route="experiment",
                tools_hint=(_LIT_TOOL,),
                question_indices=(1,),
                criteria=lit_criteria,
            ),
            TaskIntent(
                goal="Measure dissolved oxygen",
                route="experiment",
                tools_hint=("coder",),
                question_indices=(1,),
                criteria=computation_criteria,
            ),
        ),
        questions=questions or (QuestionIntent(text="What oxygen level was observed?", final_type=final_type),),
        hypotheses=Need(kind="need", needed=False, reason="direct measurement"),
        literature=Need(kind="need", needed=False, reason="direct measurement"),
    )


@pytest.mark.parametrize("final_type", ["verdict", "both"])
def test_verdict_question_covered_by_one_task_survives_evidence_only_coaddressing(final_type):
    """Attempt-15 wall (legacy port): a literature task may co-address a
    verdict/both question with evidence only (no criteria) while the
    computation task carries the substantive catalogue criterion."""
    plan = derive_plan(
        _lit_plus_computation_intent(final_type=final_type),
        catalog_templates=[_content_template()],
        tool_ids=["coder", _LIT_TOOL],
    )
    assert [list(t.criteria) for t in plan.tasks] == [[], ["oxygen_present"]]
    assert plan.tasks[0].addresses == plan.tasks[1].addresses == ("q1",)


def test_verdict_question_without_any_substantive_task_refuses_naming_the_question():
    """No gate weakening: a verdict question none of whose addressing tasks
    cites a substantive criterion still refuses at derive, and the refusal
    names the uncovered question (per-question message, not per-task)."""
    with pytest.raises(PlanDeriveError, match=r"question 'q1' .*not covered"):
        derive_plan(
            _lit_plus_computation_intent(computation_criteria=()),
            catalog_templates=[_content_template()],
            tool_ids=["coder", _LIT_TOOL],
        )


def test_verdict_question_refusal_names_the_uncovered_question_only():
    """Coverage is checked per question: q1 is covered by the computation
    task, q2 is addressed by evidence tasks only — the refusal names q2."""
    intent = _lit_plus_computation_intent(
        questions=(
            QuestionIntent(text="What oxygen level was observed?", final_type="verdict"),
            QuestionIntent(text="Is the synthesis affordable?", final_type="verdict"),
        ),
    )
    intent = intent.model_copy(
        update={
            "tasks": intent.tasks
            + (
                TaskIntent(
                    goal="Cost literature scan",
                    route="experiment",
                    tools_hint=(_LIT_TOOL,),
                    question_indices=(2,),
                ),
            )
        }
    )
    with pytest.raises(PlanDeriveError, match=r"question 'q2'") as excinfo:
        derive_plan(intent, catalog_templates=[_content_template()], tool_ids=["coder", _LIT_TOOL])
    assert "question 'q1'" not in str(excinfo.value)


def test_evidence_only_coaddressing_cannot_substitute_criterion_with_route_strings():
    """A non-substantive catalogue citation (artifact-present class) does not
    cover a verdict question — route/delivery wording cannot pass the gate."""
    template = _content_template().model_copy(
        update={
            "id": "presence_only",
            "check": CodeCheck(
                kind="artifact_exists",
                output="coder",
                allow_network=False,
                allow_cgroup_degraded=False,
            ),
        }
    )
    with pytest.raises(PlanDeriveError, match=r"question 'q1' .*not covered"):
        derive_plan(
            _lit_plus_computation_intent(computation_criteria=("presence_only",)),
            catalog_templates=[template],
            tool_ids=["coder", _LIT_TOOL],
        )


def _task(clock, *, second_required=True, state=TaskState.DONE, outcome=TaskOutcome.SUCCESS):
    rub = freeze_rubric(
        Rubric(items=[RubricItem(id="present", text="file exists", check="code", code_kind="artifact_exists")])
    )
    other = freeze_rubric(
        Rubric(items=[RubricItem(id="other", text="second file exists", check="code", code_kind="artifact_exists")])
    )
    task = Task(
        task_id="s/measure",
        kind=TaskKind.EXPERIMENT,
        spec=TaskSpec(
            task_key="measure",
            title="measurement",
            goal="measure",
            route="experiment",
            addresses=["q1"],
            criteria=[RequirementRef(template_id="oxygen_present")],
            expected_outputs=[
                OutputSpec(name="main", media_type="application/json", rubric=rub),
                OutputSpec(name="other", media_type="application/json", required=second_required, rubric=other),
            ],
        ),
        state=state,
        outcome=outcome,
        deadline=clock.now() + timedelta(hours=1),
        attempts=["old", "current"],
    )
    checks = [
        {"template_id": f"rubric:{r.digest}", "status": "MET", "effect": {"rubric_digest": r.digest}}
        for r in (rub, other)
    ]
    checks.append({"template_id": "oxygen_present", "status": "MET", "role": "validity", "required": True})
    return task, checks


def _status(task, checks):
    return task_rubric_status(
        task_keys={"measure"},
        tasks={"s/measure": task},
        results={"old": {"check_results": [{"status": "FAILED"}]}, "current": {"check_results": checks}},
    )


@pytest.mark.parametrize("raw", [b"{}", b'{"unrelated": 12}', b""])
def test_empty_content_cannot_be_answered_by_met_artifact_rubric(clock, tmp_path, raw):
    task, checks = _task(clock)
    artifacts = LocalArtifactStore(tmp_path)
    art = artifacts.put(raw, make_tool_ref("current:coder"), name="coder", media_type="application/json")
    check_result = evaluate_code_check(
        _content_template().check,
        template_id="oxygen_present",
        check_id="oxygen_present#0",
        role="validity",
        artifacts=artifacts,
        values={},
        attempt_id="current",
        out=AgentTaskOutput(
            summary="measurement", outputs={"coder": ArtifactHandle(artifact_id=art.artifact_id, name="coder")}
        ),
    )
    checks[-1] = check_result.model_dump(mode="json")
    assert _status(task, checks) == "failed"


def test_all_required_outputs_need_acceptance(clock):
    task, checks = _task(clock)
    checks[1]["status"] = "FAILED"
    assert _status(task, checks) == "failed"


def test_missing_required_content_check_is_pending(clock):
    task, checks = _task(clock)
    assert _status(task, checks[:-1]) == "pending"


def test_optional_output_failure_does_not_taint_required_answer(clock):
    task, checks = _task(clock, second_required=False)
    checks[1]["status"] = "FAILED"
    assert _status(task, checks) == "met"


def test_skipped_required_task_cannot_answer(clock):
    task, checks = _task(clock, outcome=TaskOutcome.SKIPPED)
    assert _status(task, checks) == "pending"


def test_successful_current_attempt_supersedes_old_failure(clock):
    task, checks = _task(clock)
    assert _status(task, checks) == "met"


def test_prediction_refutation_does_not_become_validity_failure(clock):
    task, checks = _task(clock)
    checks.append({"template_id": "prediction", "status": "FAILED", "role": "prediction", "required": True})
    assert _status(task, checks) == "met"


def test_optional_criterion_failure_does_not_taint_answer(clock):
    task, checks = _task(clock)
    checks.append({"template_id": "optional", "status": "FAILED", "role": "validity", "required": False})
    assert _status(task, checks) == "met"


def test_unrelated_successful_task_cannot_answer_question(clock):
    task, checks = _task(clock)
    unrelated = task.model_copy(update={"spec": task.spec.model_copy(update={"task_key": "unrelated"})})
    assert (
        task_rubric_status(
            task_keys={"measure"}, tasks={"s/unrelated": unrelated}, results={"current": {"check_results": checks}}
        )
        == "pending"
    )


def test_optional_failed_task_does_not_taint_required_answer(clock):
    task, checks = _task(clock)
    optional = task.model_copy(
        update={
            "task_id": "s/optional",
            "outcome": TaskOutcome.FAILED,
            "spec": task.spec.model_copy(update={"task_key": "optional", "required": False}),
        }
    )
    assert (
        task_rubric_status(
            task_keys={"measure", "optional"},
            tasks={"s/measure": task, "s/optional": optional},
            results={"current": {"check_results": checks}},
        )
        == "met"
    )


def test_identical_rubric_cannot_cover_two_outputs_with_one_record(clock):
    task, checks = _task(clock)
    first, second = task.spec.expected_outputs
    spec = task.spec.model_copy(
        update={"expected_outputs": [first, second.model_copy(update={"rubric": first.rubric})]}
    )
    task = task.model_copy(update={"spec": spec})
    checks[0]["effect"]["output"] = "main"
    assert _status(task, [checks[0], checks[-1]]) == "pending"
    del checks[0]["effect"]["output"]
    assert _status(task, [checks[0], checks[-1]]) == "pending"


def test_catalogue_rubric_does_not_skip_identical_rubric_on_other_output(clock, tmp_path):
    task, _ = _task(clock)
    first, second = task.spec.expected_outputs
    second = second.model_copy(update={"rubric": first.rubric})
    spec = task.spec.model_copy(
        update={"expected_outputs": [first, second], "criteria": [RequirementRef(template_id="explicit_main")]}
    )
    template = _content_template().model_copy(
        update={
            "id": "explicit_main",
            "check": CodeCheck(
                kind="rubric",
                output="main",
                rubric=first.rubric,
                allow_network=False,
                allow_cgroup_degraded=False,
            ),
        }
    )
    catalog = minimal_test_catalog().model_copy(update={"templates": [template], "defaults": {}})
    artifacts = LocalArtifactStore(tmp_path)
    art = artifacts.put(b"{}", make_tool_ref("current:main"), name="main", media_type="application/json")
    checks = evaluate_task_checks(
        catalog,
        spec,
        AgentTaskOutput(summary="one file", outputs={"main": ArtifactHandle(artifact_id=art.artifact_id, name="main")}),
        artifacts,
        task_id="s/measure",
        attempt_id="current",
        output_digest="measurement",
    )
    assert {c.effect["output"]: c.status.value for c in checks} == {"main": "MET", "other": "FAILED"}
    assert _status(task.model_copy(update={"spec": spec}), [c.model_dump(mode="json") for c in checks]) == "failed"
