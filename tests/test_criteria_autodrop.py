"""Accept-time drop of mis-cited criteria (system guarantee layer).

Live walls st_idem_32485 / st_idem_0d991: the planner kept citing catalogue
criteria on tasks whose declared outputs do not satisfy the citation rule —
catalogue pins are tool-qualified (``heracleum-tox:predict_ld50``, ``coder``)
while planner tasks declare bare output names. One parse-time repair round +
the #313 map did not converge GLM. The citation rule's PURPOSE is that a cited
criterion be verifiable against the task's actual receipt; a MIS-cited
criterion is planner noise. So accept_plan DROPS the uncitable citation (same
``criterion_citable`` predicate the parse-time validator refuses with — two
layers, one rule), journals the drop, and lets the task run with its receipts.
The per-question coverage gate stays the honest backstop: a question that
loses its last substantive citation refuses exactly as before.

The minimal test catalog pins ``main`` on its code checks (``value_match``),
so a task declaring ``dataset_overview`` mis-cites exactly like the live runs.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from coscientist.core.execution.domain import (
    Catalog,
    Need,
    OutputSpec,
    QuestionSpec,
    RequirementRef,
    TaskSpec,
)
from coscientist.core.execution.inputs import (
    criterion_citable,
    pinned_criterion_output,
)
from coscientist.core.plan.lifecycle import (
    criteria_autodrop_note,
    drop_uncitable_task_criteria,
)
from tests.checks.minimal_test_catalog import minimal_test_catalog
from tests.helpers import NEED_OFF, close_runtime, open_runtime

_NEED_ON = Need(kind="need", needed=True, reason="hypotheses required")

_Q1 = QuestionSpec(id="Q1", text="Каково значение?", final_type="verdict")


def _task(key: str, *, outputs: list[str], criteria: list[str], addresses: list[str]) -> TaskSpec:
    return TaskSpec(
        task_key=key,
        title=key,
        goal=key,
        route="experiment",
        expected_outputs=[OutputSpec(name=n, media_type="application/json") for n in outputs],
        criteria=[RequirementRef(template_id=c) for c in criteria],
        addresses=addresses,
    )


async def _open(tmp_path: Path, name: str, *, require_hypotheses: bool = False):
    rt = await open_runtime(tmp_path, name)
    await rt.create_study(
        profile="t",
        request="q",
        catalog_version=4,
        require_hypotheses=require_hypotheses,
        confirmations_needed=1,
        refutations_needed=1,
        refutation_priority=False,
        supported_needed=2,
        max_expansions=2,
        max_attempts=3,
        inputs=[],
    )
    return rt


def test_criterion_citable_is_the_shared_rule() -> None:
    """One predicate: unpinned citable anywhere; pinned only when declared."""
    assert criterion_citable(None, frozenset())
    assert criterion_citable("main", frozenset({"main"}))
    assert not criterion_citable("main", frozenset({"dataset_overview"}))
    # The parse-time teacher reads the same pin off the template.
    cat: Catalog = minimal_test_catalog()
    by_id = {t.id: t for t in cat.templates}
    assert pinned_criterion_output(by_id["value_match"]) == "main"
    assert pinned_criterion_output(by_id["human_review"]) is None


def test_drop_uncitable_task_criteria_unit() -> None:
    """Drops only pinned-to-undeclared criteria; unknown ids stay (validate
    refuses them); unpinned criteria stay; clean tasks pass through untouched."""
    cat: Catalog = minimal_test_catalog()
    good = _task("EXP-1", outputs=["main"], criteria=["value_match", "human_review"], addresses=[])
    bad = _task("EXP-2", outputs=["dataset_overview"], criteria=["value_match", "human_review"], addresses=[])
    invented = _task("EXP-3", outputs=["main"], criteria=["no_such_template"], addresses=[])
    tasks, groups = drop_uncitable_task_criteria([good, bad, invented], cat)
    assert groups == ["EXP-2: ['value_match']"]
    by_key = {t.task_key: t for t in tasks}
    assert [r.template_id for r in by_key["EXP-1"].criteria] == ["value_match", "human_review"]
    # The kept citation is real: task identity preserved, criteria list rewritten.
    assert by_key["EXP-1"] is good
    assert [r.template_id for r in by_key["EXP-2"].criteria] == ["human_review"]
    assert [r.template_id for r in by_key["EXP-3"].criteria] == ["no_such_template"]


def test_drop_uncitable_task_criteria_noop_returns_same_list() -> None:
    cat: Catalog = minimal_test_catalog()
    tasks = [_task("EXP-1", outputs=["main"], criteria=["value_match"], addresses=[])]
    rewritten, groups = drop_uncitable_task_criteria(tasks, cat)
    assert groups == []
    assert rewritten == tasks


def test_criteria_autodrop_note_names_task_and_ids() -> None:
    note = criteria_autodrop_note(["EXP-2: ['value_match']"])
    assert "EXP-2" in note
    assert "['value_match']" in note
    assert "пин требует выход" in note
    assert "приёмками" in note


@pytest.mark.asyncio
async def test_accept_drops_miscited_criterion_keeps_correct_ones(tmp_path) -> None:
    """One mis-cited citation among correct ones: the bad criterion is dropped,
    the correct ones kept, the note names the task_key + dropped ids, and the
    study accepts (tasks go with their own receipts)."""
    rt = await _open(tmp_path, "autodrop-mixed")
    try:
        exp1 = _task("EXP-1", outputs=["main"], criteria=["value_match"], addresses=["Q1"])
        exp2 = _task("EXP-2", outputs=["dataset_overview"], criteria=["value_match"], addresses=["Q1"])
        pid = await rt.accept_plan(
            [exp1, exp2],
            questions=[_Q1],
            hypotheses=NEED_OFF,
            literature=NEED_OFF,
            subquestions=[],
            expand_after=[],
            loops=[],
            spec_output=None,
        )
        assert pid
        by_key = {t.spec.task_key: t for t in rt.tasks.values()}
        assert [r.template_id for r in by_key["EXP-1"].spec.criteria] == ["value_match"]
        assert by_key["EXP-2"].spec.criteria == []
        accepted = [e for e in await rt.store.read_all() if e.type == "PlanAccepted"]
        assert len(accepted) == 1
        note = accepted[0].payload["note"]
        assert isinstance(note, str)
        assert "EXP-2" in note
        assert "['value_match']" in note
        assert "пин требует выход" in note
        assert "приёмками" in note
        # The accepted plan payload carries the REDUCED criteria (drop before freeze).
        payload_tasks = {t["task_key"]: t for t in accepted[0].payload["tasks"]}
        assert [r["template_id"] for r in payload_tasks["EXP-2"]["criteria"]] == []
        assert [r["template_id"] for r in payload_tasks["EXP-1"]["criteria"]] == ["value_match"]
        assert not [e for e in await rt.store.read_all() if e.type == "PlanRefused"]
    finally:
        await close_runtime(rt)


@pytest.mark.asyncio
async def test_drop_leaving_verdict_question_without_substantive_citation_refuses(tmp_path) -> None:
    """Dropping must not silently uncover a verdict question: when the only
    citation on the only addressing task is the mis-cited one, the EXISTING
    coverage gate refuses (honest — never bypassed by the drop)."""
    rt = await _open(tmp_path, "autodrop-uncovered")
    try:
        exp1 = _task("EXP-1", outputs=["dataset_overview"], criteria=["value_match"], addresses=["Q1"])
        pid = await rt.accept_plan(
            [exp1],
            questions=[_Q1],
            hypotheses=NEED_OFF,
            literature=NEED_OFF,
            subquestions=[],
            expand_after=[],
            loops=[],
            spec_output=None,
        )
        assert pid == ""
        refused = [e for e in await rt.store.read_all() if e.type == "PlanRefused"]
        assert len(refused) == 1
        assert "Q1" in str(refused[0].payload["detail"])
        assert "≥1 criterion" in str(refused[0].payload["detail"])
        assert not [e for e in await rt.store.read_all() if e.type == "PlanAccepted"]
    finally:
        await close_runtime(rt)


@pytest.mark.asyncio
async def test_clean_plan_no_drop_no_note_parity(tmp_path) -> None:
    """Fully clean plan: no drops, no note key (v12 payload parity)."""
    rt = await _open(tmp_path, "autodrop-clean")
    try:
        exp1 = _task("EXP-1", outputs=["main"], criteria=["value_match"], addresses=["Q1"])
        pid = await rt.accept_plan(
            [exp1],
            questions=[_Q1],
            hypotheses=NEED_OFF,
            literature=NEED_OFF,
            subquestions=[],
            expand_after=[],
            loops=[],
            spec_output=None,
        )
        assert pid
        by_key = {t.spec.task_key: t for t in rt.tasks.values()}
        assert [r.template_id for r in by_key["EXP-1"].spec.criteria] == ["value_match"]
        accepted = [e for e in await rt.store.read_all() if e.type == "PlanAccepted"]
        assert len(accepted) == 1
        assert "note" not in accepted[0].payload
        assert not [e for e in await rt.store.read_all() if e.type == "PlanRefused"]
    finally:
        await close_runtime(rt)


@pytest.mark.asyncio
async def test_drop_composes_with_hyp_auto_insert_note(tmp_path) -> None:
    """#312/#314 insert + drop compose: both system actions journaled in the
    ONE v13 note (appended with a separator — schema stays v13)."""
    rt = await _open(tmp_path, "autodrop-compose", require_hypotheses=True)
    try:
        exp1 = _task("EXP-1", outputs=["dataset_overview"], criteria=["value_match"], addresses=[])
        pid = await rt.accept_plan(
            [exp1],
            questions=[],
            hypotheses=_NEED_ON,
            literature=NEED_OFF,
            subquestions=[],
            expand_after=[],
            loops=[],
            spec_output=None,
        )
        assert pid
        assert any(t.spec.task_key == "HYP-1" for t in rt.tasks.values())
        by_key = {t.spec.task_key: t for t in rt.tasks.values()}
        assert by_key["EXP-1"].spec.criteria == []
        accepted = [e for e in await rt.store.read_all() if e.type == "PlanAccepted"]
        assert len(accepted) == 1
        note = accepted[0].payload["note"]
        assert isinstance(note, str)
        # Both notes, both actions, one journal entry.
        assert "HYP-1" in note
        assert "инвариант стадии" in note
        assert "EXP-1" in note
        assert "['value_match']" in note
        assert "приёмками" in note
        assert " | " in note
        assert not [e for e in await rt.store.read_all() if e.type == "PlanRefused"]
    finally:
        await close_runtime(rt)


# ---------------------------------------------------------------------------
# Live path (delta): parse no longer refuses a mis-citation — derive DROPS the
# uncitable criteria, records them on PlanOut.uncitable_drops, and accept_plan
# journals them on the same system note. The accept-layer drop below stays the
# guarantee for direct callers.
# ---------------------------------------------------------------------------


def _live_intent(*, require_hyp: bool = False):
    """The wave-7 live shape: tool-qualified pins (value_match pins 'main')
    cited on tasks declaring bare output names. E1 cites a CITABLE criterion
    (declares 'main' — covers the verdict question), E2 mis-cites."""
    from coscientist.agents.schemas import PlanIntent, QuestionIntent, TaskIntent

    return PlanIntent(
        tasks=(
            TaskIntent(
                key="E1",
                goal="measure",
                route="experiment",
                question_indices=(1,),
                output_names=("main",),
                criteria=("value_match",),
            ),
            TaskIntent(
                key="E2",
                goal="summarize",
                route="experiment",
                question_indices=(1,),
                output_names=("dataset_overview",),
                criteria=("value_match",),
            ),
        ),
        questions=(QuestionIntent(text="Каково значение?", final_type="verdict"),),
        hypotheses=_NEED_ON if require_hyp else NEED_OFF,
        literature=NEED_OFF,
    )


@pytest.mark.asyncio
async def test_live_path_derive_drop_journaled_at_accept(tmp_path) -> None:
    """Mis-cited plan parses → derive drops (E2's citation recorded on
    PlanOut.uncitable_drops) → accept accepts and journals the drop on the
    readable note; E1's citable citation is untouched; coverage holds."""
    from coscientist.agents.derive import derive_plan
    from coscientist.cli.run import _to_task_spec

    plan = derive_plan(_live_intent(), catalog_templates=minimal_test_catalog().templates, tool_ids=["coder"])
    assert plan.uncitable_drops == (("E2", ("value_match",)),)
    assert [t.criteria for t in plan.tasks] == [("value_match",), ()]
    assert [q.acceptance_route for q in plan.questions] == ["catalog_verified"]

    rt = await _open(tmp_path, "autodrop-live")
    try:
        pid = await rt.accept_plan(
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
            derive_uncitable_drops=plan.uncitable_drops,
        )
        assert pid
        by_key = {t.spec.task_key: t for t in rt.tasks.values()}
        assert [r.template_id for r in by_key["E1"].spec.criteria] == ["value_match"]
        assert by_key["E2"].spec.criteria == []
        accepted = [e for e in await rt.store.read_all() if e.type == "PlanAccepted"]
        assert len(accepted) == 1
        note = accepted[0].payload["note"]
        assert isinstance(note, str)
        assert "Система убрала некорректные цитировки критериев (derive): E2: ['value_match']" in note
        assert "приёмками" in note
        # The plans projection row carries the same readable note.
        plan_rows = [r for r in await rt.store.get_projection("plans") if r.get("plan_id") == pid]
        assert plan_rows and plan_rows[0]["note"] == note
        assert not [e for e in await rt.store.read_all() if e.type == "PlanRefused"]
    finally:
        await close_runtime(rt)


@pytest.mark.asyncio
async def test_live_path_derive_drop_composes_with_hyp_auto_insert(tmp_path) -> None:
    """#314 insert + derive drop compose: both system actions journaled in the
    ONE v13 note, pipeline order (derive drop first, then the insert)."""
    from coscientist.agents.derive import derive_plan
    from coscientist.cli.run import _to_task_spec
    from coscientist.core.execution.domain import Need

    intent_model = _live_intent(require_hyp=True)
    plan = derive_plan(
        intent_model,
        catalog_templates=minimal_test_catalog().templates,
        tool_ids=["coder"],
    )
    assert plan.uncitable_drops == (("E2", ("value_match",)),)

    rt = await _open(tmp_path, "autodrop-live-compose", require_hypotheses=True)
    try:
        pid = await rt.accept_plan(
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
            hypotheses=Need(kind="need", needed=True, reason="hypotheses required"),
            literature=plan.literature,
            subquestions=[],
            expand_after=[],
            loops=[],
            spec_output=None,
            derive_uncitable_drops=plan.uncitable_drops,
        )
        assert pid
        assert any(t.spec.task_key == "HYP-1" for t in rt.tasks.values())
        accepted = [e for e in await rt.store.read_all() if e.type == "PlanAccepted"]
        assert len(accepted) == 1
        note = accepted[0].payload["note"]
        assert isinstance(note, str)
        assert "(derive): E2: ['value_match']" in note
        assert "HYP-1" in note
        assert "инвариант стадии" in note
        assert " | " in note
        assert not [e for e in await rt.store.read_all() if e.type == "PlanRefused"]
    finally:
        await close_runtime(rt)


@pytest.mark.asyncio
async def test_derive_drop_and_accept_drop_compose_in_one_note(tmp_path) -> None:
    """Reported derive drops and the accept-layer drop (direct-caller guard)
    journal in the same note — two segments, one PlanAccepted."""
    rt = await _open(tmp_path, "autodrop-two-layers")
    try:
        exp = _task("EXP-1", outputs=["dataset_overview"], criteria=["value_match"], addresses=[])
        pid = await rt.accept_plan(
            [exp],
            questions=[],
            hypotheses=NEED_OFF,
            literature=NEED_OFF,
            subquestions=[],
            expand_after=[],
            loops=[],
            spec_output=None,
            derive_uncitable_drops=(("t4", ("research_result_payload",)),),
        )
        assert pid
        accepted = [e for e in await rt.store.read_all() if e.type == "PlanAccepted"]
        note = accepted[0].payload["note"]
        assert isinstance(note, str)
        assert "(derive): t4: ['research_result_payload']" in note
        assert "EXP-1: ['value_match']" in note
        assert note.count("Система убрала некорректные цитировки критериев") == 2
        assert " | " in note
    finally:
        await close_runtime(rt)
