"""Universal acceptance S1: acceptance-route resolver (design §1/§2).

Three deterministic routes for verdict/both questions: ``catalog_verified``
(substantive catalogue criterion — the pre-S1 path), ``hypothesis_claimed``
(explicit falsifiable claim + hypotheses.needed; RC4/RC5/RC6 unchanged), and
the new ``evidence_grounded`` (explicit value/review claim closed on
registered receipts — never on bare existence, never for falsifiable
claims). An absent ``claim_class`` keeps the strict pre-S1 gate: the wall is
lifted only by an explicit typed claim, never by omission.
"""

from __future__ import annotations

import pytest

from coscientist.agents.derive import PlanDeriveError, derive_plan
from coscientist.agents.schemas import PlanIntent, QuestionIntent, TaskIntent, plan_intent_model_for
from coscientist.core.claims.rubric import freeze_rubric
from coscientist.core.claims.rubric_types import Rubric, RubricItem
from coscientist.core.execution.domain import (
    Need,
    OutputSpec,
    QuestionSpec,
    RequirementRef,
    TaskSpec,
)
from coscientist.core.plan.plan_validate import validate_final_types
from tests.agents.test_scientific_acceptance import _content_template
from tests.checks.minimal_test_catalog import minimal_test_catalog
from tests.helpers import close_runtime, open_runtime

_NEED_OFF = Need(kind="need", needed=False, reason="off")
_NEED_ON = Need(kind="need", needed=True, reason="required")


def _grounded_intent(
    *,
    claim_class: str | None = "value",
    final_type: str = "verdict",
    criteria: tuple[str, ...] = (),
    hypotheses: Need = _NEED_OFF,
    tools_hint: tuple[str, ...] = ("coder",),
) -> PlanIntent:
    return PlanIntent(
        tasks=(
            TaskIntent(
                goal="Deliver the answer",
                route="experiment",
                tools_hint=tools_hint,
                question_indices=(1,),
                criteria=criteria,
            ),
        ),
        questions=(
            QuestionIntent(
                text="What synthesis cost was observed?",
                final_type=final_type,  # type: ignore[arg-type]
                claim_class=claim_class,  # type: ignore[arg-type]
            ),
        ),
        hypotheses=hypotheses,
        literature=_NEED_OFF,
    )


# ---------------------------------------------------------------------------
# (a) The §2 confirming test: empty satisfiable set + coder ⇒ plan accepted,
# question routed grounded. The owner's law: an empty catalogue is never a
# reason to refuse when an execution surface (coder) exists.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_empty_catalog_value_question_routes_grounded_and_accepts(tmp_path) -> None:
    plan = derive_plan(_grounded_intent(), catalog_templates=[], tool_ids=("coder",))
    assert plan.questions[0].acceptance_route == "evidence_grounded"
    assert plan.questions[0].claim_class == "value"
    assert plan.tasks[0].criteria == ()

    from coscientist.cli.run import _to_task_spec

    rt = await open_runtime(tmp_path, "universal-acceptance")
    try:
        await rt.create_study(
            profile="t",
            request="q",
            catalog_version=minimal_test_catalog().catalog_version,
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
            questions=[
                QuestionSpec(
                    id=q.id,
                    text=q.text,
                    final_type=q.final_type,
                    claim_class=q.claim_class,
                    acceptance_route=q.acceptance_route,
                )
                for q in plan.questions
            ],
            hypotheses=plan.hypotheses,
            literature=plan.literature,
            subquestions=[],
            expand_after=[],
            loops=[],
            spec_output=None,
        )
        assert plan_id  # validate_final_types accepts the recorded grounded route
    finally:
        await close_runtime(rt)


@pytest.mark.parametrize("claim_class", ["value", "review"])
def test_review_and_value_classes_both_route_grounded(claim_class) -> None:
    plan = derive_plan(_grounded_intent(claim_class=claim_class), catalog_templates=[], tool_ids=("coder",))
    assert plan.questions[0].acceptance_route == "evidence_grounded"


# ---------------------------------------------------------------------------
# (b) Honest typed refuse: only falsifiable-without-engines is refused under
# the new routes; existence never substitutes a verdict.
# ---------------------------------------------------------------------------


def test_falsifiable_without_any_verdict_engine_refuses() -> None:
    with pytest.raises(PlanDeriveError, match="claim_class=falsifiable requires a verdict engine"):
        derive_plan(_grounded_intent(claim_class="falsifiable"), catalog_templates=[], tool_ids=("coder",))


def test_falsifiable_with_hypotheses_routes_hypothesis_claimed() -> None:
    """The hypothesis contour is the verdict engine: derive routes the explicit
    falsifiable claim to hypothesis_claimed. HYP-n presence stays RC4's owner
    (unchanged gate at accept_plan) — derive deliberately does not duplicate it."""
    plan = derive_plan(
        _grounded_intent(claim_class="falsifiable", hypotheses=_NEED_ON),
        catalog_templates=[],
        tool_ids=("coder",),
    )
    assert plan.questions[0].acceptance_route == "hypothesis_claimed"


def test_falsifiable_with_catalogue_criterion_routes_catalog_verified() -> None:
    plan = derive_plan(
        _grounded_intent(claim_class="falsifiable", criteria=("oxygen_present",)),
        catalog_templates=[_content_template()],
        tool_ids=("coder",),
    )
    assert plan.questions[0].acceptance_route == "catalog_verified"


def test_grounded_route_needs_an_explicit_typed_claim() -> None:
    """Honesty (design §5 FP «обход маршрута»): evidence_grounded is reachable
    only through an explicit value/review claim_class — never for falsifiable,
    never by omission."""
    with pytest.raises(PlanDeriveError, match="claim_class=falsifiable"):
        derive_plan(_grounded_intent(claim_class="falsifiable"), catalog_templates=[], tool_ids=("coder",))
    with pytest.raises(PlanDeriveError, match=r"question 'q1' .*not covered"):
        derive_plan(_grounded_intent(claim_class=None), catalog_templates=[], tool_ids=("coder",))


# ---------------------------------------------------------------------------
# Honesty rails on evidence_grounded (design §8.i): grounded closure binds
# receipt CONTENT, never bare existence.
# ---------------------------------------------------------------------------


def test_grounded_question_must_cite_available_content_check() -> None:
    """A content-bearing catalogue check citable by the addressing tasks must
    be cited — the planner may not skip an available receipt-content check and
    close the question on auto existence rubrics."""
    with pytest.raises(PlanDeriveError, match="content-bearing catalogue check is available"):
        derive_plan(
            _grounded_intent(criteria=()),
            catalog_templates=[_content_template()],
            tool_ids=("coder",),
        )


def test_grounded_question_citing_the_content_check_routes_catalog_verified() -> None:
    """When the catalogue supplies the criterion, the route is catalog_verified
    (the operator catalog supplied the threshold), not grounded."""
    plan = derive_plan(
        _grounded_intent(criteria=("oxygen_present",)),
        catalog_templates=[_content_template()],
        tool_ids=("coder",),
    )
    assert plan.questions[0].acceptance_route == "catalog_verified"


def test_grounded_template_pinned_elsewhere_is_not_citable() -> None:
    """A content template pinned to an output no addressing task declares is
    not satisfiable for the question and never forces a citation (the wall is
    not resurrected through pins)."""
    elsewhere = _content_template().model_copy(
        update={"check": _content_template().check.model_copy(update={"output": "other-tool"})}
    )
    plan = derive_plan(
        _grounded_intent(),
        catalog_templates=[elsewhere],
        tool_ids=("coder", "other-tool"),
    )
    assert plan.questions[0].acceptance_route == "evidence_grounded"


def test_grounded_question_without_any_declared_output_refuses() -> None:
    """With an empty catalogue, grounded closure still needs a receipt surface:
    an addressing task that declares no output cannot ground anything."""
    with pytest.raises(PlanDeriveError, match="declares any output"):
        derive_plan(
            _grounded_intent(tools_hint=()),
            catalog_templates=[],
            tool_ids=("coder",),
        )


# ---------------------------------------------------------------------------
# Pre-S1 semantics preserved: the legacy refusal message (claim_class absent)
# and the planner-schema enum guidance.
# ---------------------------------------------------------------------------


def test_legacy_absent_claim_class_refusal_message_is_preserved() -> None:
    with pytest.raises(PlanDeriveError, match=r"question 'q1' .*not covered: addressing tasks .* cite "):
        derive_plan(
            _grounded_intent(claim_class=None),
            catalog_templates=[_content_template()],
            tool_ids=("coder",),
        )


def test_planner_schema_publishes_claim_class_enum() -> None:
    """The claim-class lever is schema guidance: the response_format JSON schema
    carries the enum, not a prompt sentence (same pattern as tools_hint)."""
    schema = plan_intent_model_for(("coder",), ()).model_json_schema()
    claim_class = schema["$defs"]["QuestionIntent"]["properties"]["claim_class"]
    enums = [opt.get("enum") for opt in claim_class.get("anyOf", [claim_class])]
    assert ["value", "review", "falsifiable"] in enums


# ---------------------------------------------------------------------------
# plan_validate backstop: validate_final_types accepts a recorded route and
# keeps the strict criterion rule for legacy questions.
# ---------------------------------------------------------------------------


def _spec_with_route(route: str | None) -> tuple[list[TaskSpec], list[QuestionSpec]]:
    rubric = freeze_rubric(
        Rubric(items=[RubricItem(id="exists", text="artifact exists", check="code", code_kind="artifact_exists")])
    )
    spec = TaskSpec(
        task_key="t1",
        title="t1",
        goal="deliver",
        route="experiment",
        addresses=["q1"],
        criteria=[],
        expected_outputs=[OutputSpec(name="coder", media_type="application/json", rubric=rubric)],
    )
    question = QuestionSpec(
        id="q1",
        text="What cost?",
        final_type="verdict",
        claim_class="value",
        acceptance_route=route,  # type: ignore[arg-type]
    )
    return [spec], [question]


def test_validate_final_types_accepts_recorded_grounded_route() -> None:
    tasks, questions = _spec_with_route("evidence_grounded")
    validate_final_types(tasks, questions)


def test_validate_final_types_accepts_recorded_hypothesis_route() -> None:
    tasks, questions = _spec_with_route("hypothesis_claimed")
    validate_final_types(tasks, questions)


def test_validate_final_types_legacy_question_still_needs_criterion() -> None:
    """No silent route: a legacy question (no acceptance_route) without a
    criterion refuses exactly as before the S1 resolver existed."""
    tasks, questions = _spec_with_route(None)
    with pytest.raises(ValueError, match="need ≥1 criterion"):
        validate_final_types(tasks, questions)


def test_validate_final_types_catalog_route_without_criterion_refuses() -> None:
    """A recorded catalog_verified route claims a criterion — the claim must
    hold; the route string alone never satisfies the backstop."""
    tasks, questions = _spec_with_route("catalog_verified")
    with pytest.raises(ValueError, match="need ≥1 criterion"):
        validate_final_types(tasks, questions)


def test_catalog_route_tasks_keep_typed_requirement_refs() -> None:
    """Catalog route tasks keep typed RequirementRef criteria — the resolver
    never rewrites criteria into free-form text."""
    from coscientist.cli.run import _to_task_spec

    plan = derive_plan(
        _grounded_intent(criteria=("oxygen_present",)),
        catalog_templates=[_content_template()],
        tool_ids=("coder",),
    )
    spec = _to_task_spec(plan.tasks[0])
    assert isinstance(spec.criteria[0], RequirementRef)
    assert spec.criteria[0].template_id == "oxygen_present"
