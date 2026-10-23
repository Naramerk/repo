"""M1 PR10: foreach materialization + quorum fan-in (DESIGN_M1 §4 / S9)."""

from __future__ import annotations

from datetime import timedelta

import pytest

from coscientist.core.artifacts import make_tool_ref
from coscientist.core.domain import (
    AgentTaskOutput,
    ArtifactHandle,
    ForEachSpec,
    InputSpec,
    QuestionSpec,
    RequirementRef,
    TaskOutcome,
    TaskSpec,
    TaskState,
)
from coscientist.core.events import EventEnvelope, make_idem
from coscientist.core.graph import GraphNode
from coscientist.core.ids import hypothesis_node_id, task_id
from coscientist.core.reasons import CloseReason, TaskReason
from coscientist.core.recover import recover
from coscientist.core.upcasters import upcast
from tests.helpers import attach_executor, close_runtime, open_runtime


def _exp(key: str, *, depends_on: list[str], **kw) -> TaskSpec:
    return TaskSpec(
        task_key=key,
        title=kw.get("title", key),
        goal=kw.get("goal", "g"),
        route="experiment",
        depends_on=depends_on,
        addresses=kw.get("addresses", ["Q1"]),
        criteria=[RequirementRef(template_id="value_match")] if kw.get("criteria", True) else [],
        inputs=kw.get("inputs", []),
        foreach=kw.get("foreach"),
        required=kw.get("required", True),
        tolerate_failed_dep=kw.get("tolerate_failed_dep", False),
    )


async def _ok(rt, arts, aid: str) -> None:
    content = b'{"count": 1}'
    art = arts.put(content, make_tool_ref(f"{aid}:out"), name="main", media_type="application/json")
    await rt.executor.record(
        aid,
        AgentTaskOutput(
            summary="ok",
            outputs={"main": ArtifactHandle(artifact_id=art.artifact_id, name="main")},
            values={"count": 1},
        ),
    )


async def _boot_stages(rt) -> None:
    for key in ("SPEC", "HYP", "LIT"):
        await rt.start_attempt(key)
        await rt.cancel_task(key, reason="cancelled")


async def _upsert_selected_hypotheses(rt, *, of_task: str, n: int, selected: bool = True):
    sid = rt.study.study_id
    now = rt.clock.now()
    for i in range(n):
        hid_logical = f"H{i}"
        nid = hypothesis_node_id(sid, hid_logical)
        node = GraphNode(
            node_id=nid,
            kind="hypothesis",
            study_id=sid,
            ref_id=hid_logical,
            attrs={
                "statement": f"s{i}",
                "prediction": f"p{i}",
                "selected": selected,
                "rank": i,
                "source_task": of_task,
            },
            created_at=now,
        )
        await rt._append(
            [
                EventEnvelope(
                    study_id=sid,
                    type="GraphNodeUpserted",
                    entity_key=nid,
                    idem_key=make_idem("GraphNodeUpserted", nid, "pr10"),
                    payload=node.model_dump(mode="json"),
                    ts=now,
                )
            ]
        )


async def _finalize_study(rt):
    for t in list(rt.tasks.values()):
        if t.state != TaskState.DONE:
            await rt.cancel_task(t.task_id, reason="cancelled")
    await rt.tick()
    assert rt._finalized
    events = await rt.store.read_all()
    fins = [e for e in events if e.type == "StudyFinalized"]
    assert len(fins) == 1
    return fins[0]


@pytest.mark.asyncio
async def test_foreach_10_hypotheses(tmp_path, clock, runtime_cls):
    """selected hypotheses of E0 → 10 EXP-<n>; no planner; no expansion quota."""
    sid = "st_fe10"
    rt = await open_runtime(tmp_path, sid, clock=clock, owner="t1", runtime_cls=runtime_cls)
    await rt.create_study(
        profile="research",
        request="q",
        catalog_version="0.1.0",
        deadline=clock.now() + timedelta(seconds=400),
        sid=sid,
        finalize_reserve_s=5,
        max_expansions=0,
    )
    _, arts = attach_executor(rt, tmp_path)
    await _boot_stages(rt)
    e0 = _exp("E0", depends_on=["PLAN"], criteria=False)
    fe_tmpl = _exp(
        "FE",
        depends_on=["E0"],
        foreach=ForEachSpec(kind="hypothesis", of_task="E0", selector="selected"),
        criteria=False,
    )
    await rt.accept_plan([e0, fe_tmpl], questions=[QuestionSpec(id="Q1", text="why")])
    await _upsert_selected_hypotheses(rt, of_task="E0", n=10)
    aid = await rt.start_attempt("E0")
    await _ok(rt, arts, aid)
    children = rt._foreach_children.get("FE") or []
    assert len(children) == 10
    assert all(k.startswith("EXP-") for k in children)
    assert rt._agent_expansions == 0
    fe_tid = task_id(sid, "FE")
    assert rt.tasks[fe_tid].state == TaskState.DONE
    assert rt.tasks[fe_tid].reason == TaskReason.SUPERSEDED
    for k in children:
        t = rt.tasks[task_id(sid, k)]
        assert t.state == TaskState.PENDING
        assert t.spec.foreach is None
        assert t.spec.foreach_item is not None
    fin = await _finalize_study(rt)
    contrib = fin.payload.get("contributing_reasons") or []
    assert any(str(c).startswith("foreach: развёрнуто 10") for c in contrib)
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_foreach_zero_items_report(tmp_path, clock, runtime_cls):
    sid = "st_fe0"
    rt = await open_runtime(tmp_path, sid, clock=clock, owner="t1", runtime_cls=runtime_cls)
    await rt.create_study(
        profile="research",
        request="q",
        catalog_version="0.1.0",
        deadline=clock.now() + timedelta(seconds=300),
        sid=sid,
        finalize_reserve_s=5,
        max_expansions=0,
    )
    _, arts = attach_executor(rt, tmp_path)
    await _boot_stages(rt)
    e0 = _exp("E0", depends_on=["PLAN"], criteria=False)
    fe_tmpl = _exp(
        "FE",
        depends_on=["E0"],
        foreach=ForEachSpec(kind="hypothesis", of_task="E0", selector="selected"),
        criteria=False,
    )
    await rt.accept_plan([e0, fe_tmpl], questions=[QuestionSpec(id="Q1", text="why")])
    aid = await rt.start_attempt("E0")
    await _ok(rt, arts, aid)
    assert rt._foreach_children.get("FE") == []
    fin = await _finalize_study(rt)
    assert "не выполнено: foreach пуст" in (fin.payload.get("contributing_reasons") or [])
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_foreach_restart_replay(tmp_path, clock, runtime_cls):
    """Crash between source DONE and foreach → recover materializes; replay == live."""
    sid = "st_fe_rr"
    rt = await open_runtime(tmp_path, sid, clock=clock, owner="t1", runtime_cls=runtime_cls)
    await rt.create_study(
        profile="research",
        request="q",
        catalog_version="0.1.0",
        deadline=clock.now() + timedelta(seconds=400),
        sid=sid,
        finalize_reserve_s=5,
        max_expansions=0,
    )
    attach_executor(rt, tmp_path)
    await _boot_stages(rt)
    e0 = _exp("E0", depends_on=["PLAN"], criteria=False)
    fe_tmpl = _exp(
        "FE",
        depends_on=["E0"],
        foreach=ForEachSpec(kind="hypothesis", of_task="E0", selector="selected"),
        criteria=False,
    )
    await rt.accept_plan([e0, fe_tmpl], questions=[QuestionSpec(id="Q1", text="why")])
    await _upsert_selected_hypotheses(rt, of_task="E0", n=3)
    e0_tid = task_id(sid, "E0")
    aid = f"{e0_tid}#1"
    now = rt.clock.now()
    await rt.writer.append_many(
        [
            EventEnvelope(
                study_id=sid,
                type="AttemptStarted",
                entity_key=aid,
                idem_key=make_idem("AttemptStarted", aid),
                payload={
                    "attempt_id": aid,
                    "n": 1,
                    "dispatch_id": f"dispatch:{aid}",
                    "route_returned": False,
                    "closed": False,
                    "close_reason": None,
                    "provenance": "real",
                    "agent_output_digest": None,
                    "deadline": (now + timedelta(seconds=60)).isoformat(),
                    "task_id": e0_tid,
                    "study_id": sid,
                    "inputs": [],
                    "limitations": [],
                },
                ts=now,
                schema_version=4,
            ),
            EventEnvelope(
                study_id=sid,
                type="AttemptClosed",
                entity_key=aid,
                idem_key=make_idem("AttemptClosed", aid),
                payload={
                    "attempt_id": aid,
                    "closed": True,
                    "close_reason": CloseReason.RESULT.value,
                    "task_id": e0_tid,
                },
                ts=now,
                schema_version=2,
            ),
            EventEnvelope(
                study_id=sid,
                type="TaskDone",
                entity_key=e0_tid,
                idem_key=make_idem("TaskDone", e0_tid, "manual"),
                payload={
                    "task_id": e0_tid,
                    "outcome": TaskOutcome.SUCCESS.value,
                    "reason": None,
                },
                ts=now,
                schema_version=2,
            ),
        ]
    )
    await close_runtime(rt)
    rt2 = await open_runtime(tmp_path, sid, clock=clock, owner="t2", runtime_cls=runtime_cls)
    attach_executor(rt2, tmp_path)
    out = await recover(rt2)
    assert out.get("foreach_resumed", 0) >= 1 or len(rt2._foreach_children.get("FE") or []) == 3
    assert len(rt2._foreach_children.get("FE") or []) == 3
    await close_runtime(rt2)
    rt3 = await open_runtime(tmp_path, sid, clock=clock, owner="t3", runtime_cls=runtime_cls)
    assert len(rt3._foreach_children.get("FE") or []) == 3
    assert rt3._agent_expansions == 0
    fin = await _finalize_study(rt3)
    assert any("foreach" in str(c) for c in (fin.payload.get("contributing_reasons") or []))
    await close_runtime(rt3)


@pytest.mark.asyncio
async def test_fanin_quorum(tmp_path, clock, runtime_cls):
    sid = "st_q"
    rt = await open_runtime(tmp_path, sid, clock=clock, owner="t1", runtime_cls=runtime_cls)
    await rt.create_study(
        profile="research",
        request="q",
        catalog_version="0.1.0",
        deadline=clock.now() + timedelta(seconds=400),
        sid=sid,
        finalize_reserve_s=5,
    )
    _, arts = attach_executor(rt, tmp_path)
    await _boot_stages(rt)
    a = _exp("A", depends_on=["PLAN"], criteria=False)
    b = _exp("B", depends_on=["PLAN"], criteria=False)
    c = _exp("C", depends_on=["PLAN"], criteria=False)
    summary = TaskSpec(
        task_key="SUM",
        title="sum",
        goal="g",
        route="experiment",
        depends_on=["A", "B", "C"],
        addresses=["Q1"],
        criteria=[],
        tolerate_failed_dep=True,
        inputs=[
            InputSpec(from_task="A", output="main", quorum=2),
            InputSpec(from_task="B", output="main", quorum=2),
            InputSpec(from_task="C", output="main", quorum=2),
        ],
    )
    await rt.accept_plan(
        [a, b, c, summary],
        questions=[QuestionSpec(id="Q1", text="why")],
    )
    for key in ("A", "B"):
        aid = await rt.start_attempt(key)
        await _ok(rt, arts, aid)
    cid = await rt.start_attempt("C")
    await rt.fail_attempt(cid, retryable=False, close_reason="failed")
    c_task = rt.tasks[task_id(sid, "C")]
    if c_task.state != TaskState.DONE:
        await rt.cancel_task(c_task.task_id, reason="failed")
    aid = await rt.start_attempt("SUM")
    att = rt.attempts[aid]
    assert len(att.get("inputs") or []) == 2
    lims = att.get("limitations") or []
    assert lims
    assert any("недоступен" in str(x.get("text") or x) for x in lims)
    await _ok(rt, arts, aid)
    fin = await _finalize_study(rt)
    assert fin.payload.get("terminal_reason")
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_quorum_all_fail(tmp_path, clock, runtime_cls):
    sid = "st_q_all"
    rt = await open_runtime(tmp_path, sid, clock=clock, owner="t1", runtime_cls=runtime_cls)
    await rt.create_study(
        profile="research",
        request="q",
        catalog_version="0.1.0",
        deadline=clock.now() + timedelta(seconds=300),
        sid=sid,
        finalize_reserve_s=5,
    )
    attach_executor(rt, tmp_path)
    await _boot_stages(rt)
    a = _exp("A", depends_on=["PLAN"], criteria=False)
    b = _exp("B", depends_on=["PLAN"], criteria=False)
    summary = TaskSpec(
        task_key="SUM",
        title="sum",
        goal="g",
        route="experiment",
        depends_on=["A", "B"],
        addresses=["Q1"],
        criteria=[],
        tolerate_failed_dep=True,
        inputs=[
            InputSpec(from_task="A", output="main", quorum=1),
            InputSpec(from_task="B", output="main", quorum=1),
        ],
    )
    await rt.accept_plan([a, b, summary], questions=[QuestionSpec(id="Q1", text="why")])
    for key in ("A", "B"):
        aid = await rt.start_attempt(key)
        await rt.fail_attempt(aid, retryable=False, close_reason="failed")
        t = rt.tasks[task_id(sid, key)]
        if t.state != TaskState.DONE:
            await rt.cancel_task(t.task_id, reason="failed")
    from coscientist.core.inputs import InputUnavailable

    with pytest.raises(InputUnavailable):
        await rt.start_attempt("SUM")
    sum_t = rt.tasks[task_id(sid, "SUM")]
    assert sum_t.state == TaskState.DONE
    assert sum_t.reason == TaskReason.INPUT_UNAVAILABLE
    fin = await _finalize_study(rt)
    assert "не выполнено: кворум входов" in (fin.payload.get("contributing_reasons") or [])
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_quorum_tie_exact(tmp_path, clock, runtime_cls):
    sid = "st_q_tie"
    rt = await open_runtime(tmp_path, sid, clock=clock, owner="t1", runtime_cls=runtime_cls)
    await rt.create_study(
        profile="research",
        request="q",
        catalog_version="0.1.0",
        deadline=clock.now() + timedelta(seconds=300),
        sid=sid,
        finalize_reserve_s=5,
    )
    _, arts = attach_executor(rt, tmp_path)
    await _boot_stages(rt)
    tasks = [_exp(k, depends_on=["PLAN"], criteria=False) for k in ("A", "B", "C")]
    summary = TaskSpec(
        task_key="SUM",
        title="sum",
        goal="g",
        route="experiment",
        depends_on=["A", "B", "C"],
        addresses=["Q1"],
        criteria=[],
        tolerate_failed_dep=True,
        inputs=[InputSpec(from_task=k, output="main", quorum=2) for k in ("A", "B", "C")],
    )
    await rt.accept_plan(tasks + [summary], questions=[QuestionSpec(id="Q1", text="why")])
    for key in ("A", "B"):
        aid = await rt.start_attempt(key)
        await _ok(rt, arts, aid)
    aid = await rt.start_attempt("C")
    await rt.fail_attempt(aid, retryable=False, close_reason="failed")
    t = rt.tasks[task_id(sid, "C")]
    if t.state != TaskState.DONE:
        await rt.cancel_task(t.task_id, reason="failed")
    aid = await rt.start_attempt("SUM")
    assert len(rt.attempts[aid]["inputs"]) == 2
    await _ok(rt, arts, aid)
    await _finalize_study(rt)
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_quorum_partial_infra_failures(tmp_path, clock, runtime_cls):
    sid = "st_q_infra"
    rt = await open_runtime(tmp_path, sid, clock=clock, owner="t1", runtime_cls=runtime_cls)
    await rt.create_study(
        profile="research",
        request="q",
        catalog_version="0.1.0",
        deadline=clock.now() + timedelta(seconds=300),
        sid=sid,
        finalize_reserve_s=5,
    )
    _, arts = attach_executor(rt, tmp_path)
    await _boot_stages(rt)
    a = _exp("A", depends_on=["PLAN"], criteria=False)
    b = _exp("B", depends_on=["PLAN"], criteria=False)
    summary = TaskSpec(
        task_key="SUM",
        title="sum",
        goal="g",
        route="experiment",
        depends_on=["A", "B"],
        addresses=["Q1"],
        criteria=[],
        tolerate_failed_dep=True,
        inputs=[
            InputSpec(from_task="A", output="main", quorum=1),
            InputSpec(from_task="B", output="main", quorum=1),
        ],
    )
    await rt.accept_plan([a, b, summary], questions=[QuestionSpec(id="Q1", text="why")])
    aid_a = await rt.start_attempt("A")
    await rt.fail_attempt(aid_a, retryable=False, close_reason="crashed")
    t = rt.tasks[task_id(sid, "A")]
    if t.state != TaskState.DONE:
        await rt.cancel_task(t.task_id, reason="failed")
    aid_b = await rt.start_attempt("B")
    await _ok(rt, arts, aid_b)
    aid = await rt.start_attempt("SUM")
    lims = rt.attempts[aid].get("limitations") or []
    assert lims
    assert len(rt.attempts[aid]["inputs"]) == 1
    await _ok(rt, arts, aid)
    fin = await _finalize_study(rt)
    assert fin.payload.get("minimal_report") or fin.payload.get("report_artifact_id")
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_foreach_plus_quorum_fanin(tmp_path, clock, runtime_cls):
    sid = "st_fe_q"
    rt = await open_runtime(tmp_path, sid, clock=clock, owner="t1", runtime_cls=runtime_cls)
    await rt.create_study(
        profile="research",
        request="q",
        catalog_version="0.1.0",
        deadline=clock.now() + timedelta(seconds=500),
        sid=sid,
        finalize_reserve_s=5,
        max_expansions=0,
    )
    _, arts = attach_executor(rt, tmp_path)
    await _boot_stages(rt)
    e0 = _exp("E0", depends_on=["PLAN"], criteria=False)
    fe = _exp(
        "FE",
        depends_on=["E0"],
        foreach=ForEachSpec(kind="hypothesis", of_task="E0", selector="selected"),
        criteria=False,
    )
    summary = TaskSpec(
        task_key="SUM",
        title="sum",
        goal="g",
        route="experiment",
        depends_on=["FE"],
        addresses=["Q1"],
        criteria=[],
        tolerate_failed_dep=True,
        inputs=[InputSpec(from_task="FE", output="main", quorum=2)],
    )
    await rt.accept_plan(
        [e0, fe, summary],
        questions=[QuestionSpec(id="Q1", text="why")],
    )
    await _upsert_selected_hypotheses(rt, of_task="E0", n=3)
    aid = await rt.start_attempt("E0")
    await _ok(rt, arts, aid)
    kids = rt._foreach_children["FE"]
    assert len(kids) == 3
    for k in kids[:2]:
        aid = await rt.start_attempt(k)
        await _ok(rt, arts, aid)
    aid = await rt.start_attempt(kids[2])
    await rt.fail_attempt(aid, retryable=False, close_reason="failed")
    t = rt.tasks[task_id(sid, kids[2])]
    if t.state != TaskState.DONE:
        await rt.cancel_task(t.task_id, reason="failed")
    aid = await rt.start_attempt("SUM")
    assert len(rt.attempts[aid]["inputs"]) == 2
    await _ok(rt, arts, aid)
    fin = await _finalize_study(rt)
    contrib = fin.payload.get("contributing_reasons") or []
    assert any("foreach: развёрнуто 3" in str(c) for c in contrib)
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_quorum_zero_inputs_edge(tmp_path, clock, runtime_cls):
    sid = "st_q0"
    rt = await open_runtime(tmp_path, sid, clock=clock, owner="t1", runtime_cls=runtime_cls)
    await rt.create_study(
        profile="research",
        request="q",
        catalog_version="0.1.0",
        deadline=clock.now() + timedelta(seconds=300),
        sid=sid,
        finalize_reserve_s=5,
        max_expansions=0,
    )
    _, arts = attach_executor(rt, tmp_path)
    await _boot_stages(rt)
    e0 = _exp("E0", depends_on=["PLAN"], criteria=False)
    fe = _exp(
        "FE",
        depends_on=["E0"],
        foreach=ForEachSpec(kind="hypothesis", of_task="E0", selector="selected"),
        criteria=False,
    )
    summary = TaskSpec(
        task_key="SUM",
        title="sum",
        goal="g",
        route="experiment",
        depends_on=["FE"],
        addresses=["Q1"],
        criteria=[],
        inputs=[InputSpec(from_task="FE", output="main", quorum=1)],
    )
    await rt.accept_plan([e0, fe, summary], questions=[QuestionSpec(id="Q1", text="why")])
    aid = await rt.start_attempt("E0")
    await _ok(rt, arts, aid)
    assert rt._foreach_children.get("FE") == []
    from coscientist.core.inputs import InputUnavailable

    with pytest.raises(InputUnavailable):
        await rt.start_attempt("SUM")
    fin = await _finalize_study(rt)
    contrib = fin.payload.get("contributing_reasons") or []
    assert "не выполнено: foreach пуст" in contrib
    assert "не выполнено: кворум входов" in contrib
    await close_runtime(rt)


def test_upcast_task_created_foreach_quorum():
    v, p = upcast(
        "TaskCreated",
        6,
        {
            "task_id": "s/E0",
            "kind": "experiment",
            "spec": {
                "task_key": "E0",
                "title": "t",
                "goal": "g",
                "route": "experiment",
                "inputs": [{"from_task": "A", "output": "main"}],
            },
            "state": "PENDING",
            "deadline": "2020-01-01T00:00:00+00:00",
            "attempts": [],
        },
    )
    assert v == 7
    assert p["spec"]["foreach"] is None
    assert p["spec"]["inputs"][0]["quorum"] is None


def test_upcast_attempt_limitations():
    v, p = upcast("AttemptStarted", 3, {"attempt_id": "a", "inputs": []})
    assert v == 4
    assert p["limitations"] == []
