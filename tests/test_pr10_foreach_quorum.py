"""M1 PR10: foreach materialization + quorum fan-in (DESIGN_M1 §4 / S9)."""

from __future__ import annotations

from datetime import timedelta

import pytest

from coscientist.core.claims.graph import GraphNode
from coscientist.core.claims.reasons import CloseReason, TaskReason
from coscientist.core.events.events import EventEnvelope, make_idem
from coscientist.core.events.ids import hypothesis_node_id, task_id
from coscientist.core.events.upcasters import writer_schema_version
from coscientist.core.execution.artifacts import make_tool_ref
from coscientist.core.execution.domain import (
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
from coscientist.core.execution.inputs import InputUnavailable
from coscientist.core.execution.recover import recover
from tests.helpers import attach_executor, close_runtime, open_runtime


def _exp(key: str, *, depends_on: list[str], **kw) -> TaskSpec:
    return TaskSpec(
        task_key=key,
        title=kw.get("title", key),
        goal=kw.get("goal", "g"),
        route="experiment",
        depends_on=depends_on,
        addresses=kw.get(
            "addresses",
            ["Q1"] if kw.get("criteria", True) else [],
        ),
        criteria=[RequirementRef(template_id="value_match")] if kw.get("criteria", True) else [],
        inputs=kw.get("inputs", []),
        foreach=kw.get("foreach"),
        required=kw.get("required", True),
        tolerate_failed_dep=kw.get("tolerate_failed_dep", False),
        tests_intent=kw.get("tests_intent"),
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


def test_foreach_selected_ignores_verdict():
    """Planner/expand never set ``selected`` from verdict; foreach reads attrs only.

    Guaranteed today: the only ``selected`` read in coscientist is
    ``foreach.select_foreach_items`` on ``attrs.selected``. An inconclusive
    (e.g. refute_unresolved) hypothesis with ``selected=True`` still expands;
    ``selected=False`` is dropped even if verdict would be ``supported``.
    """
    from pathlib import Path

    from coscientist.core.claims.graph import GraphStore, default_execution_registry
    from coscientist.core.plan.foreach import select_foreach_items

    # Static guarantee: no production write of selected from verdict/planner.
    root = Path(__file__).resolve().parents[1] / "coscientist"
    offenders: list[str] = []
    for path in root.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if path.name == "foreach.py":
            continue  # selector read is expected
        # Heuristic: assigning selected from a verdict-like name.
        if "selected" in text and ("verdict" in text):
            for i, line in enumerate(text.splitlines(), 1):
                low = line.lower()
                if "selected" in low and "verdict" in low and ("=" in line or "attrs" in low):
                    if "selector" in low or "foreach" in low or "literal" in low:
                        continue
                    if line.strip().startswith("#") or line.strip().startswith('"""'):
                        continue
                    offenders.append(f"{path.relative_to(root)}:{i}:{line.strip()}")
    assert not offenders, "selected must not be derived from verdict:\n" + "\n".join(
        offenders
    )

    class _RT:
        def __init__(self, graph):
            self.graph = graph

    sid = "st_sel"
    g = GraphStore(default_execution_registry())
    # selected=True + inconclusive → included
    g.upsert_node(
        GraphNode(
            node_id=hypothesis_node_id(sid, "H_inc"),
            kind="hypothesis",
            study_id=sid,
            ref_id="H_inc",
            attrs={
                "selected": True,
                "verdict": "inconclusive",
                "rule": "refute_unresolved",
                "source_task": "E0",
                "rank": 0,
            },
        )
    )
    # selected=False + supported → excluded (verdict must not resurrect it)
    g.upsert_node(
        GraphNode(
            node_id=hypothesis_node_id(sid, "H_sup"),
            kind="hypothesis",
            study_id=sid,
            ref_id="H_sup",
            attrs={
                "selected": False,
                "verdict": "supported",
                "source_task": "E0",
                "rank": 1,
            },
        )
    )
    # selected=True + refuted → still included
    g.upsert_node(
        GraphNode(
            node_id=hypothesis_node_id(sid, "H_ref"),
            kind="hypothesis",
            study_id=sid,
            ref_id="H_ref",
            attrs={
                "selected": True,
                "verdict": "refuted",
                "source_task": "E0",
                "rank": 2,
            },
        )
    )
    items, _notes = select_foreach_items(
        _RT(g),
        ForEachSpec(kind="hypothesis", of_task="E0", selector="selected"),
    )
    assert items == ["H_inc", "H_ref"]
    assert "H_sup" not in items


async def _upsert_selected_hypotheses(
    rt, *, of_task: str, n: int, selected: bool = True, ids: list[str] | None = None
):
    sid = rt.study.study_id
    now = rt.clock.now()
    logicals = ids if ids is not None else [f"H{i}" for i in range(n)]
    for i, hid_logical in enumerate(logicals):
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
                    schema_version=writer_schema_version('GraphNodeUpserted'),
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
async def test_foreach_when_source_already_done_live_eq_replay(tmp_path, clock, runtime_cls):
    """B1: of_task=HYP already DONE at accept_plan — expand live; reopen == live."""
    sid = "st_fe_hyp0"
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
    # Finish SPEC; keep HYP as SUCCESS source (canonical HYP-0 case uses stage HYP).
    await rt.start_attempt("SPEC")
    await rt.cancel_task("SPEC", reason="cancelled")
    await rt.start_attempt("HYP")
    # Mark HYP SUCCESS via cancel? Need SUCCESS — use record path with empty criteria.
    hyp = rt.tasks[task_id(sid, "HYP")]
    rt.tasks[hyp.task_id] = hyp.model_copy(
        update={"spec": hyp.spec.model_copy(update={"criteria": []})}
    )
    aid = list(rt.attempts)[-1] if rt.attempts else await rt.start_attempt("HYP")
    # reopen attempt if cancelled — start fresh
    if rt.tasks[task_id(sid, "HYP")].state == TaskState.DONE:
        pass
    else:
        # ensure open attempt
        open_a = [a for a in rt.tasks[task_id(sid, "HYP")].attempts if not rt.attempts.get(a, {}).get("closed")]
        if not open_a:
            aid = await rt.start_attempt("HYP")
        else:
            aid = open_a[0]
        await _ok(rt, arts, aid)
    assert rt.tasks[task_id(sid, "HYP")].state == TaskState.DONE
    assert rt.tasks[task_id(sid, "HYP")].outcome == TaskOutcome.SUCCESS

    await _upsert_selected_hypotheses(rt, of_task="HYP", n=3)
    # LIT still pending — cancel so plan can accept without waiting.
    await rt.start_attempt("LIT")
    await rt.cancel_task("LIT", reason="cancelled")

    fe_tmpl = _exp(
        "FE",
        depends_on=["HYP"],
        foreach=ForEachSpec(kind="hypothesis", of_task="HYP", selector="selected"),
        criteria=False,
    )
    await rt.accept_plan([fe_tmpl], questions=[QuestionSpec(id="Q1", text="why")])
    # B1: expanded immediately because HYP already DONE.
    children_live = list(rt._foreach_children.get("FE") or [])
    assert len(children_live) == 3
    assert rt.tasks[task_id(sid, "FE")].reason == TaskReason.SUPERSEDED
    items_live = sorted(
        rt.tasks[task_id(sid, k)].spec.foreach_item for k in children_live
    )

    await close_runtime(rt)
    rt2 = await open_runtime(tmp_path, sid, clock=clock, owner="t2", runtime_cls=runtime_cls)
    children_replay = list(rt2._foreach_children.get("FE") or [])
    assert children_replay == children_live
    items_replay = sorted(
        rt2.tasks[task_id(sid, k)].spec.foreach_item for k in children_replay
    )
    assert items_replay == items_live
    assert rt2._agent_expansions == 0
    fin = await _finalize_study(rt2)
    assert any("foreach: развёрнуто 3" in str(c) for c in (fin.payload.get("contributing_reasons") or []))
    await close_runtime(rt2)


@pytest.mark.asyncio
async def test_foreach_natural_sort_and_source_task_required(tmp_path, clock, runtime_cls):
    sid = "st_fe_nat"
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
    await rt.accept_plan([e0, fe], questions=[QuestionSpec(id="Q1", text="why")])
    await _upsert_selected_hypotheses(
        rt, of_task="E0", n=3, ids=["H10", "H2", "H1"]
    )
    # Orphan without source_task must be ignored.
    nid = hypothesis_node_id(sid, "Horphan")
    await rt._append(
        [
            EventEnvelope(
                study_id=sid,
                type="GraphNodeUpserted",
                entity_key=nid,
                idem_key=make_idem("GraphNodeUpserted", nid, "orphan"),
                payload=GraphNode(
                    node_id=nid,
                    kind="hypothesis",
                    study_id=sid,
                    ref_id="Horphan",
                    attrs={"statement": "x", "prediction": "y", "selected": True, "rank": 0},
                    created_at=rt.clock.now(),
                ).model_dump(mode="json"),
                ts=rt.clock.now(),
                schema_version=writer_schema_version('GraphNodeUpserted'),
                )
        ]
    )
    aid = await rt.start_attempt("E0")
    await _ok(rt, arts, aid)
    kids = rt._foreach_children["FE"]
    items = [rt.tasks[task_id(sid, k)].spec.foreach_item for k in kids]
    assert items == ["H1", "H2", "H10"]
    assert "Horphan" not in items
    await _finalize_study(rt)
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
        tests_intent="refute",
    )
    await rt.accept_plan([e0, fe_tmpl], questions=[QuestionSpec(id="Q1", text="why")])
    aid = await rt.start_attempt("E0")
    await _ok(rt, arts, aid)
    assert rt._foreach_children.get("FE") == []
    fin = await _finalize_study(rt)
    contrib = fin.payload.get("contributing_reasons") or []
    assert "не выполнено: foreach пуст" in contrib
    assert "не выполнено: проверка опровержения не выполнена" in contrib
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_foreach_cap_limitation(tmp_path, clock, runtime_cls):
    """N2: cap keeps best ranks; journals total/dropped; report names ids."""
    sid = "st_fe_cap"
    rt = await open_runtime(tmp_path, sid, clock=clock, owner="t1", runtime_cls=runtime_cls)
    await rt.create_study(
        profile="research",
        request="q",
        catalog_version="0.1.0",
        deadline=clock.now() + timedelta(seconds=300),
        sid=sid,
        finalize_reserve_s=5,
        max_expansions=0,
        max_foreach_children=3,
    )
    _, arts = attach_executor(rt, tmp_path)
    await _boot_stages(rt)
    e0 = _exp("E0", depends_on=["PLAN"], criteria=False)
    fe = _exp(
        "FE",
        depends_on=["E0"],
        foreach=ForEachSpec(kind="hypothesis", of_task="E0", selector="selected"),
        criteria=False,
        tests_intent="refute",
    )
    await rt.accept_plan([e0, fe], questions=[QuestionSpec(id="Q1", text="why")])
    # Worse natural order first, but better ranks (0..2) must be kept — not nat tail.
    await _upsert_selected_hypotheses(
        rt, of_task="E0", n=5, ids=["H10", "H9", "H8", "H1", "H2"]
    )
    aid = await rt.start_attempt("E0")
    await _ok(rt, arts, aid)
    kids = rt._foreach_children["FE"]
    assert len(kids) == 3
    kept = [rt.tasks[task_id(sid, k)].spec.foreach_item for k in kids]
    assert set(kept) == {"H10", "H9", "H8"}  # ranks 0..2 kept (not nat-tail H1,H2)
    assert kept == ["H8", "H9", "H10"]  # then nat sort among kept
    pe = [e for e in await rt.store.read_all() if e.type == "PlanExpanded"]
    assert pe and pe[0].payload.get("foreach_total") == 5
    assert pe[0].payload.get("foreach_dropped") == ["H1", "H2"]
    fin = await _finalize_study(rt)
    contrib = fin.payload.get("contributing_reasons") or []
    assert any("отброшены H1, H2" in str(c) for c in contrib)
    assert any(
        "проверка опровержения не выполнена (H1)" in str(c) for c in contrib
    )
    assert any(
        "проверка опровержения не выполнена (H2)" in str(c) for c in contrib
    )
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_foreach_restart_replay(tmp_path, clock, runtime_cls):
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
                    "attempt_role": "agent",
                    "charged": True,
                },
                ts=now,
                schema_version=1,
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
                schema_version=1,
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
                schema_version=1,
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
    # Deterministic plan id / idem — only one foreach PlanExpanded.
    pe = [e for e in await rt3.store.read_all() if e.type == "PlanExpanded" and e.payload.get("foreach")]
    assert len(pe) == 1
    assert pe[0].payload["plan_id"].startswith(f"pl_foreach_{sid}_FE_")
    await _finalize_study(rt3)
    await close_runtime(rt3)


@pytest.mark.asyncio
async def test_fanin_quorum(tmp_path, clock, runtime_cls):
    """R1: static A,B,C share one pool — quorum=2 of 3 starts (DESIGN_M1:245)."""
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
        criteria=[RequirementRef(template_id="artifact_present")],
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
        criteria=[RequirementRef(template_id="artifact_present")],
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
    with pytest.raises(InputUnavailable):
        await rt.start_attempt("SUM")
    sum_t = rt.tasks[task_id(sid, "SUM")]
    assert sum_t.state == TaskState.DONE
    assert sum_t.reason == TaskReason.INPUT_UNAVAILABLE
    fin = await _finalize_study(rt)
    assert "не выполнено: кворум входов" in (fin.payload.get("contributing_reasons") or [])
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_quorum_required_not_softened(tmp_path, clock, runtime_cls):
    """M2: required (no quorum) input stays required even if sibling has quorum."""
    sid = "st_q_req"
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
        criteria=[RequirementRef(template_id="artifact_present")],
        tolerate_failed_dep=True,
        inputs=[
            InputSpec(from_task="A", output="main", quorum=1),  # pool
            InputSpec(from_task="B", output="main"),  # required, no quorum
        ],
    )
    await rt.accept_plan([a, b, summary], questions=[QuestionSpec(id="Q1", text="why")])
    aid = await rt.start_attempt("A")
    await _ok(rt, arts, aid)
    # B fails — required must block even though quorum pool is satisfied.
    bid = await rt.start_attempt("B")
    await rt.fail_attempt(bid, retryable=False, close_reason="failed")
    t = rt.tasks[task_id(sid, "B")]
    if t.state != TaskState.DONE:
        await rt.cancel_task(t.task_id, reason="failed")
    with pytest.raises(InputUnavailable):
        await rt.start_attempt("SUM")
    await _finalize_study(rt)
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_quorum_tie_exact(tmp_path, clock, runtime_cls):
    """Static pool: exactly quorum=2 of A,B,C."""
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
        criteria=[RequirementRef(template_id="artifact_present")],
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
    """Static pool quorum=1: one infra-fail → start with limitations."""
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
        criteria=[RequirementRef(template_id="artifact_present")],
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
async def test_quorum_xy_not_pooled_by_value(tmp_path, clock, runtime_cls):
    """M2: foreach X q=2 and Y q=2 stay separate — X children never satisfy Y."""
    sid = "st_q_xy"
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
    src_x = _exp("SX", depends_on=["PLAN"], criteria=False)
    src_y = _exp("SY", depends_on=["PLAN"], criteria=False)
    fx = _exp(
        "FX",
        depends_on=["SX"],
        foreach=ForEachSpec(kind="hypothesis", of_task="SX", selector="selected"),
        criteria=False,
    )
    fy = _exp(
        "FY",
        depends_on=["SY"],
        foreach=ForEachSpec(kind="hypothesis", of_task="SY", selector="selected"),
        criteria=False,
    )
    summary = TaskSpec(
        task_key="SUM",
        title="sum",
        goal="g",
        route="experiment",
        depends_on=["FX", "FY"],
        addresses=["Q1"],
        criteria=[RequirementRef(template_id="artifact_present")],
        tolerate_failed_dep=True,
        inputs=[
            InputSpec(from_task="FX", output="main", quorum=2),
            InputSpec(from_task="FY", output="main", quorum=2),
        ],
    )
    await rt.accept_plan(
        [src_x, src_y, fx, fy, summary],
        questions=[QuestionSpec(id="Q1", text="why")],
    )
    await _upsert_selected_hypotheses(rt, of_task="SX", n=3, ids=["HX0", "HX1", "HX2"])
    await _upsert_selected_hypotheses(rt, of_task="SY", n=3, ids=["HY0", "HY1", "HY2"])
    for sk in ("SX", "SY"):
        aid = await rt.start_attempt(sk)
        await _ok(rt, arts, aid)
    x_kids = rt._foreach_children["FX"]
    y_kids = rt._foreach_children["FY"]
    assert len(x_kids) == 3 and len(y_kids) == 3
    # Satisfy X fully; leave Y with 0 successes.
    for k in x_kids:
        aid = await rt.start_attempt(k)
        await _ok(rt, arts, aid)
    for k in y_kids:
        aid = await rt.start_attempt(k)
        await rt.fail_attempt(aid, retryable=False, close_reason="failed")
        t = rt.tasks[task_id(sid, k)]
        if t.state != TaskState.DONE:
            await rt.cancel_task(t.task_id, reason="failed")
    with pytest.raises(InputUnavailable):
        await rt.start_attempt("SUM")
    sum_t = rt.tasks[task_id(sid, "SUM")]
    assert sum_t.state == TaskState.DONE
    assert sum_t.reason == TaskReason.INPUT_UNAVAILABLE
    await _finalize_study(rt)
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
        criteria=[RequirementRef(template_id="artifact_present")],
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
    """Empty foreach + fan-in → SKIPPED; never starts with silent drop / fake real."""
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
        criteria=[RequirementRef(template_id="artifact_present")],
        inputs=[InputSpec(from_task="FE", output="main", quorum=1)],
    )
    await rt.accept_plan([e0, fe, summary], questions=[QuestionSpec(id="Q1", text="why")])
    aid = await rt.start_attempt("E0")
    await _ok(rt, arts, aid)
    assert rt._foreach_children.get("FE") == []
    with pytest.raises(InputUnavailable):
        await rt.start_attempt("SUM")
    sum_t = rt.tasks[task_id(sid, "SUM")]
    assert sum_t.state == TaskState.DONE
    assert sum_t.reason == TaskReason.INPUT_UNAVAILABLE
    fin = await _finalize_study(rt)
    contrib = fin.payload.get("contributing_reasons") or []
    assert "не выполнено: foreach пуст" in contrib
    assert "не выполнено: кворум входов" in contrib or "не выполнено: foreach пуст" in contrib
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_foreach_refuse_skips_template(tmp_path, clock, runtime_cls, monkeypatch):
    """N1: ExpansionRefused → template TaskDone SKIPPED (never left PENDING)."""
    from coscientist.core.plan import foreach as foreach_mod

    sid = "st_fe_ref"
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
    await rt.accept_plan([e0, fe], questions=[QuestionSpec(id="Q1", text="why")])
    await _upsert_selected_hypotheses(rt, of_task="E0", n=2)

    def _boom(_runtime, _prototypes):
        return "forced_key_fail"

    monkeypatch.setattr(foreach_mod, "assign_core_keys", _boom)
    aid = await rt.start_attempt("E0")
    await _ok(rt, arts, aid)
    fe_t = rt.tasks[task_id(sid, "FE")]
    assert fe_t.state == TaskState.DONE
    assert fe_t.outcome == TaskOutcome.SKIPPED
    assert fe_t.reason == TaskReason.REFUSED
    events = await rt.store.read_all()
    assert any(e.type == "ExpansionRefused" for e in events)
    assert "FE" not in (rt._foreach_children or {})
    with pytest.raises(RuntimeError, match="foreach template"):
        await rt.start_attempt("FE")
    await _finalize_study(rt)
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_foreach_template_cannot_be_started(tmp_path, clock, runtime_cls):
    """Direct start_attempt on a foreach template raises."""
    sid = "st_fe_nostart"
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
    attach_executor(rt, tmp_path)
    await _boot_stages(rt)
    e0 = _exp("E0", depends_on=["PLAN"], criteria=False)
    fe = _exp(
        "FE",
        depends_on=["E0"],
        foreach=ForEachSpec(kind="hypothesis", of_task="E0", selector="selected"),
        criteria=False,
    )
    await rt.accept_plan([e0, fe], questions=[QuestionSpec(id="Q1", text="why")])
    fe_t = rt.tasks[task_id(sid, "FE")]
    assert fe_t.state == TaskState.PENDING
    assert fe_t.spec.foreach is not None
    with pytest.raises(RuntimeError, match="foreach template"):
        await rt.start_attempt("FE")
    await _finalize_study(rt)
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_driver_loop_skips_pending_foreach_template(
    tmp_path, clock, runtime_cls, monkeypatch
):
    """Test harness loop over iter_dispatchable_tasks (no product driver yet)."""
    from coscientist.core.plan import foreach as foreach_mod

    sid = "st_fe_drv"
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
    fe = _exp(
        "FE",
        depends_on=["E0"],
        foreach=ForEachSpec(kind="hypothesis", of_task="E0", selector="selected"),
        criteria=False,
    )
    await rt.accept_plan([e0, fe], questions=[QuestionSpec(id="Q1", text="why")])

    async def _no_expand(_runtime, tmpl):
        return None

    monkeypatch.setattr(foreach_mod, "_materialize_one", _no_expand)
    await _upsert_selected_hypotheses(rt, of_task="E0", n=2)
    # Driver loop: only dispatchable tasks; FE stays PENDING with foreach.
    started: list[str] = []
    for _ in range(8):
        ready = rt.iter_dispatchable_tasks()
        assert all(t.spec.foreach is None for t in ready)
        assert all(t.spec.task_key != "FE" for t in ready)
        if not ready:
            break
        t = ready[0]
        aid = await rt.start_attempt(t.spec.task_key)
        started.append(t.spec.task_key)
        await _ok(rt, arts, aid)
        await rt.tick()
    assert "E0" in started
    assert "FE" not in started
    fe_t = rt.tasks[task_id(sid, "FE")]
    assert fe_t.state == TaskState.PENDING
    assert fe_t.spec.foreach is not None
    await _finalize_study(rt)
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_foreach_refused_in_expand_after_queues_plan_n(
    tmp_path, clock, runtime_cls, monkeypatch
):
    """Documented: TaskDone(refused) on expand_after template still queues PLAN-n."""
    from coscientist.core.plan import foreach as foreach_mod

    sid = "st_fe_ea"
    rt = await open_runtime(tmp_path, sid, clock=clock, owner="t1", runtime_cls=runtime_cls)
    await rt.create_study(
        profile="research",
        request="q",
        catalog_version="0.1.0",
        deadline=clock.now() + timedelta(seconds=400),
        sid=sid,
        finalize_reserve_s=5,
        max_expansions=2,
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
    await rt.accept_plan(
        [e0, fe],
        questions=[QuestionSpec(id="Q1", text="why")],
        expand_after=["FE"],
    )
    await _upsert_selected_hypotheses(rt, of_task="E0", n=2)

    def _boom(_runtime, _prototypes):
        return "forced_key_fail"

    monkeypatch.setattr(foreach_mod, "assign_core_keys", _boom)
    aid = await rt.start_attempt("E0")
    await _ok(rt, arts, aid)
    fe_t = rt.tasks[task_id(sid, "FE")]
    assert fe_t.state == TaskState.DONE
    assert fe_t.reason == TaskReason.REFUSED
    plan_n = [
        t
        for t in rt.tasks.values()
        if t.spec.task_key.startswith("PLAN-") and t.spec.task_key != "PLAN"
    ]
    assert plan_n, "REFUSED on expand_after template must still queue PLAN-n"
    await _finalize_study(rt)
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_foreach_missing_and_non_numeric_rank(tmp_path, clock, runtime_cls):
    """Missing/non-numeric rank sort LAST (cap drops them); note; no SKIP."""
    sid = "st_fe_rank"
    rt = await open_runtime(tmp_path, sid, clock=clock, owner="t1", runtime_cls=runtime_cls)
    await rt.create_study(
        profile="research",
        request="q",
        catalog_version="0.1.0",
        deadline=clock.now() + timedelta(seconds=300),
        sid=sid,
        finalize_reserve_s=5,
        max_expansions=0,
        max_foreach_children=2,  # keep only best ranks
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
    await rt.accept_plan([e0, fe], questions=[QuestionSpec(id="Q1", text="why")])
    now = rt.clock.now()
    specs = [
        ("Hbest", {"rank": 0, "selected": True, "source_task": "E0"}),
        ("Hmiss", {"selected": True, "source_task": "E0"}),  # missing → last
        ("Hbad", {"rank": "x", "selected": True, "source_task": "E0"}),  # non-numeric → last
        ("H2", {"rank": 2, "selected": True, "source_task": "E0"}),
    ]
    for hid, attrs in specs:
        attrs = {**attrs, "statement": "s", "prediction": "p"}
        nid = hypothesis_node_id(sid, hid)
        await rt._append(
            [
                EventEnvelope(
                    study_id=sid,
                    type="GraphNodeUpserted",
                    entity_key=nid,
                    idem_key=make_idem("GraphNodeUpserted", nid, "rank"),
                    payload=GraphNode(
                        node_id=nid,
                        kind="hypothesis",
                        study_id=sid,
                        ref_id=hid,
                        attrs=attrs,
                        created_at=now,
                    ).model_dump(mode="json"),
                    ts=now,
                    schema_version=writer_schema_version('GraphNodeUpserted'),
                )
            ]
        )
    aid = await rt.start_attempt("E0")
    await _ok(rt, arts, aid)
    fe_t = rt.tasks[task_id(sid, "FE")]
    assert fe_t.reason == TaskReason.SUPERSEDED  # not REFUSED for bad rank
    kids = rt._foreach_children["FE"]
    items = [rt.tasks[task_id(sid, k)].spec.foreach_item for k in kids]
    assert set(items) == {"Hbest", "H2"}  # numeric ranks kept; miss/bad dropped
    pe = [e for e in await rt.store.read_all() if e.type == "PlanExpanded"][0]
    assert set(pe.payload.get("foreach_dropped") or []) == {"Hbad", "Hmiss"}
    notes = pe.payload.get("foreach_rank_notes") or []
    assert any("нечисловой rank" in str(n) and "Hbad" in str(n) for n in notes)
    fin = await _finalize_study(rt)
    contrib = fin.payload.get("contributing_reasons") or []
    assert any("нечисловой rank" in str(c) for c in contrib)
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_foreach_when_source_done_via_expand_plan(tmp_path, clock, runtime_cls):
    """B1: foreach added via expand_plan expands when of_task already DONE."""
    sid = "st_fe_exp"
    rt = await open_runtime(tmp_path, sid, clock=clock, owner="t1", runtime_cls=runtime_cls)
    await rt.create_study(
        profile="research",
        request="q",
        catalog_version="0.1.0",
        deadline=clock.now() + timedelta(seconds=400),
        sid=sid,
        finalize_reserve_s=5,
        max_expansions=2,
    )
    _, arts = attach_executor(rt, tmp_path)
    await _boot_stages(rt)
    e0 = _exp("E0", depends_on=["PLAN"], criteria=False)
    await rt.accept_plan([e0], questions=[QuestionSpec(id="Q1", text="why")])
    await _upsert_selected_hypotheses(rt, of_task="E0", n=2)
    aid = await rt.start_attempt("E0")
    await _ok(rt, arts, aid)
    assert rt.tasks[task_id(sid, "E0")].state == TaskState.DONE
    fe = _exp(
        "FE",
        depends_on=["E0"],
        foreach=ForEachSpec(kind="hypothesis", of_task="E0", selector="selected"),
        criteria=False,
    )
    pid = await rt.expand_plan(
        after_task="E0",
        added_tasks=[fe],
        origin="emergent",
        reason="add_foreach",
        assign_keys=False,
    )
    assert pid is not None
    children = list(rt._foreach_children.get("FE") or [])
    assert len(children) == 2
    assert rt.tasks[task_id(sid, "FE")].reason == TaskReason.SUPERSEDED
    await close_runtime(rt)
    rt2 = await open_runtime(tmp_path, sid, clock=clock, owner="t2", runtime_cls=runtime_cls)
    assert list(rt2._foreach_children.get("FE") or []) == children
    await _finalize_study(rt2)
    await close_runtime(rt2)


def test_quorum_rejects_zero():
    from coscientist.core.execution.quorum import validate_quorum_specs

    with pytest.raises(ValueError, match="quorum"):
        validate_quorum_specs(
            [
                TaskSpec(
                    task_key="S",
                    title="s",
                    goal="g",
                    route="experiment",
                    inputs=[InputSpec(from_task="A", output="main", quorum=0)],
                )
            ]
        )


def test_quorum_rejects_gt_static_pool():
    from coscientist.core.execution.quorum import validate_quorum_specs

    with pytest.raises(ValueError, match="pool size"):
        validate_quorum_specs(
            [
                TaskSpec(
                    task_key="S",
                    title="s",
                    goal="g",
                    route="experiment",
                    inputs=[
                        InputSpec(from_task="A", output="main", quorum=3),
                        InputSpec(from_task="B", output="main", quorum=3),
                    ],
                )
            ]
        )


def test_quorum_rejects_mix_all_and_int():
    """M-A: static pool cannot mix 'all' and int (order-independent reject)."""
    from coscientist.core.execution.quorum import validate_quorum_specs

    with pytest.raises(ValueError, match="mix"):
        validate_quorum_specs(
            [
                TaskSpec(
                    task_key="S",
                    title="s",
                    goal="g",
                    route="experiment",
                    inputs=[
                        InputSpec(from_task="A", output="main", quorum="all"),
                        InputSpec(from_task="B", output="main", quorum=1),
                    ],
                )
            ]
        )


@pytest.mark.asyncio
async def test_foreach_quorum_gt_children_misses(tmp_path, clock, runtime_cls):
    """Foreach pool: quorum > children → SKIPPED + explicit report line."""
    sid = "st_q_gt"
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
        criteria=[RequirementRef(template_id="artifact_present")],
        tolerate_failed_dep=True,
        inputs=[InputSpec(from_task="FE", output="main", quorum=3)],
    )
    await rt.accept_plan([e0, fe, summary], questions=[QuestionSpec(id="Q1", text="why")])
    await _upsert_selected_hypotheses(rt, of_task="E0", n=2)  # only 2 children
    aid = await rt.start_attempt("E0")
    await _ok(rt, arts, aid)
    kids = rt._foreach_children["FE"]
    assert len(kids) == 2
    for k in kids:
        aid = await rt.start_attempt(k)
        await _ok(rt, arts, aid)
    with pytest.raises(InputUnavailable):
        await rt.start_attempt("SUM")
    assert rt.tasks[task_id(sid, "SUM")].reason == TaskReason.INPUT_UNAVAILABLE
    fin = await _finalize_study(rt)
    contrib = fin.payload.get("contributing_reasons") or []
    assert "не выполнено: кворум > числа детей" in contrib
    assert "не выполнено: кворум входов" not in contrib
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_foreach_empty_superseded_expand_after_queues_plan_n(
    tmp_path, clock, runtime_cls
):
    """Empty foreach SUPERSEDED counts as done for expand_after (like REFUSED)."""
    sid = "st_fe_ea_empty"
    rt = await open_runtime(tmp_path, sid, clock=clock, owner="t1", runtime_cls=runtime_cls)
    await rt.create_study(
        profile="research",
        request="q",
        catalog_version="0.1.0",
        deadline=clock.now() + timedelta(seconds=400),
        sid=sid,
        finalize_reserve_s=5,
        max_expansions=2,
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
    await rt.accept_plan(
        [e0, fe],
        questions=[QuestionSpec(id="Q1", text="why")],
        expand_after=["FE"],
    )
    # No hypotheses → empty foreach → SUPERSEDED
    aid = await rt.start_attempt("E0")
    await _ok(rt, arts, aid)
    fe_t = rt.tasks[task_id(sid, "FE")]
    assert fe_t.reason == TaskReason.SUPERSEDED
    assert rt._foreach_children.get("FE") == []
    plan_n = [
        t
        for t in rt.tasks.values()
        if t.spec.task_key.startswith("PLAN-") and t.spec.task_key != "PLAN"
    ]
    assert plan_n, "empty foreach SUPERSEDED on expand_after must queue PLAN-n"
    await _finalize_study(rt)
    await close_runtime(rt)




