"""RC6 (#102): selected hyp referenced by ≥1 plan task."""

from __future__ import annotations

from datetime import timedelta

import pytest

from coscientist.core.claims.graph import GraphNode
from coscientist.core.claims.reasons import TaskReason
from coscientist.core.events.events import EventEnvelope, make_idem
from coscientist.core.events.ids import hypothesis_node_id, task_id
from coscientist.core.events.upcasters import writer_schema_version
from coscientist.core.execution.domain import (
    ForEachSpec,
    Need,
    QuestionSpec,
    TaskOutcome,
    TaskSpec,
    TaskState,
)
from coscientist.core.plan.foreach import assert_selected_hyp_ref_for_hyp_n
from coscientist.core.plan.need import (
    PlanNeedRefuse,
    assert_foreach_items_include_selected,
    assert_plan_refs_selected_hyp,
    selected_hypothesis_id,
)
from tests.helpers import NEED_OFF, attach_executor, close_runtime, open_runtime

_NEED_ON = Need(kind="need", needed=True, reason="hypotheses required")
_DETAIL_PREFIX = "selected_hyp_ref_required:"


def _row(
    i: int,
    *,
    selected: bool = False,
    hid: str | None = None,
) -> dict[str, object]:
    return {
        "id": hid if hid is not None else f"H{i}",
        "statement": f"s{i}",
        "prediction": f"p{i}",
        "rank": i,
        "selected": selected,
    }


def _ok_three(*, selected_index: int = 0) -> list[dict[str, object]]:
    return [_row(i, selected=(i == selected_index)) for i in range(3)]


def _exp(*, foreach_item: str | None = None) -> TaskSpec:
    return TaskSpec(
        task_key="EXP-1",
        title="e",
        goal="e",
        route="experiment",
        foreach_item=foreach_item,
    )


def test_plan_requires_selected_hyp_ref() -> None:
    """DoD: plan without a task bound to the selected id → typed refuse."""
    hyps = _ok_three(selected_index=1)
    with pytest.raises(PlanNeedRefuse) as ei:
        assert_plan_refs_selected_hyp([_exp()], hyps)
    assert ei.value.reason == "selected_hyp_ref_required"
    assert str(ei.value).startswith(_DETAIL_PREFIX)
    assert "selected_id=H1" in str(ei.value)


def test_plan_accepts_foreach_item_bound_to_selected() -> None:
    hyps = _ok_three(selected_index=2)
    assert selected_hypothesis_id(hyps) == "H2"
    assert_plan_refs_selected_hyp([_exp(foreach_item="H2")], hyps)


def test_foreach_items_must_include_selected() -> None:
    hyps = _ok_three(selected_index=0)
    assert_foreach_items_include_selected(["H0", "H1"], hyps)
    with pytest.raises(PlanNeedRefuse) as ei:
        assert_foreach_items_include_selected(["H1", "H2"], hyps)
    assert ei.value.reason == "selected_hyp_ref_required"
    assert "selected_id=H0" in str(ei.value)


def test_selected_hyp_ref_gate_skips_non_hyp_n_source() -> None:
    """Non-HYP-n of_task (PR10 fixtures) must not apply RC6."""

    class _RT:
        graph = None

    assert_selected_hyp_ref_for_hyp_n(
        _RT(),  # type: ignore[arg-type]
        ForEachSpec(kind="hypothesis", of_task="E0", selector="selected"),
        items=[],
    )


async def _upsert_hyps(
    rt,
    *,
    of_task: str,
    n: int,
    selected_index: int | None = 0,
) -> None:
    sid = rt.study.study_id
    now = rt.clock.now()
    for i in range(n):
        hid = f"H{i}"
        nid = hypothesis_node_id(sid, hid)
        selected = selected_index is not None and i == selected_index
        node = GraphNode(
            node_id=nid,
            kind="hypothesis",
            study_id=sid,
            ref_id=hid,
            attrs={
                "statement": f"s{i}",
                "prediction": f"p{i}",
                "selected": selected,
                "rank": i,
                "source_task": of_task,
            },
            created_at=now,
        )
        await rt.append_events(
            [
                EventEnvelope(
                    study_id=sid,
                    type="GraphNodeUpserted",
                    entity_key=nid,
                    idem_key=make_idem("GraphNodeUpserted", nid, "rc6"),
                    payload=node.model_dump(mode="json"),
                    ts=now,
                    schema_version=writer_schema_version("GraphNodeUpserted"),
                )
            ]
        )


async def _boot_stages(rt) -> None:
    for key in ("SPEC", "HYP", "LIT"):
        await rt.start_attempt(key)
        await rt.cancel_task(key, reason="cancelled")


async def _mark_hyp_n_success(rt, arts, task_key: str = "HYP-0") -> None:
    """Close HYP-n as SUCCESS so foreach selects items (not SKIPPED cancel)."""
    from coscientist.core.execution.artifacts import make_tool_ref
    from coscientist.core.execution.domain import AgentTaskOutput, ArtifactHandle

    sid = rt.study.study_id
    tid = task_id(sid, task_key)
    task = rt.tasks[tid]
    rt.tasks[tid] = task.model_copy(
        update={"spec": task.spec.model_copy(update={"criteria": []})}
    )
    aid = await rt.start_attempt(task_key)
    content = b'{"count": 1}'
    art = arts.put(
        content, make_tool_ref(f"{aid}:out"), name="main", media_type="application/json"
    )
    await rt.executor.record(
        aid,
        AgentTaskOutput(
            summary="ok",
            outputs={"main": ArtifactHandle(artifact_id=art.artifact_id, name="main")},
            values={"count": 1},
        ),
    )
    assert rt.tasks[tid].state == TaskState.DONE
    assert rt.tasks[tid].outcome == TaskOutcome.SUCCESS


@pytest.mark.asyncio
async def test_foreach_hyp_n_refuses_when_selected_not_in_items(tmp_path) -> None:
    """HYP-n SKIPPED → empty foreach items → ExpansionRefused (RC6)."""
    rt = await open_runtime(tmp_path, "rc6-noref")
    try:
        await rt.create_study(
            profile="t",
            request="q",
            catalog_version=4,
            deadline=rt.clock.now() + timedelta(seconds=400),
            require_hypotheses=True,
            confirmations_needed=1,
            refutations_needed=1,
            refutation_priority=False,
            supported_needed=2,
            max_expansions=2,
            max_attempts=3,
            inputs=[],
        )
        attach_executor(rt, tmp_path)
        await _boot_stages(rt)
        hyp = TaskSpec(
            task_key="HYP-0",
            title="hypotheses",
            goal="propose",
            route="hypotheses",
        )
        fe = TaskSpec(
            task_key="FE",
            title="fe",
            goal="fe",
            route="experiment",
            depends_on=["HYP-0"],
            foreach=ForEachSpec(kind="hypothesis", of_task="HYP-0", selector="selected"),
            criteria=[],
            addresses=[],
        )
        pid = await rt.accept_plan(
            [hyp, fe],
            questions=[QuestionSpec(id="Q1", text="q")],
            hypotheses=_NEED_ON,
            literature=NEED_OFF,
            subquestions=[],
            expand_after=[],
            loops=[],
            spec_output=None,
        )
        assert pid
        await _upsert_hyps(rt, of_task="HYP-0", n=3, selected_index=0)
        await rt.start_attempt("HYP-0")
        await rt.cancel_task("HYP-0", reason="cancelled", detail="skipped source")
        fe_tid = task_id(rt.study.study_id, "FE")
        assert rt.tasks[fe_tid].state == TaskState.DONE
        assert rt.tasks[fe_tid].reason == TaskReason.REFUSED
        refused = [e for e in await rt.store.read_all() if e.type == "ExpansionRefused"]
        assert len(refused) == 1
        assert refused[0].payload["reason"] == "selected_hyp_ref_required"
        assert str(refused[0].payload["detail"]).startswith(_DETAIL_PREFIX)
    finally:
        await close_runtime(rt)


@pytest.mark.asyncio
async def test_foreach_hyp_n_binds_selected_foreach_item(tmp_path) -> None:
    """HYP-n SUCCESS + selected artifact → child foreach_item == selected id."""
    rt = await open_runtime(tmp_path, "rc6-ok")
    try:
        await rt.create_study(
            profile="t",
            request="q",
            catalog_version=4,
            deadline=rt.clock.now() + timedelta(seconds=400),
            require_hypotheses=True,
            confirmations_needed=1,
            refutations_needed=1,
            refutation_priority=False,
            supported_needed=2,
            max_expansions=2,
            max_attempts=3,
            inputs=[],
        )
        _, arts = attach_executor(rt, tmp_path)
        await _boot_stages(rt)
        hyp = TaskSpec(
            task_key="HYP-0",
            title="hypotheses",
            goal="propose",
            route="hypotheses",
        )
        fe = TaskSpec(
            task_key="FE",
            title="fe",
            goal="fe",
            route="experiment",
            depends_on=["HYP-0"],
            foreach=ForEachSpec(kind="hypothesis", of_task="HYP-0", selector="selected"),
            criteria=[],
            addresses=[],
        )
        pid = await rt.accept_plan(
            [hyp, fe],
            questions=[QuestionSpec(id="Q1", text="q")],
            hypotheses=_NEED_ON,
            literature=NEED_OFF,
            subquestions=[],
            expand_after=[],
            loops=[],
            spec_output=None,
        )
        assert pid
        await _upsert_hyps(rt, of_task="HYP-0", n=3, selected_index=1)
        await _mark_hyp_n_success(rt, arts, "HYP-0")
        children = getattr(rt, "_foreach_children", {}).get("FE") or []
        assert len(children) == 1
        child = rt.tasks[task_id(rt.study.study_id, children[0])]
        assert child.spec.foreach_item == "H1"
        assert_plan_refs_selected_hyp(
            [child.spec],
            [
                _row(0),
                _row(1, selected=True),
                _row(2),
            ],
        )
    finally:
        await close_runtime(rt)
