"""RC5 (#101): hypotheses artifact validate — ≥3 hyps; exactly one selected."""

from __future__ import annotations

from datetime import timedelta

import pytest

from coscientist.core.claims.graph import GraphNode
from coscientist.core.claims.reasons import TaskReason
from coscientist.core.events.events import EventEnvelope, make_idem
from coscientist.core.events.ids import hypothesis_node_id, task_id
from coscientist.core.events.upcasters import writer_schema_version
from coscientist.core.execution.domain import ForEachSpec, Need, QuestionSpec, TaskSpec, TaskState
from coscientist.core.plan.foreach import assert_hypotheses_artifact_for_hyp_n
from coscientist.core.plan.need import PlanNeedRefuse, validate_hypotheses_artifact
from tests.helpers import NEED_OFF, attach_executor, close_runtime, open_runtime

_NEED_ON = Need(kind="need", needed=True, reason="hypotheses required")


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


@pytest.mark.parametrize(
    ("hyps", "reason", "detail_prefix"),
    [
        ([_row(0, selected=True), _row(1)], "hypotheses_lt_three", "hypotheses_lt_three:"),
        (
            [_row(0), _row(1), _row(2)],
            "hypotheses_no_selected",
            "hypotheses_no_selected:",
        ),
        ([], "hypotheses_lt_three", "hypotheses_lt_three:"),
    ],
)
def test_rejects_lt_three_or_no_selected(
    hyps: list[dict[str, object]], reason: str, detail_prefix: str
) -> None:
    """DoD: <3 hyps or zero selected → typed refuse."""
    with pytest.raises(PlanNeedRefuse) as ei:
        validate_hypotheses_artifact(hyps)
    assert ei.value.reason == reason
    assert str(ei.value).startswith(detail_prefix)


def test_rejects_selected_not_unique() -> None:
    hyps = [_row(0, selected=True), _row(1, selected=True), _row(2)]
    with pytest.raises(PlanNeedRefuse) as ei:
        validate_hypotheses_artifact(hyps)
    assert ei.value.reason == "hypotheses_selected_not_unique"
    assert "hypotheses_selected_not_unique:count=2" in str(ei.value)


def test_accepts_three_with_exactly_one_selected() -> None:
    validate_hypotheses_artifact(_ok_three(selected_index=1))


@pytest.mark.parametrize(
    "bad",
    [
        {**_row(9, selected=True, hid="Hbad"), "statement": ""},
        {k: v for k, v in _row(9, selected=True, hid="Hbad").items() if k != "rank"},
        {**_row(9, selected=True, hid="Hbad"), "selected": 1},
        {k: v for k, v in _row(9, selected=True, hid="Hbad").items() if k != "id"},
    ],
)
def test_rejects_invalid_row_fields(bad: dict[str, object]) -> None:
    hyps: list[dict[str, object]] = [_row(1), _row(2), bad]
    with pytest.raises(PlanNeedRefuse) as ei:
        validate_hypotheses_artifact(hyps)
    assert ei.value.reason == "invalid"


def test_assert_hyp_n_gate_skips_non_hyp_n_source() -> None:
    """Non-HYP-n of_task (e.g. E0 fixtures) must not apply RC5 (≥2 hyps OK)."""

    class _RT:
        graph = None

    assert_hypotheses_artifact_for_hyp_n(
        _RT(),  # type: ignore[arg-type]
        ForEachSpec(kind="hypothesis", of_task="E0", selector="selected"),
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
                    idem_key=make_idem("GraphNodeUpserted", nid, "rc5"),
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


async def _study_with_hyp_foreach(tmp_path, name: str, *, selector: str):
    rt = await open_runtime(tmp_path, name)
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
        foreach=ForEachSpec(kind="hypothesis", of_task="HYP-0", selector=selector),
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
    return rt


@pytest.mark.asyncio
async def test_foreach_hyp_n_refuses_lt_three(tmp_path) -> None:
    """HYP-n foreach source with <3 artifact hyps → ExpansionRefused (RC5)."""
    rt = await _study_with_hyp_foreach(tmp_path, "rc5-lt3", selector="selected")
    try:
        await _upsert_hyps(rt, of_task="HYP-0", n=2, selected_index=0)
        await rt.start_attempt("HYP-0")
        await rt.cancel_task("HYP-0", reason="cancelled", detail="done for foreach")
        fe_tid = task_id(rt.study.study_id, "FE")
        assert rt.tasks[fe_tid].state == TaskState.DONE
        assert rt.tasks[fe_tid].reason == TaskReason.REFUSED
        refused = [e for e in await rt.store.read_all() if e.type == "ExpansionRefused"]
        assert len(refused) == 1
        assert refused[0].payload["reason"] == "hypotheses_lt_three"
        assert str(refused[0].payload["detail"]).startswith("hypotheses_lt_three:")
    finally:
        await close_runtime(rt)


@pytest.mark.asyncio
async def test_foreach_hyp_n_refuses_no_selected(tmp_path) -> None:
    rt = await _study_with_hyp_foreach(tmp_path, "rc5-noselect", selector="all")
    try:
        await _upsert_hyps(rt, of_task="HYP-0", n=3, selected_index=None)
        await rt.start_attempt("HYP-0")
        await rt.cancel_task("HYP-0", reason="cancelled", detail="done for foreach")
        refused = [e for e in await rt.store.read_all() if e.type == "ExpansionRefused"]
        assert len(refused) == 1
        assert refused[0].payload["reason"] == "hypotheses_no_selected"
    finally:
        await close_runtime(rt)
