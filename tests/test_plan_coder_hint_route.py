"""Route-wins delivery: the reserved ``coder`` tools_hint survives only on
experiment-route tasks.

Live st_d705d826: the planner emitted a hypotheses-route verdicts task with
``tools_hint ['coder']`` — acceptance had no check for the combo, and the
driver's coder dispatch typed-refused mid-run (``code execution is
experiment delivery only``), killing an otherwise healthy study. The accept
path now strips the hint off stage routes (LIT / HYP / PLAN) the same way it
declares the hyp stage output: system guarantee, readable journal note,
never silent."""

from __future__ import annotations

from coscientist.core.execution.domain import CODER_HINT, Need, TaskSpec
from coscientist.core.plan.lifecycle import (
    coder_hint_drop_note,
    strip_coder_hint_off_stage_routes,
)
from tests.helpers import NEED_OFF, close_runtime, open_runtime

_NEED_ON = Need(kind="need", needed=True, reason="hypotheses required")

_HYP_CODER = TaskSpec(
    task_key="HYP-1",
    title="verdicts",
    goal="produce hypothesis verdicts",
    route="hypotheses",
    tools_hint=[CODER_HINT],
)
_EXP_CODER = TaskSpec(
    task_key="EXP-1",
    title="compute",
    goal="compute ld50",
    route="experiment",
    tools_hint=[CODER_HINT],
)


def test_strip_removes_coder_hint_from_stage_routes() -> None:
    rewritten, touched = strip_coder_hint_off_stage_routes([_HYP_CODER, _EXP_CODER])
    assert touched == ["HYP-1: hypotheses"]
    hyp, exp = rewritten
    assert hyp.tools_hint == []  # stage route: hint stripped
    assert exp.tools_hint == [CODER_HINT]  # experiment route: delivery kept
    # Idempotent: nothing left to strip.
    again, touched2 = strip_coder_hint_off_stage_routes(rewritten)
    assert touched2 == []
    assert again == rewritten


def test_strip_covers_every_stage_route_alias() -> None:
    stage_specs = [
        TaskSpec(task_key=k, title=k, goal=k, route=route, tools_hint=[CODER_HINT])
        for k, route in (
            ("LIT-1", "literature"),
            ("LIT-2", "lit"),
            ("HYP-2", "hypothesis"),
            ("HYP-3", "hyp"),
            ("PLAN-1", "planner"),
            ("PLAN-2", "plan"),
        )
    ]
    rewritten, touched = strip_coder_hint_off_stage_routes(stage_specs)
    assert len(touched) == 6
    assert all(spec.tools_hint == [] for spec in rewritten)


def test_strip_keeps_bare_and_tool_hints() -> None:
    hyp_tools = TaskSpec(
        task_key="HYP-4",
        title="h",
        goal="h",
        route="hypotheses",
        tools_hint=["papers_search"],
    )
    rewritten, touched = strip_coder_hint_off_stage_routes([hyp_tools])
    assert touched == []
    assert rewritten == [hyp_tools]


def test_coder_hint_drop_note_names_tasks_and_rule() -> None:
    note = coder_hint_drop_note(["HYP-1: hypotheses"])
    assert "HYP-1: hypotheses" in note
    assert "'coder'" in note
    assert "маршрут" in note


async def _accept(rt, tasks):  # type: ignore[no-untyped-def]
    return await rt.accept_plan(
        tasks,
        questions=[],
        hypotheses=_NEED_ON,
        literature=NEED_OFF,
        subquestions=[],
        expand_after=[],
        loops=[],
        spec_output=None,
    )


async def test_accept_strips_coder_hint_and_journals_note(tmp_path) -> None:
    """The system guarantee rides the accept path: the stage task is accepted
    with the hint stripped and the PlanAccepted note names the rule."""
    rt = await open_runtime(tmp_path, "coder-hint-strip")
    try:
        await rt.create_study(
            profile="t",
            request="q",
            catalog_version=4,
            require_hypotheses=False,
            confirmations_needed=1,
            refutations_needed=1,
            refutation_priority=False,
            supported_needed=2,
            max_expansions=2,
            max_attempts=3,
            inputs=[],
        )
        pid = await _accept(rt, [_HYP_CODER, _EXP_CODER])
        assert pid
        accepted = [e for e in await rt.store.read_all() if e.type == "PlanAccepted"]
        assert len(accepted) == 1
        note = accepted[0].payload["note"]
        assert isinstance(note, str) and "HYP-1" in note and "маршрут" in note
        # The accepted payload carries the REWRITTEN spec, not the draft.
        payload_hyp = next(t for t in accepted[0].payload["tasks"] if t["task_key"] == "HYP-1")
        assert CODER_HINT not in payload_hyp["tools_hint"]
        payload_exp = next(t for t in accepted[0].payload["tasks"] if t["task_key"] == "EXP-1")
        assert CODER_HINT in payload_exp["tools_hint"]
    finally:
        await close_runtime(rt)
