"""M1 PR11: LoopSpec (scope, 3 predicates, mandatory max_iter, LoopCapped) / S10."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest

from coscientist.core.artifacts import make_tool_ref
from coscientist.core.claims import RuntimeClaim
from coscientist.core.domain import (
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
from coscientist.core.graph import GraphNode
from coscientist.core.ids import hypothesis_node_id
from coscientist.core.loop import loop_is_complete, loop_status
from coscientist.core.loop_predicates import (
    _claims_for_loop_scope,
    count_survivors,
    eval_loop_predicate,
    register_loop_predicate,
    scope_fingerprint,
    validate_loop_predicate,
)
from coscientist.core.reasons import Disposition, TaskReason, TerminalReason
from coscientist.core.recover import recover
from coscientist.core.upcasters import (
    UPCASTERS,
    assert_upcaster_chain_continuous,
    register_upcaster,
    upcast,
    writer_schema_version,
)
from tests.helpers import attach_executor, close_runtime, open_runtime


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
    tmp_path, clock, sid: str, *, max_loop_iter: int = 8, runtime_cls=None
):
    kw: dict[str, Any] = {"clock": clock, "owner": "t1"}
    if runtime_cls is not None:
        kw["runtime_cls"] = runtime_cls
    rt = await open_runtime(tmp_path, sid, **kw)
    await rt.create_study(
        profile="research",
        request="q",
        catalog_version="0.1.0",
        deadline=clock.now() + timedelta(seconds=400),
        sid=sid,
        finalize_reserve_s=5,
        max_expansions=0,
        max_loop_iter=max_loop_iter,
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
    assert has_art ^ has_min
    assert fin.payload.get("terminal_reason") is not None
    return fin


async def _assert_plan_refused(rt, *, match: str) -> None:
    events = await rt.store.read_all()
    refused = [e for e in events if e.type == "PlanRefused"]
    assert len(refused) == 1, "invalid LoopSpec must emit PlanRefused"
    assert match.lower() in str(refused[0].payload.get("detail") or "").lower()
    fins = [e for e in events if e.type == "StudyFinalized"]
    assert len(fins) == 1, "PlanRefused must finalize immediately"
    assert rt._finalized is True


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
    )
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
    )
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
    )
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
    )
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
    )
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
    )
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
    )

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
    )
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
    )
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
async def test_loop_refuse_survives_restart(tmp_path, clock, runtime_cls):
    """After ExpansionRefused+LoopCapped, reopen restores capped; POST unblocked."""
    rt, arts = await _open_study(
        tmp_path, clock, "st_loop_ref_re", runtime_cls=runtime_cls
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
    )
    for it in (1, 2):
        for t in current_loop_iter_tasks(rt, "L1", it):
            aid = await rt.start_attempt(t.spec.task_key)
            await _ok(rt, arts, aid)
    assert loop_status(rt, "L1") == "capped"
    live_capped = dict(getattr(rt, "_loop_capped", {})["L1"])

    await close_runtime(rt)
    rt2 = await open_runtime(
        tmp_path, "st_loop_ref_re", clock=clock, owner="t2", runtime_cls=runtime_cls
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
    )
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
    )
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

    from coscientist.core.domain import ReqStatus
    from coscientist.core.loop_predicates import _pred_criterion_met

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
    )
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


def test_survivors_le_real_claims_and_scope_filter():
    """survivors_le through real RuntimeClaims; outside-loop claims ignored."""
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    loop_key = "EXP-LOOP"
    outside_key = "EXP-OUT"

    class Study:
        study_id = "st_surv"
        refutations_needed = 1
        refutation_priority = True
        confirmations_needed = 1
        supported_needed = 2

    class RT:
        def __init__(self):
            self.study = Study()
            self.graph = object()  # non-None so _claims_for_loop_scope proceeds
            self.artifacts = None
            self.results = {}
            self.attempts = {}
            self.tasks = {
                "t_loop": Task(
                    task_id="t_loop",
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
                    task_id="t_out",
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

    def _refute(hid: str, exp: str) -> RuntimeClaim:
        return RuntimeClaim(
            study_id="st_surv",
            target=hid,
            target_kind="hypothesis",
            experiment_id=exp,
            attempt_id=f"{exp}#1",
            criterion_status="not_met",
            claim_outcome="inconsistent",
            intent="refute",
            severe=True,
            refute_capable=True,
            validity_ok=True,
            accepted=True,
            frozen=True,
            provenance="real",
            evidence_kind="computational",
            check_role="prediction",
            independence_key=exp,
            effect={
                "est": 1.0,
                "ci_low": 1.0,
                "ci_high": 1.0,
                "producer": "agent",
                "trusted": False,
                "core_evaluated": True,
                "countable": True,
            },
        )

    # H1 refuted by loop task; H2 unrefuted; H3 refuted only by outside task.
    by_target = {
        "hypothesis:H1": [_refute("H1", loop_key)],
        "hypothesis:H2": [],
        "hypothesis:H3": [_refute("H3", outside_key)],
    }

    import coscientist.core.loop_predicates as lp

    real_fct = lp.filtered_claims_by_target if hasattr(lp, "filtered_claims_by_target") else None
    # Patch at claims module used inside _claims_for_loop_scope.
    import coscientist.core.claims as claims_mod

    orig = claims_mod.filtered_claims_by_target

    def fake_filtered(*_a, **_k):
        return by_target

    claims_mod.filtered_claims_by_target = fake_filtered  # type: ignore[assignment]
    try:
        rt = RT()
        loop = _loop(
            until=LoopPredicate(name="survivors_le", params={"n": 1}),
            scope_hypotheses=["H1", "H2", "H3"],
            scope_questions=[],
        )
        scoped = _claims_for_loop_scope(rt, loop, upto_iter=1)
        assert [c.experiment_id for c in scoped["H1"]] == [loop_key]
        assert scoped["H2"] == []
        # Outside refute must not appear under loop scope.
        assert scoped["H3"] == []
        n = count_survivors(rt, loop, upto_iter=1)
        # H1 refuted (loop), H2 survivor, H3 unrefuted under loop scope → 2 survivors.
        assert n == 2
        assert eval_loop_predicate(rt, loop, iter_n=1) is False
        loop_le1 = loop.model_copy(
            update={"until": LoopPredicate(name="survivors_le", params={"n": 2})}
        )
        assert eval_loop_predicate(rt, loop_le1, iter_n=1) is True
    finally:
        claims_mod.filtered_claims_by_target = orig  # type: ignore[assignment]
        _ = real_fct


def test_dispositions_preserve_infeasible_under_cap():
    from coscientist.core.dispositions import apply_loop_cap_dispositions, dispositions

    plans = {
        "p1": {
            "questions": [{"id": "Q1", "text": "q1"}, {"id": "Q2", "text": "q2"}],
            "tasks": [],
        }
    }
    out = dispositions(
        tasks={},
        plans=plans,
        loop_capped_questions={"Q1": {"loop_id": "L1"}, "Q2": {"loop_id": "L1"}},
    )
    # not_attempted under cap → inconclusive + iteration_cap.
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
        "Q1": {"loop_id": "L1"},
        "Q2": {"loop_id": "L1"},
        "Q3": {"loop_id": "L1"},
        "Q4": {"loop_id": "L1"},
    }
    apply_loop_cap_dispositions(result, capped)
    assert result["Q1"]["disposition"] == Disposition.INFEASIBLE.value
    assert result["Q1"]["reason"] == "infeasible"
    assert result["Q3"]["disposition"] == Disposition.NEEDS_MORE_DATA.value
    assert result["Q2"]["disposition"] == Disposition.INCONCLUSIVE.value
    assert result["Q2"]["reason"] == TaskReason.ITERATION_CAP.value
    assert result["Q4"]["disposition"] == Disposition.ANSWERED.value
    assert result["Q4"]["reason"] == TaskReason.ITERATION_CAP.value


def test_upcast_study_created_max_loop_iter():
    v, p = upcast(
        "StudyCreated",
        1,
        {
            "study_id": "st_old",
            "profile": "research",
            "request": "q",
            "catalog_version": "0.1.0",
            "hitl_policy": {},
            "deadline": "2026-01-01T01:00:00+00:00",
            "created_at": "2026-01-01T00:00:00+00:00",
        },
    )
    assert v == 10
    assert p["max_loop_iter"] == 8


def test_upcast_plan_accepted_loops_default():
    v, p = upcast(
        "PlanAccepted",
        1,
        {
            "plan_id": "pl1",
            "study_id": "st",
            "revision": 1,
            "hypothesis_id": None,
            "tasks": [],
        },
    )
    assert v == 8
    assert p["loops"] == []


def test_upcast_task_created_loop_fields():
    v, p = upcast(
        "TaskCreated",
        1,
        {
            "task_id": "st:EXP-0",
            "kind": "experiment",
            "spec": {
                "task_key": "EXP-0",
                "title": "t",
                "goal": "g",
                "route": "experiment",
            },
            "state": "PENDING",
            "deadline": "2026-01-01T01:00:00+00:00",
        },
    )
    assert v == 10
    assert p["spec"]["loop_id"] is None
    assert p["spec"]["loop_iter"] is None


def test_upcaster_duplicate_and_continuous_chains():
    with pytest.raises(ValueError, match="duplicate upcaster"):
        register_upcaster("TaskCreated", 1, lambda p: p)
    for etype, writer in (
        ("StudyCreated", 10),
        ("PlanAccepted", 8),
        ("TaskCreated", 10),
        ("AttemptStarted", writer_schema_version("AttemptStarted")),
        ("ResultRecorded", writer_schema_version("ResultRecorded")),
        ("StudyFinalized", writer_schema_version("StudyFinalized")),
    ):
        assert_upcaster_chain_continuous(etype, writer)
        assert writer_schema_version(etype) == writer
    # Every registered chain is continuous 1..N-1 for its max.
    for etype, bucket in UPCASTERS.items():
        n = writer_schema_version(etype)
        assert_upcaster_chain_continuous(etype, n)
        assert max(bucket) == n - 1


def test_validate_loop_predicate_builtins():
    validate_loop_predicate(LoopPredicate(name="survivors_le", params={"n": 1}))
    validate_loop_predicate(
        LoopPredicate(name="criterion_met", params={"criterion_id": "value_match"})
    )
    validate_loop_predicate(LoopPredicate(name="no_change", params={}))
    with pytest.raises(ValueError, match="unknown"):
        validate_loop_predicate(LoopPredicate(name="Nope", params={}))
    with pytest.raises(ValueError, match="int"):
        validate_loop_predicate(LoopPredicate(name="survivors_le", params={"n": 1.5}))
    with pytest.raises(ValueError, match="int"):
        validate_loop_predicate(LoopPredicate(name="survivors_le", params={"n": True}))
    with pytest.raises(ValueError, match="int"):
        validate_loop_predicate(
            LoopPredicate(name="survivors_le", params={"n": float("inf")})
        )


def test_builtin_predicate_override_rejected():
    with pytest.raises(ValueError, match="cannot override built-in"):
        register_loop_predicate("survivors_le", lambda *a, **k: True)
    with pytest.raises(ValueError, match="cannot override built-in"):
        register_loop_predicate("criterion_met", lambda *a, **k: True)
    with pytest.raises(ValueError, match="cannot override built-in"):
        register_loop_predicate("no_change", lambda *a, **k: True)


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

    register_loop_predicate("test_always_true_pr11", always_true)
    loop = _loop(until=LoopPredicate(name="test_always_true_pr11", params={}))
    assert eval_loop_predicate(None, loop, iter_n=1) is True


def test_normalize_loop_id_nfkc_and_invisible():
    from coscientist.core.loop import normalize_loop_id
    from coscientist.core.rules import PlanInvalid

    assert normalize_loop_id("Ｌ１") == "L1"  # fullwidth → NFKC ASCII
    assert normalize_loop_id("Loop_1") == "Loop_1"
    with pytest.raises(PlanInvalid, match="invisible"):
        normalize_loop_id("L\u200b1")  # zero-width space
    with pytest.raises(PlanInvalid, match="required"):
        normalize_loop_id("   ")


@pytest.mark.asyncio
async def test_unregistered_plugin_predicate_fail_closed_on_restart(
    tmp_path, clock, runtime_cls
):
    """Plugin until accepted live; after restart without plugin → refuse, not extra iters."""
    import coscientist.core.loop_predicates as lp

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
    )
    for t in current_loop_iter_tasks(rt, "L1", 1):
        aid = await rt.start_attempt(t.spec.task_key)
        await _ok(rt, arts, aid)
    assert len(current_loop_iter_tasks(rt, "L1", 2)) == 1
    await close_runtime(rt)

    # Drop plugin from registry (simulates restart without plugin load).
    lp._REGISTRY.pop(name, None)

    rt2 = await open_runtime(
        tmp_path, "st_loop_plug", clock=clock, owner="t2", runtime_cls=runtime_cls
    )
    attach_executor(rt2, tmp_path)
    await recover(rt2)
    # Completing iter-2 body should refuse (unknown predicate), not spawn iter 3+.
    for t in current_loop_iter_tasks(rt2, "L1", 2):
        if t.state != TaskState.DONE:
            aid = await rt2.start_attempt(t.spec.task_key)
            await _ok(rt2, arts, aid)
    assert loop_status(rt2, "L1") == "capped"
    assert current_loop_iter_tasks(rt2, "L1", 3) == []
    events = await rt2.store.read_all()
    assert any(e.type == "LoopCapped" for e in events)
    assert any(
        e.type == "ExpansionRefused" and e.payload.get("loop_id") == "L1" for e in events
    )
    await _drive_to_finalized(rt2, arts)
    await close_runtime(rt2)


@pytest.mark.asyncio
async def test_plan_expanded_uses_writer_constant(tmp_path, clock, runtime_cls):
    from coscientist.core.events import PLAN_EXPANDED_SCHEMA_VERSION

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
    )
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
