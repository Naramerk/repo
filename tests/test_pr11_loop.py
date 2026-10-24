"""M1 PR11: LoopSpec (scope, 3 predicates, mandatory max_iter, LoopCapped) / S10."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest

from coscientist.core.claims.graph import GraphNode
from coscientist.core.claims.reasons import Disposition, TaskReason, TerminalReason
from coscientist.core.events.events import PLAN_ACCEPTED_SCHEMA_VERSION
from coscientist.core.events.ids import hypothesis_node_id
from coscientist.core.events.upcasters import (
    UPCASTERS,
    assert_upcaster_chain_continuous,
    register_upcaster,
    upcast,
    writer_schema_version,
)
from coscientist.core.execution.artifacts import make_tool_ref
from coscientist.core.execution.domain import (
    AgentTaskOutput,
    ArtifactHandle,
    LoopPredicate,
    LoopSpec,
    QuestionSpec,
    RequirementRef,
    Task,
    TaskKind,
    TaskSpec,
    TaskState,
)
from coscientist.core.execution.recover import recover
from coscientist.core.plan.loop import loop_is_complete, loop_status
from coscientist.core.plan.loop_predicates import (
    _claims_for_loop_scope,
    count_survivors,
    eval_loop_predicate,
    register_loop_predicate,
    scope_fingerprint,
    validate_loop_predicate,
)
from tests.helpers import NEED_OFF, NEED_UNRECORDED, attach_executor, close_runtime, open_runtime


def _body(key: str = "BODY", *, depends_on: list[str] | None = None) -> list[TaskSpec]:
    return [
        TaskSpec(
            task_key=key,
            title=key,
            goal="loop body",
            route="experiment",
            depends_on=list(depends_on or []),
            addresses=["Q1"],
            criteria=[RequirementRef(template_id="value_match")],
        )
    ]


def _loop(
    *,
    loop_id: str = "L1",
    max_iter: int = 3,
    until: LoopPredicate | None = None,
    scope_hypotheses: list[str] | None = None,
    scope_questions: list[str] | None = None,
    depends_on: list[str] | None = None,
    body: list[TaskSpec] | None = None,
) -> LoopSpec:
    return LoopSpec(
        loop_id=loop_id,
        body=body or _body(),
        until=until or LoopPredicate(name="survivors_le", params={"n": 0}),
        max_iter=max_iter,
        scope_hypotheses=list(scope_hypotheses if scope_hypotheses is not None else []),
        scope_questions=list(scope_questions if scope_questions is not None else ["Q1"]),
        depends_on=list(depends_on or []),
    )


def current_loop_iter_tasks(rt, loop_id: str, iter_n: int):
    return [
        t
        for t in rt.tasks.values()
        if t.spec.loop_id == loop_id and t.spec.loop_iter == iter_n
    ]


async def _ok(rt, arts, aid: str, *, count: int = 1) -> None:
    """Record experiment output. ``count=225`` matches catalog ``value_match`` target."""
    content = f'{{"count": {int(count)}}}'.encode()
    art = arts.put(
        content, make_tool_ref(f"{aid}:out"), name="main", media_type="application/json"
    )
    await rt.executor.record(
        aid,
        AgentTaskOutput(
            summary="ok",
            outputs={"main": ArtifactHandle(artifact_id=art.artifact_id, name="main")},
            values={"count": int(count)},
        ),
    )


def _upsert_hyps(rt, *hids: str) -> None:
    sid = rt.study.study_id
    for hid in hids:
        nid = hypothesis_node_id(sid, hid)
        if rt.graph.get_node(nid) is not None:
            continue
        rt.graph.upsert_node(
            GraphNode(
                node_id=nid,
                kind="hypothesis",
                study_id=sid,
                ref_id=hid,
                attrs={"text": hid},
            )
        )


async def _complete_stages(rt, arts) -> None:
    """Drive SPEC/HYP/LIT to SUCCESS (no cancel) so accept_plan can proceed."""
    for key in ("SPEC", "HYP", "LIT"):
        t = next(x for x in rt.tasks.values() if x.spec.task_key == key)
        if t.state == TaskState.DONE:
            continue
        aid = await rt.start_attempt(key)
        await _ok(rt, arts, aid)


async def _open_study(
    tmp_path,
    clock,
    sid: str,
    *,
    max_loop_iter: int = 8,
    max_expansions: int = 0,
    runtime_cls=None,
):
    kw: dict[str, Any] = {"clock": clock, "owner": "t1"}
    if runtime_cls is not None:
        kw["runtime_cls"] = runtime_cls
    rt = await open_runtime(tmp_path, sid, **kw)
    await rt.create_study(
        profile="research",
        request="q",
        catalog_version=3,
        deadline=clock.now() + timedelta(seconds=400),
        sid=sid,
        finalize_reserve_s=5,
        max_expansions=max_expansions,
        max_loop_iter=max_loop_iter,
        require_hypotheses=False,
    )
    arts = attach_executor(rt, tmp_path)[1]
    await _complete_stages(rt, arts)
    return rt, arts


async def _drive_to_finalized(rt, arts) -> Any:
    """Complete every dispatchable task; study finalizes on its own via tick."""
    for _ in range(64):
        if getattr(rt, "_finalized", False):
            break
        pending = [
            t
            for t in rt.tasks.values()
            if t.state == TaskState.PENDING and t.spec.foreach is None
        ]
        progressed = False
        for t in pending:
            if not rt._deps_done(t):  # noqa: SLF001
                continue
            aid = await rt.start_attempt(t.spec.task_key)
            if aid is None:
                progressed = True
                continue
            await _ok(rt, arts, aid)
            progressed = True
        await rt.tick()
        if getattr(rt, "_finalized", False):
            break
        if not progressed:
            # Orphan barriers / stuck deps — one more tick then stop.
            await rt.tick()
            break
    assert rt._finalized is True, "study must finalize without manual cancel"
    events = await rt.store.read_all()
    fins = [e for e in events if e.type == "StudyFinalized"]
    assert len(fins) == 1
    fin = fins[0]
    has_art = fin.payload.get("report_artifact_id") is not None
    has_min = fin.payload.get("minimal_report") is not None
    assert has_art ^ has_min, "report_artifact_id XOR minimal_report"
    cids = fin.payload.get("conclusion_ids")
    assert isinstance(cids, list) and len(cids) > 0, "conclusion_ids must be non-empty"
    assert fin.payload.get("terminal_reason") is not None
    return fin


def _assert_internal_error_finalized(fins: list[Any]) -> Any:
    """Exactly one StudyFinalized(internal_error) with report XOR + conclusion_ids."""
    from coscientist.core.claims.reasons import TerminalReason

    assert len(fins) == 1
    fin = fins[0]
    assert fin.payload.get("terminal_reason") == TerminalReason.INTERNAL_ERROR.value
    has_art = fin.payload.get("report_artifact_id") is not None
    has_min = fin.payload.get("minimal_report") is not None
    assert has_art ^ has_min, "report_artifact_id XOR minimal_report"
    cids = fin.payload.get("conclusion_ids")
    assert isinstance(cids, list) and len(cids) > 0, "conclusion_ids must be non-empty"
    return fin


async def _assert_plan_refused(rt, *, match: str) -> None:
    from coscientist.core.claims.reasons import TerminalReason

    events = await rt.store.read_all()
    refused = [e for e in events if e.type == "PlanRefused"]
    assert len(refused) == 1, "invalid LoopSpec must emit PlanRefused"
    assert match.lower() in str(refused[0].payload.get("detail") or "").lower()
    fins = [e for e in events if e.type == "StudyFinalized"]
    assert len(fins) == 1, "PlanRefused must finalize immediately"
    assert rt._finalized is True
    fin = fins[0]
    assert fin.payload.get("terminal_reason") == TerminalReason.REFUSED.value
    has_art = fin.payload.get("report_artifact_id") is not None
    has_min = fin.payload.get("minimal_report") is not None
    assert has_art ^ has_min, "report_artifact_id XOR minimal_report"
    cids = fin.payload.get("conclusion_ids")
    assert isinstance(cids, list) and len(cids) > 0, "conclusion_ids must be non-empty"
    report_line = str(refused[0].payload.get("report_line") or "")
    contrib = list(fin.payload.get("contributing_reasons") or [])
    assert report_line, "PlanRefused must carry report_line"
    assert report_line in contrib or any(
        "план отклонён" in str(c) for c in contrib
    ), f"report_line missing from contributing_reasons: {contrib!r}"


@pytest.mark.asyncio
async def test_loop_max_iter_missing_honest_refusal(tmp_path, clock, runtime_cls):
    """max_iter missing → PlanRefused + StudyFinalized (not raw ValueError)."""
    rt, _arts = await _open_study(
        tmp_path, clock, "st_loop_no_max", runtime_cls=runtime_cls
    )
    pid = await rt.accept_plan(
        [
            TaskSpec(
                task_key="E0",
                title="e",
                goal="g",
                route="experiment",
                depends_on=["PLAN"],
                addresses=["Q1"],
                criteria=[RequirementRef(template_id="value_match")],
            )
        ],
        questions=[QuestionSpec(id="Q1", text="q")],
        loops=[
            {
                "loop_id": "L1",
                "body": [
                    {
                        "task_key": "BODY",
                        "title": "b",
                        "goal": "g",
                        "route": "experiment",
                        "depends_on": [],
                        "addresses": ["Q1"],
                        "criteria": [{"template_id": "value_match"}],
                    }
                ],
                "until": {"name": "survivors_le", "params": {"n": 0}},
                "scope_hypotheses": [],
                "scope_questions": ["Q1"],
            }
        ],
     hypotheses=NEED_OFF, literature=NEED_OFF)
    assert pid == ""
    await _assert_plan_refused(rt, match="max_iter")
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_loop_max_iter_exceeds_profile_rejected(tmp_path, clock, runtime_cls):
    rt, _arts = await _open_study(
        tmp_path, clock, "st_loop_cap_prof", max_loop_iter=3, runtime_cls=runtime_cls
    )
    pid = await rt.accept_plan(
        [
            TaskSpec(
                task_key="POST",
                title="post",
                goal="g",
                route="experiment",
                depends_on=["loop:L1"],
                addresses=["Q1"],
                criteria=[RequirementRef(template_id="value_match")],
                required=False,
            )
        ],
        questions=[QuestionSpec(id="Q1", text="q")],
        loops=[_loop(max_iter=9)],
     hypotheses=NEED_OFF, literature=NEED_OFF)
    assert pid == ""
    await _assert_plan_refused(rt, match="max_loop_iter")
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_loop_unknown_predicate_rejected(tmp_path, clock, runtime_cls):
    rt, _arts = await _open_study(
        tmp_path, clock, "st_loop_unk", runtime_cls=runtime_cls
    )
    pid = await rt.accept_plan(
        [],
        questions=[QuestionSpec(id="Q1", text="q")],
        loops=[_loop(until=LoopPredicate(name="not_a_real_pred", params={}))],
     hypotheses=NEED_OFF, literature=NEED_OFF)
    assert pid == ""
    await _assert_plan_refused(rt, match="unknown loop predicate")
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_loop_unknown_scope_question_refused(tmp_path, clock, runtime_cls):
    """scope_questions unknown id → PlanRefused even when plan questions non-empty."""
    rt, _arts = await _open_study(
        tmp_path, clock, "st_loop_bad_q", runtime_cls=runtime_cls
    )
    pid = await rt.accept_plan(
        [],
        questions=[QuestionSpec(id="Q1", text="q")],
        loops=[_loop(scope_questions=["Q_MISSING"])],
     hypotheses=NEED_OFF, literature=NEED_OFF)
    assert pid == ""
    await _assert_plan_refused(rt, match="scope_questions")
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_loop_unknown_scope_hypothesis_refused(tmp_path, clock, runtime_cls):
    rt, _arts = await _open_study(
        tmp_path, clock, "st_loop_bad_h", runtime_cls=runtime_cls
    )
    pid = await rt.accept_plan(
        [],
        questions=[QuestionSpec(id="Q1", text="q")],
        loops=[_loop(scope_hypotheses=["H_MISSING"])],
     hypotheses=NEED_OFF, literature=NEED_OFF)
    assert pid == ""
    await _assert_plan_refused(rt, match="scope_hypotheses")
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_loop_until_empty_scope_completes(tmp_path, clock, runtime_cls):
    """survivors_le(0) + empty scope → until met after iter 1; LoopCompleted; quota unused."""
    rt, arts = await _open_study(
        tmp_path, clock, "st_loop_surv", runtime_cls=runtime_cls
    )
    await rt.accept_plan(
        [
            TaskSpec(
                task_key="POST",
                title="after loop",
                goal="g",
                route="experiment",
                depends_on=["loop:L1"],
                addresses=["Q1"],
                criteria=[RequirementRef(template_id="value_match")],
                required=False,
            )
        ],
        questions=[QuestionSpec(id="Q1", text="q")],
        loops=[
            _loop(
                max_iter=5,
                until=LoopPredicate(name="survivors_le", params={"n": 0}),
                scope_hypotheses=[],
            )
        ],
     hypotheses=NEED_OFF, literature=NEED_OFF)
    assert getattr(rt, "_agent_expansions", 0) == 0
    assert len(current_loop_iter_tasks(rt, "L1", 1)) == 1

    for t in current_loop_iter_tasks(rt, "L1", 1):
        aid = await rt.start_attempt(t.spec.task_key)
        await _ok(rt, arts, aid)
    assert loop_status(rt, "L1") == "done"
    assert "L1" not in getattr(rt, "_loop_capped", {})
    assert len(current_loop_iter_tasks(rt, "L1", 2)) == 0

    events = await rt.store.read_all()
    completed = [e for e in events if e.type == "LoopCompleted"]
    assert len(completed) == 1
    assert completed[0].payload["loop_id"] == "L1"
    assert completed[0].payload["iter"] == 1
    assert completed[0].payload["reason"] == "until_met"
    assert [e for e in events if e.type == "LoopCapped"] == []
    assert getattr(rt, "_agent_expansions", 0) == 0

    fin = await _drive_to_finalized(rt, arts)
    assert fin.payload["terminal_reason"] == "completed"
    assert fin.payload["report_artifact_id"] is not None
    assert fin.payload.get("minimal_report") is None
    contrib = fin.payload["contributing_reasons"]
    assert "цикл завершён после 1 итераций, осталось 0" in contrib
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_loop_iteration_cap_honest_final(tmp_path, clock, runtime_cls):
    """Unrefuted scope hyp → survivors_le(0) never met → LoopCapped at max_iter."""
    rt, arts = await _open_study(
        tmp_path, clock, "st_loop_cap", runtime_cls=runtime_cls
    )
    _upsert_hyps(rt, "H1")
    await rt.accept_plan(
        [
            TaskSpec(
                task_key="POST",
                title="after",
                goal="g",
                route="experiment",
                depends_on=["loop:L1"],
                addresses=["Q1"],
                criteria=[RequirementRef(template_id="value_match")],
                required=False,
            )
        ],
        questions=[QuestionSpec(id="Q1", text="which hyp")],
        loops=[
            _loop(
                max_iter=2,
                until=LoopPredicate(name="survivors_le", params={"n": 0}),
                scope_hypotheses=["H1"],
                scope_questions=["Q1"],
            )
        ],
     hypotheses=NEED_OFF, literature=NEED_OFF)

    for it in (1, 2):
        tasks = current_loop_iter_tasks(rt, "L1", it)
        assert len(tasks) == 1, f"expected exactly 1 task in iter {it}"
        for t in tasks:
            aid = await rt.start_attempt(t.spec.task_key)
            await _ok(rt, arts, aid)

    assert loop_status(rt, "L1") == "capped"
    capped = getattr(rt, "_loop_capped", {})
    assert "L1" in capped
    assert capped["L1"]["iter"] == 2
    assert current_loop_iter_tasks(rt, "L1", 3) == []

    fin = await _drive_to_finalized(rt, arts)
    events = await rt.store.read_all()
    lc = [e for e in events if e.type == "LoopCapped"]
    assert len(lc) == 1
    assert lc[0].payload["loop_id"] == "L1"
    assert lc[0].payload["iter"] == 2
    assert fin.payload["terminal_reason"] == "completed"
    assert fin.payload["terminal_reason"] != TerminalReason.EXPANSION_LIMIT.value
    contrib = list(fin.payload["contributing_reasons"])
    assert "iteration_cap" in contrib
    assert "цикл остановлен лимитом после 2 итераций, осталось 1" in contrib
    qdisp = fin.payload["question_dispositions"]
    assert qdisp["Q1"]["reason"] == TaskReason.ITERATION_CAP.value
    assert fin.payload["report_artifact_id"] is not None
    assert fin.payload.get("minimal_report") is None
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_loop_infinite_guard_max_iter(tmp_path, clock, runtime_cls):
    """Adversarial: until always false — never creates more than max_iter iters."""
    rt, arts = await _open_study(
        tmp_path, clock, "st_loop_inf", runtime_cls=runtime_cls
    )
    _upsert_hyps(rt, "H1")
    await rt.accept_plan(
        [],
        questions=[QuestionSpec(id="Q1", text="q")],
        loops=[
            _loop(
                max_iter=3,
                until=LoopPredicate(name="survivors_le", params={"n": 0}),
                scope_hypotheses=["H1"],
            )
        ],
     hypotheses=NEED_OFF, literature=NEED_OFF)
    for it in (1, 2, 3):
        tasks = current_loop_iter_tasks(rt, "L1", it)
        assert len(tasks) == 1
        for t in tasks:
            aid = await rt.start_attempt(t.spec.task_key)
            await _ok(rt, arts, aid)
    assert current_loop_iter_tasks(rt, "L1", 4) == []
    assert loop_status(rt, "L1") == "capped"
    await _drive_to_finalized(rt, arts)
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_loop_completed_survives_restart(tmp_path, clock, runtime_cls):
    """Kill after LoopCompleted → reopen restores done; replay == live."""
    rt, arts = await _open_study(
        tmp_path, clock, "st_loop_done_re", runtime_cls=runtime_cls
    )
    await rt.accept_plan(
        [
            TaskSpec(
                task_key="POST",
                title="after",
                goal="g",
                route="experiment",
                depends_on=["loop:L1"],
                addresses=["Q1"],
                criteria=[RequirementRef(template_id="value_match")],
                required=False,
            )
        ],
        questions=[QuestionSpec(id="Q1", text="q")],
        loops=[
            _loop(
                max_iter=5,
                until=LoopPredicate(name="survivors_le", params={"n": 0}),
                scope_hypotheses=[],
            )
        ],
     hypotheses=NEED_OFF, literature=NEED_OFF)
    for t in current_loop_iter_tasks(rt, "L1", 1):
        aid = await rt.start_attempt(t.spec.task_key)
        await _ok(rt, arts, aid)
    assert loop_status(rt, "L1") == "done"
    events = await rt.store.read_all()
    assert sum(1 for e in events if e.type == "LoopCompleted") == 1
    assert sum(1 for e in events if e.type == "LoopCapped") == 0
    live_iter_keys = {t.spec.task_key for t in current_loop_iter_tasks(rt, "L1", 1)}

    await close_runtime(rt)
    rt2 = await open_runtime(
        tmp_path, "st_loop_done_re", clock=clock, owner="t2", runtime_cls=runtime_cls
    )
    attach_executor(rt2, tmp_path)
    await recover(rt2)
    assert loop_status(rt2, "L1") == "done"
    assert "L1" in getattr(rt2, "_loop_done", set())
    assert {t.spec.task_key for t in current_loop_iter_tasks(rt2, "L1", 1)} == live_iter_keys
    events2 = await rt2.store.read_all()
    assert sum(1 for e in events2 if e.type == "LoopCompleted") == 1
    assert sum(1 for e in events2 if e.type == "LoopCapped") == 0
    fin = await _drive_to_finalized(rt2, arts)
    assert fin.payload["terminal_reason"] == "completed"
    await close_runtime(rt2)


@pytest.mark.asyncio
async def test_loop_capped_survives_restart(tmp_path, clock, runtime_cls):
    """After true iteration_cap LoopCapped, reopen restores capped; POST unblocked."""
    rt, arts = await _open_study(
        tmp_path, clock, "st_loop_cap_re", runtime_cls=runtime_cls
    )
    _upsert_hyps(rt, "H1")
    await rt.accept_plan(
        [
            TaskSpec(
                task_key="POST",
                title="after",
                goal="g",
                route="experiment",
                depends_on=["loop:L1"],
                addresses=["Q1"],
                criteria=[RequirementRef(template_id="value_match")],
                required=False,
            )
        ],
        questions=[QuestionSpec(id="Q1", text="q")],
        loops=[
            _loop(
                max_iter=2,
                until=LoopPredicate(name="survivors_le", params={"n": 0}),
                scope_hypotheses=["H1"],
            )
        ],
     hypotheses=NEED_OFF, literature=NEED_OFF)
    for it in (1, 2):
        for t in current_loop_iter_tasks(rt, "L1", it):
            aid = await rt.start_attempt(t.spec.task_key)
            await _ok(rt, arts, aid)
    assert loop_status(rt, "L1") == "capped"
    live_capped = dict(getattr(rt, "_loop_capped", {})["L1"])
    assert live_capped.get("reason") == "iteration_cap"

    await close_runtime(rt)
    rt2 = await open_runtime(
        tmp_path, "st_loop_cap_re", clock=clock, owner="t2", runtime_cls=runtime_cls
    )
    attach_executor(rt2, tmp_path)
    await recover(rt2)
    assert loop_status(rt2, "L1") == "capped"
    assert rt2._loop_capped["L1"]["iter"] == live_capped["iter"]
    assert loop_is_complete(rt2, "L1")
    fin = await _drive_to_finalized(rt2, arts)
    assert fin.payload["terminal_reason"] == "completed"
    await close_runtime(rt2)


@pytest.mark.asyncio
async def test_loop_refuse_error_survives_restart(tmp_path, clock, runtime_cls):
    """LoopRefused+LoopCapped(reason=error) folds on reopen; not iteration_cap."""
    import coscientist.core.plan.loop_predicates as lp

    name = "test_plugin_refuse_restart"
    register_loop_predicate(name, lambda *a, **k: False)
    rt, arts = await _open_study(
        tmp_path, clock, "st_loop_err_re", runtime_cls=runtime_cls
    )
    await rt.accept_plan(
        [
            TaskSpec(
                task_key="POST",
                title="after",
                goal="g",
                route="experiment",
                depends_on=["loop:L1"],
                addresses=["Q1"],
                criteria=[RequirementRef(template_id="value_match")],
                required=False,
            )
        ],
        questions=[QuestionSpec(id="Q1", text="q")],
        loops=[
            _loop(
                max_iter=5,
                until=LoopPredicate(name=name, params={}),
                scope_hypotheses=[],
                scope_questions=["Q1"],
            )
        ],
     hypotheses=NEED_OFF, literature=NEED_OFF)
    for t in current_loop_iter_tasks(rt, "L1", 1):
        aid = await rt.start_attempt(t.spec.task_key)
        await _ok(rt, arts, aid)
    lp._REGISTRY.pop(name, None)
    for t in current_loop_iter_tasks(rt, "L1", 2):
        aid = await rt.start_attempt(t.spec.task_key)
        await _ok(rt, arts, aid)
    assert loop_status(rt, "L1") == "refused"
    events = await rt.store.read_all()
    refused = [e for e in events if e.type == "LoopRefused"]
    assert len(refused) == 1
    assert refused[0].payload["reason"] == "error"
    capped = [e for e in events if e.type == "LoopCapped"]
    assert len(capped) == 1
    assert capped[0].payload["reason"] == "error"
    # Must not stamp iteration_cap on dispositions for error path.
    from coscientist.core.plan.loop import capped_scope_questions

    assert capped_scope_questions(rt) == {}

    await close_runtime(rt)
    rt2 = await open_runtime(
        tmp_path, "st_loop_err_re", clock=clock, owner="t2", runtime_cls=runtime_cls
    )
    attach_executor(rt2, tmp_path)
    await recover(rt2)
    assert loop_status(rt2, "L1") == "refused"
    assert loop_is_complete(rt2, "L1")
    assert current_loop_iter_tasks(rt2, "L1", 3) == []
    fin = await _drive_to_finalized(rt2, arts)
    contrib = list(fin.payload["contributing_reasons"])
    assert "iteration_cap" not in contrib
    assert any("ошибкой" in str(c) for c in contrib)
    await close_runtime(rt2)


@pytest.mark.asyncio
async def test_loop_no_change_stops_on_stable_scope(tmp_path, clock, runtime_cls):
    """no_change: i=1 false; empty scope fingerprints equal after iter 2 → done."""
    rt, arts = await _open_study(
        tmp_path, clock, "st_loop_nc", runtime_cls=runtime_cls
    )
    loop = _loop(
        max_iter=5,
        until=LoopPredicate(name="no_change", params={}),
        scope_hypotheses=[],
        scope_questions=[],
    )
    await rt.accept_plan(
        [],
        questions=[QuestionSpec(id="Q1", text="q")],
        loops=[loop],
     hypotheses=NEED_OFF, literature=NEED_OFF)
    assert len(current_loop_iter_tasks(rt, "L1", 1)) == 1
    for t in current_loop_iter_tasks(rt, "L1", 1):
        aid = await rt.start_attempt(t.spec.task_key)
        await _ok(rt, arts, aid)
    assert len(current_loop_iter_tasks(rt, "L1", 2)) == 1
    for t in current_loop_iter_tasks(rt, "L1", 2):
        aid = await rt.start_attempt(t.spec.task_key)
        await _ok(rt, arts, aid)
    assert loop_status(rt, "L1") == "done"
    assert current_loop_iter_tasks(rt, "L1", 3) == []
    events = await rt.store.read_all()
    assert sum(1 for e in events if e.type == "LoopCompleted") == 1
    await _drive_to_finalized(rt, arts)
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_loop_criterion_met(tmp_path, clock, runtime_cls):
    """criterion_met: value_match MET on loop body → LoopCompleted after iter 1."""
    rt, arts = await _open_study(
        tmp_path, clock, "st_loop_crit", runtime_cls=runtime_cls
    )
    await rt.accept_plan(
        [],
        questions=[QuestionSpec(id="Q1", text="q")],
        loops=[
            _loop(
                max_iter=4,
                until=LoopPredicate(
                    name="criterion_met", params={"criterion_id": "value_match"}
                ),
                scope_hypotheses=[],
                scope_questions=["Q1"],
            )
        ],
     hypotheses=NEED_OFF, literature=NEED_OFF)
    assert len(current_loop_iter_tasks(rt, "L1", 1)) == 1
    for t in current_loop_iter_tasks(rt, "L1", 1):
        aid = await rt.start_attempt(t.spec.task_key)
        await _ok(rt, arts, aid, count=225)  # catalog value_match target
    assert loop_status(rt, "L1") == "done"
    events = await rt.store.read_all()
    assert sum(1 for e in events if e.type == "LoopCompleted") == 1
    assert current_loop_iter_tasks(rt, "L1", 2) == []
    await _drive_to_finalized(rt, arts)
    await close_runtime(rt)


def test_pred_criterion_met_reads_check_results():
    """Unit: criterion_met true only when loop task check_results status=MET."""
    from datetime import datetime, timezone

    from coscientist.core.execution.domain import ReqStatus
    from coscientist.core.plan.loop_predicates import _pred_criterion_met

    class RT:
        def __init__(self):
            self.tasks = {
                "t1": Task(
                    task_id="t1",
                    kind=TaskKind.EXPERIMENT,
                    spec=TaskSpec(
                        task_key="EXP-0",
                        title="t",
                        goal="g",
                        route="experiment",
                        loop_id="L1",
                        loop_iter=1,
                    ),
                    state=TaskState.DONE,
                    deadline=datetime.now(timezone.utc),
                )
            }
            self.attempts = {"a1": {"task_id": "t1"}}
            self.results = {
                "a1": {
                    "check_results": [
                        {
                            "template_id": "value_match",
                            "status": ReqStatus.MET.value,
                        }
                    ]
                }
            }

    loop = _loop(
        until=LoopPredicate(
            name="criterion_met", params={"criterion_id": "value_match"}
        ),
        scope_hypotheses=[],
        scope_questions=[],
    )
    assert _pred_criterion_met(RT(), loop, iter_n=1, ctx={}) is True
    rt2 = RT()
    rt2.results["a1"]["check_results"][0]["status"] = ReqStatus.FAILED.value
    assert _pred_criterion_met(rt2, loop, iter_n=1, ctx={}) is False


@pytest.mark.asyncio
async def test_loop_restart_idempotent(tmp_path, clock, runtime_cls):
    rt, arts = await _open_study(
        tmp_path, clock, "st_loop_re", runtime_cls=runtime_cls
    )
    _upsert_hyps(rt, "H1")
    await rt.accept_plan(
        [],
        questions=[QuestionSpec(id="Q1", text="q")],
        loops=[
            _loop(
                max_iter=2,
                until=LoopPredicate(name="survivors_le", params={"n": 0}),
                scope_hypotheses=["H1"],
            )
        ],
     hypotheses=NEED_OFF, literature=NEED_OFF)
    for t in current_loop_iter_tasks(rt, "L1", 1):
        aid = await rt.start_attempt(t.spec.task_key)
        await _ok(rt, arts, aid)
    keys_before = {t.spec.task_key for t in current_loop_iter_tasks(rt, "L1", 2)}
    assert keys_before

    await close_runtime(rt)
    rt2 = await open_runtime(
        tmp_path, "st_loop_re", clock=clock, owner="t2", runtime_cls=runtime_cls
    )
    attach_executor(rt2, tmp_path)
    await recover(rt2)
    keys_after = {t.spec.task_key for t in current_loop_iter_tasks(rt2, "L1", 2)}
    assert keys_before == keys_after
    for t in current_loop_iter_tasks(rt2, "L1", 2):
        if t.state != TaskState.DONE:
            aid = await rt2.start_attempt(t.spec.task_key)
            await _ok(rt2, arts, aid)
    events = await rt2.store.read_all()
    assert sum(1 for e in events if e.type == "LoopCapped") == 1
    await _drive_to_finalized(rt2, arts)
    await close_runtime(rt2)


def _seed_refute_claim_on_graph(rt, *, hid: str, exp_key: str, claim_suffix: str) -> None:
    """Upsert a countable refute claim on the real graph (no monkeypatch)."""
    from datetime import datetime, timezone

    from coscientist.core.claims.graph import GraphEdge, GraphNode
    from coscientist.core.events.ids import claim_node_id, experiment_node_id, hypothesis_node_id

    sid = rt.study.study_id
    now = datetime.now(timezone.utc)
    hid_n = hypothesis_node_id(sid, hid)
    if rt.graph.get_node(hid_n) is None:
        rt.graph.upsert_node(
            GraphNode(
                node_id=hid_n,
                kind="hypothesis",
                study_id=sid,
                ref_id=hid,
                attrs={"text": hid},
            )
        )
    eid = experiment_node_id(sid, exp_key)
    if rt.graph.get_node(eid) is None:
        rt.graph.upsert_node(
            GraphNode(
                node_id=eid,
                kind="experiment",
                study_id=sid,
                ref_id=exp_key,
                attrs={"task_key": exp_key},
            )
        )
        rt.graph.add_edge(
            GraphEdge(
                edge_id=f"tests:{eid}:{hid_n}",
                kind="tests",
                study_id=sid,
                src_id=eid,
                dst_id=hid_n,
                attrs={"intent": "refute"},
                created_at=now,
            )
        )
    cid = claim_node_id(sid, f"{sid}/{exp_key}#{claim_suffix}")
    aid = f"{sid}/{exp_key}#1"
    rt.graph.upsert_node(
        GraphNode(
            node_id=cid,
            kind="claim",
            study_id=sid,
            ref_id=cid,
            attrs={
                "provenance": "real",
                "attempt_id": aid,
                "attempt_outcome": "SUCCESS",
                "experiment_id": exp_key,
                "target": hid,
                "criterion_status": "not_met",
                "claim_outcome": "inconsistent",
                "accepted": True,
                "frozen": True,
                "evidence_kind": "computational",
                "validity_ok": True,
                "check_role": "prediction",
                "severe": True,
                "refute_capable": True,
                "independence_key": exp_key,
                "effect": {
                    "est": 1.0,
                    "ci_low": 1.0,
                    "ci_high": 1.0,
                    "producer": "agent",
                    "trusted": False,
                    "core_evaluated": True,
                    "countable": True,
                    "produced_by_call_id": f"{aid}:1",
                },
            },
        )
    )
    rt.graph.add_edge(
        GraphEdge(
            edge_id=f"about:{cid}:{hid_n}",
            kind="about",
            study_id=sid,
            src_id=cid,
            dst_id=hid_n,
            created_at=now,
        )
    )
    rt.results[aid] = {
        "outcome": "SUCCESS",
        "task_id": f"{sid}/{exp_key}",
        "check_results": [
            {
                "template_id": "value_match",
                "status": "FAILED",
                "role": "prediction",
            }
        ],
    }
    rt.attempts[aid] = {"task_id": f"{sid}/{exp_key}", "attempt_id": aid}


def test_survivors_le_real_graph_scope_filter():
    """survivors_le via real GraphStore claims; outside-loop claims ignored."""
    from datetime import datetime, timezone

    from coscientist.core.claims.graph import GraphStore, default_execution_registry
    from coscientist.core.execution.domain import HitlPolicy, Study

    now = datetime.now(timezone.utc)
    loop_key = "EXP-LOOP"
    outside_key = "EXP-OUT"
    sid = "st_surv_graph"

    class RT:
        def __init__(self):
            self.study = Study(
                study_id=sid,
                profile="research",
                request="q",
                catalog_version=3,
                hitl_policy=HitlPolicy(),
                deadline=now,
                created_at=now,
                refutations_needed=1,
                refutation_priority=True,
                confirmations_needed=1,
                supported_needed=2,
                require_hypotheses=False,
            )
            self.graph = GraphStore(default_execution_registry())
            self.artifacts = None
            self.results = {}
            self.attempts = {}
            self.tasks = {
                "t_loop": Task(
                    task_id=f"{sid}/{loop_key}",
                    kind=TaskKind.EXPERIMENT,
                    spec=TaskSpec(
                        task_key=loop_key,
                        title="loop",
                        goal="g",
                        route="experiment",
                        loop_id="L1",
                        loop_iter=1,
                    ),
                    state=TaskState.DONE,
                    deadline=now,
                ),
                "t_out": Task(
                    task_id=f"{sid}/{outside_key}",
                    kind=TaskKind.EXPERIMENT,
                    spec=TaskSpec(
                        task_key=outside_key,
                        title="out",
                        goal="g",
                        route="experiment",
                    ),
                    state=TaskState.DONE,
                    deadline=now,
                ),
            }

    rt = RT()
    _seed_refute_claim_on_graph(rt, hid="H1", exp_key=loop_key, claim_suffix="c1")
    _seed_refute_claim_on_graph(rt, hid="H3", exp_key=outside_key, claim_suffix="c3")
    # H2: hypothesis node only — unrefuted survivor.
    from coscientist.core.claims.graph import GraphNode
    from coscientist.core.events.ids import hypothesis_node_id

    rt.graph.upsert_node(
        GraphNode(
            node_id=hypothesis_node_id(sid, "H2"),
            kind="hypothesis",
            study_id=sid,
            ref_id="H2",
            attrs={"text": "H2"},
        )
    )
    loop = _loop(
        until=LoopPredicate(name="survivors_le", params={"n": 1}),
        scope_hypotheses=["H1", "H2", "H3"],
        scope_questions=[],
    )
    scoped = _claims_for_loop_scope(rt, loop, upto_iter=1)
    assert [c.experiment_id for c in scoped["H1"]] == [loop_key]
    assert scoped["H2"] == []
    assert scoped["H3"] == []  # outside-loop refute excluded
    n = count_survivors(rt, loop, upto_iter=1)
    # H1 refuted (loop), H2 + H3 survivors under loop scope → 2
    assert n == 2
    assert eval_loop_predicate(rt, loop, iter_n=1) is False
    loop_le2 = loop.model_copy(
        update={"until": LoopPredicate(name="survivors_le", params={"n": 2})}
    )
    assert eval_loop_predicate(rt, loop_le2, iter_n=1) is True


@pytest.mark.asyncio
async def test_survivors_le_e2e_real_graph_runtime(tmp_path, clock, runtime_cls):
    """E2E: survivors_le on live runtime graph claims (no filtered_claims monkeypatch)."""
    rt, arts = await _open_study(
        tmp_path, clock, "st_surv_e2e", runtime_cls=runtime_cls
    )
    _upsert_hyps(rt, "H1", "H2")
    await rt.accept_plan(
        [],
        questions=[QuestionSpec(id="Q1", text="q")],
        loops=[
            _loop(
                max_iter=3,
                until=LoopPredicate(name="survivors_le", params={"n": 0}),
                scope_hypotheses=["H1", "H2"],
                scope_questions=[],
            )
        ],
     hypotheses=NEED_OFF, literature=NEED_OFF)
    body = current_loop_iter_tasks(rt, "L1", 1)
    assert len(body) == 1
    exp_key = body[0].spec.task_key
    aid = await rt.start_attempt(exp_key)
    await _ok(rt, arts, aid)
    # After iter 1 without claims: 2 survivors → continue.
    assert loop_status(rt, "L1") == "running"
    assert len(current_loop_iter_tasks(rt, "L1", 2)) == 1
    # Seed refute claims for both hyps from the loop body task; until should fire.
    _seed_refute_claim_on_graph(rt, hid="H1", exp_key=exp_key, claim_suffix="e2e1")
    _seed_refute_claim_on_graph(rt, hid="H2", exp_key=exp_key, claim_suffix="e2e2")
    assert count_survivors(
        rt,
        _loop(
            until=LoopPredicate(name="survivors_le", params={"n": 0}),
            scope_hypotheses=["H1", "H2"],
            scope_questions=[],
        ).model_copy(update={"loop_id": "L1"}),
        upto_iter=1,
    ) == 0
    # Drive iter 2 completion → until met via real graph claims.
    for t in current_loop_iter_tasks(rt, "L1", 2):
        # Claims already on graph from iter-1 key; also attach iter-2 key claims.
        _seed_refute_claim_on_graph(
            rt, hid="H1", exp_key=t.spec.task_key, claim_suffix="e2e1b"
        )
        _seed_refute_claim_on_graph(
            rt, hid="H2", exp_key=t.spec.task_key, claim_suffix="e2e2b"
        )
        aid2 = await rt.start_attempt(t.spec.task_key)
        await _ok(rt, arts, aid2)
    assert loop_status(rt, "L1") == "done"
    events = await rt.store.read_all()
    assert sum(1 for e in events if e.type == "LoopCompleted") == 1
    assert sum(1 for e in events if e.type == "LoopCapped") == 0
    await _drive_to_finalized(rt, arts)
    await close_runtime(rt)


def test_dispositions_preserve_infeasible_and_outside_answers_under_cap():
    from coscientist.core.claims.dispositions import apply_loop_cap_dispositions, dispositions

    plans = {
        "p1": {
            "questions": [{"id": "Q1", "text": "q1"}, {"id": "Q2", "text": "q2"}],
            "tasks": [],
        }
    }
    out = dispositions(
        tasks={},
        plans=plans,
        loop_capped_questions={
            "Q1": {"loop_id": "L1", "reason": "iteration_cap", "iter": 2},
            "Q2": {"loop_id": "L1", "reason": "iteration_cap", "iter": 2},
        },
    )
    # not_attempted under true cap → inconclusive + iteration_cap.
    assert out["Q1"]["disposition"] == Disposition.INCONCLUSIVE.value
    assert out["Q1"]["reason"] == TaskReason.ITERATION_CAP.value
    assert out["Q2"]["disposition"] == Disposition.INCONCLUSIVE.value

    result = {
        "Q1": {
            "disposition": Disposition.INFEASIBLE.value,
            "reason": "infeasible",
            "formulation": "q1",
        },
        "Q2": {
            "disposition": Disposition.NOT_ATTEMPTED.value,
            "reason": "no_task",
            "formulation": "q2",
        },
        "Q3": {
            "disposition": Disposition.NEEDS_MORE_DATA.value,
            "reason": "needs_more_data",
            "formulation": "q3",
        },
        "Q4": {
            "disposition": Disposition.ANSWERED.value,
            "reason": None,
            "formulation": "q4",
        },
    }
    capped = {
        "Q1": {"loop_id": "L1", "reason": "iteration_cap", "iter": 2},
        "Q2": {"loop_id": "L1", "reason": "iteration_cap", "iter": 2},
        "Q3": {"loop_id": "L1", "reason": "iteration_cap", "iter": 2},
        "Q4": {"loop_id": "L1", "reason": "iteration_cap", "iter": 2},
    }
    apply_loop_cap_dispositions(result, capped)
    assert result["Q1"]["disposition"] == Disposition.INFEASIBLE.value
    assert result["Q1"]["reason"] == "infeasible"
    assert result["Q3"]["disposition"] == Disposition.NEEDS_MORE_DATA.value
    assert result["Q2"]["disposition"] == Disposition.INCONCLUSIVE.value
    assert result["Q2"]["reason"] == TaskReason.ITERATION_CAP.value
    # Outside-loop / already-answered: keep answer, do not stamp iteration_cap.
    assert result["Q4"]["disposition"] == Disposition.ANSWERED.value
    assert result["Q4"]["reason"] is None
    # Error/refuse / zero-iter must not apply.
    err = {
        "Q2": {
            "disposition": Disposition.NOT_ATTEMPTED.value,
            "reason": "no_task",
            "formulation": "q2",
        }
    }
    apply_loop_cap_dispositions(
        err, {"Q2": {"loop_id": "L1", "reason": "error", "iter": 1}}
    )
    assert err["Q2"]["disposition"] == Disposition.NOT_ATTEMPTED.value
    apply_loop_cap_dispositions(
        err, {"Q2": {"loop_id": "L1", "reason": "iteration_cap", "iter": 0}}
    )
    assert err["Q2"]["disposition"] == Disposition.NOT_ATTEMPTED.value


def test_upcast_study_created_max_loop_iter():
    v, p = upcast(
        "StudyCreated",
        1,
        {
            "study_id": "st_old",
            "profile": "research",
            "request": "q",
            "catalog_version": 3,
            "hitl_policy": {},
            "deadline": "2026-01-01T01:00:00+00:00",
            "created_at": "2026-01-01T00:00:00+00:00", "require_hypotheses": False
        },
    )
    assert v == 14
    # #24 v10 setdefault(max_loop_iter, 8); #23 v11 preserves it; PR20 v12 max_proposals.
    assert p["max_loop_iter"] == 8
    assert p["as_of"] is None
    assert p["user_input_ids"] == []
    assert p["max_proposals"] == 3
    from coscientist.core.events.upcasters import _study_created_v9_to_v10

    assert _study_created_v9_to_v10({})["max_loop_iter"] == 8


def test_upcast_plan_accepted_loops_default():
    v, p = upcast(
        "PlanAccepted", 1,
        {
            "plan_id": "pl1", "study_id": "st", "revision": 1,
            "hypothesis_id": None, "tasks": [],
        },
    )
    assert v == PLAN_ACCEPTED_SCHEMA_VERSION  # G4 Need after PR15 kind v11
    assert p["loops"] == []
    assert p.get("rollup") == "all"
    assert p["hypotheses"] == NEED_UNRECORDED


def test_upcast_task_created_loop_fields():
    v, p = upcast(
        "TaskCreated",
        1,
        {
            "task_id": "st:EXP-0",
            "kind": "experiment",
            "spec": {
                    "sandbox_check_limits": {
                    "exec_ok": {
                        "timeout_s": 5.0,
                        "memory_mb": 64,
                        "max_output_bytes": 1024,
                        "pids_max": 16,
                        "rlimit_nproc": 16,
                        "reap_timeout_s": 1.0,
                        "output_drain_join_s": 1.0,
                        "allow_network": False,
                        "allow_cgroup_degraded": False,
                    }
                },
                "task_key": "EXP-0",
                "title": "t",
                "goal": "g",
                "route": "experiment",
            },
            "state": "PENDING",
            "deadline": "2026-01-01T01:00:00+00:00",
        },
    )
    assert v == 14
    assert p["spec"]["loop_id"] is None
    assert p["spec"]["loop_iter"] is None


def test_upcaster_duplicate_and_continuous_chains():
    from coscientist.core.events.events import (
        PLAN_ACCEPTED_SCHEMA_VERSION,
        STUDY_CREATED_SCHEMA_VERSION,
        TASK_CREATED_SCHEMA_VERSION,
    )

    with pytest.raises(ValueError, match="duplicate upcaster"):
        register_upcaster("TaskCreated", 1, lambda p: p)
    for etype, writer in (
        ("StudyCreated", STUDY_CREATED_SCHEMA_VERSION),
        ("PlanAccepted", PLAN_ACCEPTED_SCHEMA_VERSION),
        ("TaskCreated", TASK_CREATED_SCHEMA_VERSION),
        ("AttemptStarted", writer_schema_version("AttemptStarted")),
        ("ResultRecorded", writer_schema_version("ResultRecorded")),
        ("StudyFinalized", writer_schema_version("StudyFinalized")),
    ):
        assert_upcaster_chain_continuous(etype, writer)
        assert writer_schema_version(etype) == writer
    # Writer constants must match the end of each upcaster chain.
    assert writer_schema_version("StudyCreated") == STUDY_CREATED_SCHEMA_VERSION
    assert writer_schema_version("PlanAccepted") == PLAN_ACCEPTED_SCHEMA_VERSION
    assert writer_schema_version("TaskCreated") == TASK_CREATED_SCHEMA_VERSION
    # Every registered chain is continuous 1..N-1 for its max.
    for etype, bucket in UPCASTERS.items():
        n = writer_schema_version(etype)
        assert_upcaster_chain_continuous(etype, n)
        assert max(bucket) == n - 1


def test_validate_loop_predicate_builtins():
    from coscientist.core.claims.rules import PlanInvalid
    from coscientist.core.plan.loop_predicates import SURVIVORS_LE_N_MAX

    validate_loop_predicate(LoopPredicate(name="survivors_le", params={"n": 1}))
    validate_loop_predicate(
        LoopPredicate(name="criterion_met", params={"criterion_id": "value_match"})
    )
    validate_loop_predicate(LoopPredicate(name="no_change", params={}))
    with pytest.raises(ValueError, match="unknown"):
        validate_loop_predicate(LoopPredicate(name="Nope", params={}))
    with pytest.raises(PlanInvalid, match="int"):
        validate_loop_predicate(LoopPredicate(name="survivors_le", params={"n": 1.5}))
    with pytest.raises(PlanInvalid, match="int"):
        validate_loop_predicate(LoopPredicate(name="survivors_le", params={"n": True}))
    with pytest.raises(PlanInvalid, match="int"):
        validate_loop_predicate(
            LoopPredicate(name="survivors_le", params={"n": float("inf")})
        )
    # Huge int must not OverflowError via float(); PlanInvalid instead.
    with pytest.raises(PlanInvalid, match=str(SURVIVORS_LE_N_MAX)):
        validate_loop_predicate(
            LoopPredicate(name="survivors_le", params={"n": 10**400})
        )


@pytest.mark.asyncio
async def test_survivors_le_huge_n_plan_refused(tmp_path, clock, runtime_cls):
    """accept_plan with n=10**400 → PlanRefused (no OverflowError crash)."""
    rt, _arts = await _open_study(
        tmp_path, clock, "st_loop_huge_n", runtime_cls=runtime_cls
    )
    pid = await rt.accept_plan(
        [],
        questions=[QuestionSpec(id="Q1", text="q")],
        loops=[
            _loop(
                until=LoopPredicate(name="survivors_le", params={"n": 10**400}),
                scope_hypotheses=[],
            )
        ],
     hypotheses=NEED_OFF, literature=NEED_OFF)
    assert pid == ""
    await _assert_plan_refused(rt, match="10000")
    await close_runtime(rt)


def test_builtin_predicate_override_rejected():
    with pytest.raises(ValueError, match="cannot override built-in"):
        register_loop_predicate("survivors_le", lambda *a, **k: True)
    with pytest.raises(ValueError, match="cannot override built-in"):
        register_loop_predicate("criterion_met", lambda *a, **k: True)
    with pytest.raises(ValueError, match="cannot override built-in"):
        register_loop_predicate("no_change", lambda *a, **k: True)


def test_plugin_predicate_override_rejected():
    register_loop_predicate("test_plugin_once_pr11", lambda *a, **k: True)
    with pytest.raises(ValueError, match="cannot override registered"):
        register_loop_predicate("test_plugin_once_pr11", lambda *a, **k: False)


def test_scope_fingerprint_stable_empty():
    class RT:
        tasks = {}
        study = None
        graph = None
        results = {}
        attempts = {}

    loop = _loop(scope_hypotheses=[], scope_questions=[])
    a = scope_fingerprint(RT(), loop, upto_iter=1)
    b = scope_fingerprint(RT(), loop, upto_iter=2)
    assert a == b


def test_plugin_loop_predicate_registration():
    def always_true(runtime, loop, *, iter_n, ctx):
        return True

    register_loop_predicate("test_always_true_pr11b", always_true)
    loop = _loop(until=LoopPredicate(name="test_always_true_pr11b", params={}))
    assert eval_loop_predicate(None, loop, iter_n=1) is True


def test_normalize_loop_id_nfkc_and_invisible():
    from coscientist.core.claims.rules import PlanInvalid
    from coscientist.core.plan.loop import normalize_loop_id, parse_loop_dep

    assert normalize_loop_id("Ｌ１") == "L1"  # fullwidth → NFKC ASCII
    assert normalize_loop_id("Loop_1") == "Loop_1"
    assert parse_loop_dep("loop:Ｌ１") == "L1"
    with pytest.raises(PlanInvalid, match="invisible"):
        normalize_loop_id("L\u200b1")  # zero-width space
    with pytest.raises(PlanInvalid, match="required"):
        normalize_loop_id("   ")


@pytest.mark.asyncio
async def test_fullwidth_loop_dep_normalized_on_accept(tmp_path, clock, runtime_cls):
    """depends_on=['loop:Ｌ１'] stores normalized loop:L1 and barrier works."""
    rt, arts = await _open_study(
        tmp_path, clock, "st_loop_fw", runtime_cls=runtime_cls
    )
    await rt.accept_plan(
        [
            TaskSpec(
                task_key="POST",
                title="after",
                goal="g",
                route="experiment",
                depends_on=["loop:Ｌ１"],
                addresses=["Q1"],
                criteria=[RequirementRef(template_id="value_match")],
                required=False,
            )
        ],
        questions=[QuestionSpec(id="Q1", text="q")],
        loops=[
            _loop(
                loop_id="Ｌ１",
                max_iter=1,
                until=LoopPredicate(name="survivors_le", params={"n": 0}),
                scope_hypotheses=[],
            )
        ],
     hypotheses=NEED_OFF, literature=NEED_OFF)
    post = next(t for t in rt.tasks.values() if t.spec.task_key == "POST")
    assert post.spec.depends_on == ["loop:L1"]
    plans = list(rt.plans.values())
    loops = plans[-1].get("loops") or []
    assert loops and loops[0]["loop_id"] == "L1"
    for t in current_loop_iter_tasks(rt, "L1", 1):
        aid = await rt.start_attempt(t.spec.task_key)
        await _ok(rt, arts, aid)
    assert loop_status(rt, "L1") == "done"
    aid = await rt.start_attempt("POST")
    await _ok(rt, arts, aid)
    await _drive_to_finalized(rt, arts)
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_unregistered_plugin_predicate_fail_closed_on_restart(
    tmp_path, clock, runtime_cls
):
    """Plugin until accepted live; after restart without plugin → refuse, not extra iters."""
    import coscientist.core.plan.loop_predicates as lp

    def always_false(runtime, loop, *, iter_n, ctx):
        return False

    name = "test_plugin_gone_pr11"
    register_loop_predicate(name, always_false)
    rt, arts = await _open_study(
        tmp_path, clock, "st_loop_plug", runtime_cls=runtime_cls
    )
    await rt.accept_plan(
        [
            TaskSpec(
                task_key="POST",
                title="after",
                goal="g",
                route="experiment",
                depends_on=["loop:L1"],
                addresses=["Q1"],
                criteria=[RequirementRef(template_id="value_match")],
                required=False,
            )
        ],
        questions=[QuestionSpec(id="Q1", text="q")],
        loops=[
            _loop(
                max_iter=5,
                until=LoopPredicate(name=name, params={}),
                scope_hypotheses=[],
            )
        ],
     hypotheses=NEED_OFF, literature=NEED_OFF)
    for t in current_loop_iter_tasks(rt, "L1", 1):
        aid = await rt.start_attempt(t.spec.task_key)
        await _ok(rt, arts, aid)
    assert len(current_loop_iter_tasks(rt, "L1", 2)) == 1
    await close_runtime(rt)

    lp._REGISTRY.pop(name, None)

    rt2 = await open_runtime(
        tmp_path, "st_loop_plug", clock=clock, owner="t2", runtime_cls=runtime_cls
    )
    attach_executor(rt2, tmp_path)
    await recover(rt2)
    for t in current_loop_iter_tasks(rt2, "L1", 2):
        if t.state != TaskState.DONE:
            aid = await rt2.start_attempt(t.spec.task_key)
            await _ok(rt2, arts, aid)
    assert loop_status(rt2, "L1") == "refused"
    assert current_loop_iter_tasks(rt2, "L1", 3) == []
    events = await rt2.store.read_all()
    assert any(e.type == "LoopRefused" for e in events)
    assert any(
        e.type == "LoopCapped" and e.payload.get("reason") == "error" for e in events
    )
    assert any(
        e.type == "ExpansionRefused" and e.payload.get("loop_id") == "L1" for e in events
    )
    await _drive_to_finalized(rt2, arts)
    await close_runtime(rt2)


@pytest.mark.asyncio
async def test_plan_expanded_uses_writer_constant(tmp_path, clock, runtime_cls):
    from coscientist.core.events.events import PLAN_EXPANDED_SCHEMA_VERSION

    rt, arts = await _open_study(
        tmp_path, clock, "st_loop_sv", runtime_cls=runtime_cls
    )
    await rt.accept_plan(
        [],
        questions=[QuestionSpec(id="Q1", text="q")],
        loops=[
            _loop(
                max_iter=1,
                until=LoopPredicate(name="survivors_le", params={"n": 0}),
                scope_hypotheses=[],
            )
        ],
     hypotheses=NEED_OFF, literature=NEED_OFF)
    expanded = [
        e
        for e in await rt.store.read_all()
        if e.type == "PlanExpanded" and e.payload.get("loop")
    ]
    assert expanded
    assert all(e.schema_version == PLAN_EXPANDED_SCHEMA_VERSION for e in expanded)
    for t in current_loop_iter_tasks(rt, "L1", 1):
        aid = await rt.start_attempt(t.spec.task_key)
        await _ok(rt, arts, aid)
    await _drive_to_finalized(rt, arts)
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_loop_unexpected_error_finalizes_live(tmp_path, clock, runtime_cls):
    """Predicate body RuntimeError → journal + StudyFinalized(internal_error)."""
    import coscientist.core.plan.loop_predicates as lp
    from coscientist.core.claims.reasons import TerminalReason

    name = f"test_plugin_boom_live_{runtime_cls.__name__}"

    def boom(*_a, **_k):
        raise RuntimeError("predicate body boom")

    lp._REGISTRY.pop(name, None)
    register_loop_predicate(name, boom)
    try:
        rt, arts = await _open_study(
            tmp_path, clock, "st_loop_boom_live", runtime_cls=runtime_cls
        )
        await rt.accept_plan(
            [],
            questions=[QuestionSpec(id="Q1", text="q")],
            loops=[
                _loop(
                    max_iter=5,
                    until=LoopPredicate(name=name, params={}),
                    scope_hypotheses=[],
                )
            ],
         hypotheses=NEED_OFF, literature=NEED_OFF)
        for t in current_loop_iter_tasks(rt, "L1", 1):
            aid = await rt.start_attempt(t.spec.task_key)
            await _ok(rt, arts, aid)
        assert rt._finalized is True
        events = await rt.store.read_all()
        fins = [e for e in events if e.type == "StudyFinalized"]
        assert len(fins) == 1
        assert fins[0].payload["terminal_reason"] == TerminalReason.INTERNAL_ERROR.value
        assert any(e.type == "LoopRefused" for e in events)
        assert any(
            e.type == "LoopCapped" and e.payload.get("reason") == "error" for e in events
        )
        contrib = list(fins[0].payload.get("contributing_reasons") or [])
        assert "iteration_cap" not in contrib
        assert any("ошибкой" in str(c) or c == "error" for c in contrib)
        await close_runtime(rt)
    finally:
        lp._REGISTRY.pop(name, None)


@pytest.mark.asyncio
async def test_loop_unexpected_error_finalizes_on_recover(
    tmp_path, clock, runtime_cls, monkeypatch
):
    """recover() must finalize internal_error, not hang before tick()."""
    import coscientist.core.plan.loop as loop_mod
    import coscientist.core.plan.loop_predicates as lp

    name = f"test_plugin_ok_then_boom_{runtime_cls.__name__}"
    lp._REGISTRY.pop(name, None)
    register_loop_predicate(name, lambda *_a, **_k: False)
    try:
        rt, arts = await _open_study(
            tmp_path, clock, "st_loop_boom_rec", runtime_cls=runtime_cls
        )
        await rt.accept_plan(
            [],
            questions=[QuestionSpec(id="Q1", text="q")],
            loops=[
                _loop(
                    max_iter=5,
                    until=LoopPredicate(name=name, params={}),
                    scope_hypotheses=[],
                )
            ],
         hypotheses=NEED_OFF, literature=NEED_OFF)
        for t in current_loop_iter_tasks(rt, "L1", 1):
            aid = await rt.start_attempt(t.spec.task_key)
            await _ok(rt, arts, aid)
        assert loop_status(rt, "L1") == "running"
        assert rt._finalized is False
        await close_runtime(rt)

        rt2 = await open_runtime(
            tmp_path,
            "st_loop_boom_rec",
            clock=clock,
            owner="t2",
            runtime_cls=runtime_cls,
        )
        attach_executor(rt2, tmp_path)

        def _boom_loops(_runtime):
            raise ValueError("corrupt LoopSpec journal")

        monkeypatch.setattr(loop_mod, "_loops_from_plans", _boom_loops)
        await recover(rt2)
        assert rt2._finalized is True
        events = await rt2.store.read_all()
        fins = [e for e in events if e.type == "StudyFinalized"]
        _assert_internal_error_finalized(fins)
        # Second recover must not hang / raise; still exactly one StudyFinalized.
        await recover(rt2)
        assert rt2._finalized is True
        events2 = await rt2.store.read_all()
        _assert_internal_error_finalized(
            [e for e in events2 if e.type == "StudyFinalized"]
        )
        await close_runtime(rt2)
    finally:
        lp._REGISTRY.pop(name, None)


def test_count_survivors_and_validate_loop_specs_plan_invalid():
    """Unit: count_survivors + validate_loop_specs → PlanInvalid (no runtime)."""
    from coscientist.core.claims.rules import PlanInvalid
    from coscientist.core.plan.loop import validate_loop_specs

    class RT:
        tasks = {}
        study = None
        graph = None
        results = {}
        attempts = {}
        plans = {}

    loop = _loop(
        until=LoopPredicate(name="survivors_le", params={"n": 0}),
        scope_hypotheses=[],
        scope_questions=[],
    )
    assert count_survivors(RT(), loop, upto_iter=1) == 0
    with pytest.raises(PlanInvalid, match="max_iter is required"):
        validate_loop_specs(
            [{"loop_id": "L1", "body": [], "until": {"name": "no_change", "params": {}}}],
            known_keys=set(),
            max_loop_iter=8,
            known_questions=set(),
            known_hypotheses=set(),
        )
    with pytest.raises(PlanInvalid, match="body empty"):
        validate_loop_specs(
            [
                LoopSpec(
                    loop_id="L1",
                    body=[],
                    until=LoopPredicate(name="no_change", params={}),
                    max_iter=1,
                )
            ],
            known_keys=set(),
            max_loop_iter=8,
            known_questions=set(),
            known_hypotheses=set(),
        )


@pytest.mark.asyncio
async def test_materialize_loop_iter_writes_plan_expanded(tmp_path, clock, runtime_cls):
    """materialize_loop_iter emits PlanExpanded(loop=true) at writer schema."""
    from coscientist.core.events.events import PLAN_EXPANDED_SCHEMA_VERSION
    from coscientist.core.plan.loop import materialize_loop_iter

    rt, arts = await _open_study(
        tmp_path, clock, "st_loop_mat", runtime_cls=runtime_cls
    )
    await rt.accept_plan(
        [],
        questions=[QuestionSpec(id="Q1", text="q")],
        loops=[],
     hypotheses=NEED_OFF, literature=NEED_OFF)
    # Direct materialize (accept with empty loops leaves no auto-iter).
    loop = _loop(
        max_iter=2,
        until=LoopPredicate(name="survivors_le", params={"n": 0}),
        scope_hypotheses=[],
    )
    pid = await materialize_loop_iter(rt, loop, iter_n=1)
    assert pid
    expanded = [
        e
        for e in await rt.store.read_all()
        if e.type == "PlanExpanded" and e.payload.get("loop")
    ]
    assert expanded
    assert all(e.schema_version == PLAN_EXPANDED_SCHEMA_VERSION for e in expanded)
    assert current_loop_iter_tasks(rt, "L1", 1)
    await rt.cancel_study(by="test")
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_loop_validation_error_on_restart_no_monkeypatch(
    tmp_path, clock, runtime_cls
):
    """Corrupt plan loops → real pydantic ValidationError → internal_error finalize."""
    import coscientist.core.plan.loop_predicates as lp
    from coscientist.core.claims.reasons import TerminalReason
    from coscientist.core.events.events import (
        PLAN_ACCEPTED_SCHEMA_VERSION,
        EventEnvelope,
        make_idem,
    )

    name = f"test_plugin_keep_running_{runtime_cls.__name__}"
    lp._REGISTRY.pop(name, None)
    register_loop_predicate(name, lambda *_a, **_k: False)
    try:
        rt, arts = await _open_study(
            tmp_path, clock, "st_loop_val_re", runtime_cls=runtime_cls
        )
        await rt.accept_plan(
            [],
            questions=[QuestionSpec(id="Q1", text="q")],
            loops=[
                _loop(
                    max_iter=5,
                    until=LoopPredicate(name=name, params={}),
                    scope_hypotheses=[],
                )
            ],
         hypotheses=NEED_OFF, literature=NEED_OFF)
        for t in current_loop_iter_tasks(rt, "L1", 1):
            aid = await rt.start_attempt(t.spec.task_key)
            await _ok(rt, arts, aid)
        assert loop_status(rt, "L1") == "running"
        await close_runtime(rt)

        rt2 = await open_runtime(
            tmp_path,
            "st_loop_val_re",
            clock=clock,
            owner="t2",
            runtime_cls=runtime_cls,
        )
        attach_executor(rt2, tmp_path)
        sid = rt2.study.study_id
        # Inject a higher-revision PlanAccepted whose loops fail LoopSpec.model_validate.
        await rt2.append_events(
            [
                EventEnvelope(
                    study_id=sid,
                    type="PlanAccepted",
                    entity_key="pl_corrupt_loops",
                    idem_key=make_idem("PlanAccepted", sid, "corrupt_loops"),
                    payload={
                        "plan_id": "pl_corrupt_loops",
                        "study_id": sid,
                        "revision": 9999,
                        "hypothesis_id": None,
                        "tasks": [],
                        "questions": [{"id": "Q1", "text": "q"}],
                        "expand_after": [],
                        "loops": [
                            {
                                "loop_id": "L1",
                                "body": "not-a-list",
                                "until": {"name": "no_change", "params": {}},
                                "max_iter": 2,
                            }
                        ],
                        "hypotheses": {"needed": False, "reason": "test"},
                        "literature": {"needed": False, "reason": "test"},
                    },
                    ts=rt2.clock.now(),
                    schema_version=PLAN_ACCEPTED_SCHEMA_VERSION,
                )
            ]
        )
        await recover(rt2)
        assert rt2._finalized is True
        events = await rt2.store.read_all()
        fins = [e for e in events if e.type == "StudyFinalized"]
        assert len(fins) == 1
        assert fins[0].payload["terminal_reason"] == TerminalReason.INTERNAL_ERROR.value
        has_art = fins[0].payload.get("report_artifact_id") is not None
        has_min = fins[0].payload.get("minimal_report") is not None
        assert has_art ^ has_min
        await recover(rt2)
        assert (
            len([e for e in await rt2.store.read_all() if e.type == "StudyFinalized"])
            == 1
        )
        await close_runtime(rt2)
    finally:
        lp._REGISTRY.pop(name, None)


@pytest.mark.asyncio
async def test_refuse_loop_failure_in_expected_branch_finalizes(
    tmp_path, clock, runtime_cls, monkeypatch
):
    """Exception inside _refuse_loop (expected-eval branch) still finalizes."""
    import coscientist.core.plan.loop as loop_mod
    import coscientist.core.plan.loop_predicates as lp
    from coscientist.core.claims.reasons import TerminalReason

    name = f"test_plugin_gone_live_{runtime_cls.__name__}"
    lp._REGISTRY.pop(name, None)
    register_loop_predicate(name, lambda *_a, **_k: False)
    try:
        rt, arts = await _open_study(
            tmp_path, clock, "st_loop_refuse_boom", runtime_cls=runtime_cls
        )
        await rt.accept_plan(
            [],
            questions=[QuestionSpec(id="Q1", text="q")],
            loops=[
                _loop(
                    max_iter=5,
                    until=LoopPredicate(name=name, params={}),
                    scope_hypotheses=[],
                )
            ],
         hypotheses=NEED_OFF, literature=NEED_OFF)
        # Unregister so eval raises UnknownLoopPredicateError (expected branch).
        lp._REGISTRY.pop(name, None)

        async def _refuse_boom(*_a, **_k):
            raise RuntimeError("refuse journal failed")

        monkeypatch.setattr(loop_mod, "_refuse_loop", _refuse_boom)
        for t in current_loop_iter_tasks(rt, "L1", 1):
            aid = await rt.start_attempt(t.spec.task_key)
            await _ok(rt, arts, aid)
        assert rt._finalized is True
        events = await rt.store.read_all()
        fins = [e for e in events if e.type == "StudyFinalized"]
        assert len(fins) == 1
        assert fins[0].payload["terminal_reason"] == TerminalReason.INTERNAL_ERROR.value
        has_art = fins[0].payload.get("report_artifact_id") is not None
        has_min = fins[0].payload.get("minimal_report") is not None
        assert has_art ^ has_min
        await close_runtime(rt)
    finally:
        lp._REGISTRY.pop(name, None)


@pytest.mark.asyncio
async def test_internal_error_closes_running_tasks_and_attempts(
    tmp_path, clock, runtime_cls
):
    """RUNNING task + open attempt are closed (CORE_BUG / CRASHED) on internal_error."""
    import coscientist.core.plan.loop_predicates as lp
    from coscientist.core.claims.reasons import CloseReason, TaskReason
    from coscientist.core.events.errors import AttemptClosedError, Conflict, StudyClosedError
    from coscientist.core.execution.domain import TaskState

    name = f"test_plugin_boom_running_{runtime_cls.__name__}"
    lp._REGISTRY.pop(name, None)

    def boom(*_a, **_k):
        raise RuntimeError("predicate body boom")

    register_loop_predicate(name, boom)
    try:
        rt, arts = await _open_study(
            tmp_path, clock, "st_loop_run_close", runtime_cls=runtime_cls
        )
        await rt.accept_plan(
            [
                TaskSpec(
                    task_key="OUT",
                    title="outside",
                    goal="g",
                    route="experiment",
                    addresses=["Q1"],
                    criteria=[RequirementRef(template_id="value_match")],
                    required=False,
                )
            ],
            questions=[QuestionSpec(id="Q1", text="q")],
            loops=[
                _loop(
                    max_iter=5,
                    until=LoopPredicate(name=name, params={}),
                    scope_hypotheses=[],
                )
            ],
         hypotheses=NEED_OFF, literature=NEED_OFF)
        out = next(t for t in rt.tasks.values() if t.spec.task_key == "OUT")
        out_aid = await rt.start_attempt("OUT")
        assert out_aid
        assert rt.tasks[out.task_id].state == TaskState.RUNNING
        assert rt.attempts[out_aid].get("closed") is not True
        for t in current_loop_iter_tasks(rt, "L1", 1):
            aid = await rt.start_attempt(t.spec.task_key)
            await _ok(rt, arts, aid)
        assert rt._finalized is True
        events = await rt.store.read_all()
        fins = [e for e in events if e.type == "StudyFinalized"]
        _assert_internal_error_finalized(fins)
        out_done = [
            e
            for e in events
            if e.type == "TaskDone" and e.entity_key == out.task_id
        ]
        assert out_done
        assert out_done[-1].payload.get("reason") == TaskReason.CORE_BUG.value
        n_out_done = len(out_done)
        closed = [
            e
            for e in events
            if e.type == "AttemptClosed" and e.entity_key == out_aid
        ]
        assert closed
        assert closed[-1].payload.get("close_reason") == CloseReason.CRASHED.value
        assert rt.tasks[out.task_id].state == TaskState.DONE
        # Late write on the closed attempt must not add another TaskDone.
        with pytest.raises((AttemptClosedError, StudyClosedError, Conflict)):
            await _ok(rt, arts, out_aid)
        events_late = await rt.store.read_all()
        assert (
            len(
                [
                    e
                    for e in events_late
                    if e.type == "TaskDone" and e.entity_key == out.task_id
                ]
            )
            == n_out_done
        )
        await close_runtime(rt)
    finally:
        lp._REGISTRY.pop(name, None)


@pytest.mark.asyncio
async def test_internal_error_close_does_not_reenter_hooks(
    tmp_path, clock, runtime_cls
):
    """Nested _append(TaskDone CORE_BUG) under _finalizing must not create PLAN-n."""
    import coscientist.core.plan.loop_predicates as lp

    name = f"test_plugin_boom_reentry_{runtime_cls.__name__}"
    lp._REGISTRY.pop(name, None)

    def boom(*_a, **_k):
        raise RuntimeError("predicate body boom")

    register_loop_predicate(name, boom)
    try:
        rt, arts = await _open_study(
            tmp_path,
            clock,
            "st_loop_reentry",
            max_expansions=2,
            runtime_cls=runtime_cls,
        )
        await rt.accept_plan(
            [
                TaskSpec(
                    task_key="OUT",
                    title="outside",
                    goal="g",
                    route="experiment",
                    addresses=["Q1"],
                    criteria=[RequirementRef(template_id="value_match")],
                    required=False,
                )
            ],
            questions=[QuestionSpec(id="Q1", text="q")],
            expand_after=["OUT"],
            loops=[
                _loop(
                    max_iter=5,
                    until=LoopPredicate(name=name, params={}),
                    scope_hypotheses=[],
                )
            ],
         hypotheses=NEED_OFF, literature=NEED_OFF)
        out_aid = await rt.start_attempt("OUT")
        assert out_aid
        created_before = {
            e.entity_key
            for e in await rt.store.read_all()
            if e.type == "TaskCreated"
        }
        for t in current_loop_iter_tasks(rt, "L1", 1):
            aid = await rt.start_attempt(t.spec.task_key)
            await _ok(rt, arts, aid)
        assert rt._finalized is True
        assert rt._finalizing is False
        events = await rt.store.read_all()
        fins = [e for e in events if e.type == "StudyFinalized"]
        _assert_internal_error_finalized(fins)
        created_after = {e.entity_key for e in events if e.type == "TaskCreated"}
        # No PLAN-n / new materialization from expand_after on CORE_BUG close.
        assert created_after == created_before
        assert not any(
            t.spec.task_key.startswith("PLAN-") for t in rt.tasks.values()
        )
        await close_runtime(rt)
    finally:
        lp._REGISTRY.pop(name, None)


@pytest.mark.asyncio
async def test_count_survivors_failure_finalizes_internal_error(
    tmp_path, clock, runtime_cls, monkeypatch
):
    """Exception inside count_survivors during advance → internal_error."""
    import coscientist.core.plan.loop_predicates as lp

    def boom_survivors(*_a, **_k):
        raise RuntimeError("count_survivors boom")

    monkeypatch.setattr(lp, "count_survivors", boom_survivors)
    rt, arts = await _open_study(
        tmp_path, clock, "st_loop_surv_boom", runtime_cls=runtime_cls
    )
    await rt.accept_plan(
        [],
        questions=[QuestionSpec(id="Q1", text="q")],
        loops=[
            _loop(
                max_iter=5,
                until=LoopPredicate(name="survivors_le", params={"n": 0}),
                scope_hypotheses=[],
            )
        ],
     hypotheses=NEED_OFF, literature=NEED_OFF)
    for t in current_loop_iter_tasks(rt, "L1", 1):
        aid = await rt.start_attempt(t.spec.task_key)
        await _ok(rt, arts, aid)
    assert rt._finalized is True
    fins = [e for e in await rt.store.read_all() if e.type == "StudyFinalized"]
    _assert_internal_error_finalized(fins)
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_materialize_loop_iter_failure_finalizes_internal_error(
    tmp_path, clock, runtime_cls, monkeypatch
):
    """Exception inside materialize_loop_iter during advance → internal_error."""
    import coscientist.core.plan.loop as loop_mod
    import coscientist.core.plan.loop_predicates as lp

    name = f"test_plugin_keep_mat_{runtime_cls.__name__}"
    lp._REGISTRY.pop(name, None)
    register_loop_predicate(name, lambda *_a, **_k: False)
    try:
        rt, arts = await _open_study(
            tmp_path, clock, "st_loop_mat_boom", runtime_cls=runtime_cls
        )
        await rt.accept_plan(
            [],
            questions=[QuestionSpec(id="Q1", text="q")],
            loops=[
                _loop(
                    max_iter=5,
                    until=LoopPredicate(name=name, params={}),
                    scope_hypotheses=[],
                )
            ],
         hypotheses=NEED_OFF, literature=NEED_OFF)

        async def boom_mat(*_a, **_k):
            raise RuntimeError("materialize_loop_iter boom")

        monkeypatch.setattr(loop_mod, "materialize_loop_iter", boom_mat)
        for t in current_loop_iter_tasks(rt, "L1", 1):
            aid = await rt.start_attempt(t.spec.task_key)
            await _ok(rt, arts, aid)
        assert rt._finalized is True
        fins = [e for e in await rt.store.read_all() if e.type == "StudyFinalized"]
        _assert_internal_error_finalized(fins)
        await close_runtime(rt)
    finally:
        lp._REGISTRY.pop(name, None)


@pytest.mark.asyncio
async def test_plan_invalid_from_predicate_body_finalizes(
    tmp_path, clock, runtime_cls
):
    """PlanInvalid raised from a predicate body during advance → internal_error."""
    import coscientist.core.plan.loop_predicates as lp
    from coscientist.core.claims.rules import PlanInvalid

    name = f"test_plugin_plan_invalid_{runtime_cls.__name__}"
    lp._REGISTRY.pop(name, None)

    def raise_plan_invalid(*_a, **_k):
        raise PlanInvalid("predicate body PlanInvalid")

    register_loop_predicate(name, raise_plan_invalid)
    try:
        rt, arts = await _open_study(
            tmp_path, clock, "st_loop_pi_body", runtime_cls=runtime_cls
        )
        await rt.accept_plan(
            [],
            questions=[QuestionSpec(id="Q1", text="q")],
            loops=[
                _loop(
                    max_iter=5,
                    until=LoopPredicate(name=name, params={}),
                    scope_hypotheses=[],
                )
            ],
         hypotheses=NEED_OFF, literature=NEED_OFF)
        for t in current_loop_iter_tasks(rt, "L1", 1):
            aid = await rt.start_attempt(t.spec.task_key)
            await _ok(rt, arts, aid)
        assert rt._finalized is True
        events = await rt.store.read_all()
        fins = [e for e in events if e.type == "StudyFinalized"]
        _assert_internal_error_finalized(fins)
        await close_runtime(rt)
    finally:
        lp._REGISTRY.pop(name, None)


def test_upcast_plan_expanded_v2_loop_field_defaults():
    """Old PlanExpanded v2 without loop fields → TaskSpec defaults on replay."""
    v, p = upcast(
        "PlanExpanded",
        2,
        {
            "plan_id": "pl_old",
            "added_tasks": [
                {
                    "task_key": "EXP-0",
                    "title": "t",
                    "goal": "g",
                    "route": "experiment",
                    "expected_outputs": [
                        {"name": "main", "media_type": "application/json"}
                    ],
                }
            ],
            "added_questions": [],
        },
    )
    assert v == 5  # PR15 kind+evidence after PR16 decomposes v4
    spec = p["added_tasks"][0]
    assert spec["loop_id"] is None
    assert spec["loop_iter"] is None
    assert spec["loop_template_key"] is None
    assert p.get("rollup") == "all"
    assert spec["expected_outputs"][0].get("kind") is None
    assert spec["expected_outputs"][0].get("evidence_kind") is None
