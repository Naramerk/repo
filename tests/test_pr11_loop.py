"""M1 PR11: LoopSpec (scope, 3 predicates, mandatory max_iter, LoopCapped) / S10."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest

from coscientist.core.artifacts import make_tool_ref
from coscientist.core.domain import (
    AgentTaskOutput,
    ArtifactHandle,
    LoopPredicate,
    LoopSpec,
    QuestionSpec,
    RequirementRef,
    TaskSpec,
    TaskState,
)
from coscientist.core.loop import loop_is_complete, loop_status
from coscientist.core.loop_predicates import (
    eval_loop_predicate,
    register_loop_predicate,
    scope_fingerprint,
    validate_loop_predicate,
)
from coscientist.core.reasons import TaskReason, TerminalReason
from coscientist.core.recover import recover
from coscientist.core.upcasters import upcast
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
        until=until or LoopPredicate(name="survivors_le", params={"n": 1}),
        max_iter=max_iter,
        scope_hypotheses=list(scope_hypotheses or ["H1", "H2", "H3"]),
        scope_questions=list(scope_questions or ["Q1"]),
        depends_on=list(depends_on or []),
    )


def current_loop_iter_tasks(rt, loop_id: str, iter_n: int):
    return [
        t
        for t in rt.tasks.values()
        if t.spec.loop_id == loop_id and t.spec.loop_iter == iter_n
    ]


async def _ok(rt, arts, aid: str) -> None:
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


async def _boot_stages(rt) -> None:
    for key in ("SPEC", "HYP", "LIT"):
        await rt.start_attempt(key)
        await rt.cancel_task(key, reason="cancelled")


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
    await _boot_stages(rt)
    return rt, arts


async def _finalize_study(rt) -> Any:
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
async def test_loop_max_iter_missing_honest_refusal(tmp_path, clock, runtime_cls):
    """max_iter missing → accept_plan raises (honest refusal, no silent unbound)."""
    rt, _arts = await _open_study(
        tmp_path, clock, "st_loop_no_max", runtime_cls=runtime_cls
    )
    with pytest.raises(ValueError, match="max_iter is required"):
        await rt.accept_plan(
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
                    "until": {"name": "survivors_le", "params": {"n": 1}},
                    "scope_hypotheses": ["H1"],
                    "scope_questions": ["Q1"],
                }
            ],
        )
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_loop_max_iter_exceeds_profile_rejected(tmp_path, clock, runtime_cls):
    rt, _arts = await _open_study(
        tmp_path, clock, "st_loop_cap_prof", max_loop_iter=3, runtime_cls=runtime_cls
    )
    with pytest.raises(ValueError, match="max_loop_iter"):
        await rt.accept_plan(
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
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_loop_unknown_predicate_rejected(tmp_path, clock, runtime_cls):
    rt, _arts = await _open_study(
        tmp_path, clock, "st_loop_unk", runtime_cls=runtime_cls
    )
    with pytest.raises(ValueError, match="unknown loop predicate"):
        await rt.accept_plan(
            [],
            questions=[QuestionSpec(id="Q1", text="q")],
            loops=[_loop(until=LoopPredicate(name="not_a_real_pred", params={}))],
        )
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_loop_until_one_survivor(tmp_path, clock, runtime_cls, monkeypatch):
    """survivors_le(1): stop when survivors drop to 1; no LoopCapped; quota unused."""
    import coscientist.core.loop_predicates as lp

    def fake_survivors(runtime, loop, *, upto_iter):
        return 1 if int(upto_iter) >= 2 else 3

    monkeypatch.setattr(lp, "count_survivors", fake_survivors)

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
                until=LoopPredicate(name="survivors_le", params={"n": 1}),
            )
        ],
    )
    assert getattr(rt, "_agent_expansions", 0) == 0
    assert current_loop_iter_tasks(rt, "L1", 1)

    for t in current_loop_iter_tasks(rt, "L1", 1):
        aid = await rt.start_attempt(t.spec.task_key)
        await _ok(rt, arts, aid)
    assert current_loop_iter_tasks(rt, "L1", 2)
    assert not loop_is_complete(rt, "L1")

    for t in current_loop_iter_tasks(rt, "L1", 2):
        aid = await rt.start_attempt(t.spec.task_key)
        await _ok(rt, arts, aid)
    assert loop_status(rt, "L1") == "done"
    assert "L1" not in (getattr(rt, "_loop_capped", {}) or {})

    aid = await rt.start_attempt("POST")
    await _ok(rt, arts, aid)

    events = await rt.store.read_all()
    assert not any(e.type == "LoopCapped" for e in events)
    assert getattr(rt, "_agent_expansions", 0) == 0

    fin = await _finalize_study(rt)
    assert fin.payload.get("terminal_reason") != "iteration_cap"
    assert fin.payload.get("report_artifact_id") or fin.payload.get("minimal_report")
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_loop_iteration_cap_honest_final(tmp_path, clock, runtime_cls, monkeypatch):
    """Predicate never true → LoopCapped at max_iter; finalize with iteration_cap."""
    import coscientist.core.loop_predicates as lp

    monkeypatch.setattr(lp, "count_survivors", lambda *a, **k: 99)

    rt, arts = await _open_study(
        tmp_path, clock, "st_loop_cap", runtime_cls=runtime_cls
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
        questions=[QuestionSpec(id="Q1", text="which hyp")],
        loops=[
            _loop(
                max_iter=2,
                until=LoopPredicate(name="survivors_le", params={"n": 0}),
                scope_questions=["Q1"],
            )
        ],
    )

    for it in (1, 2):
        tasks = current_loop_iter_tasks(rt, "L1", it)
        assert tasks, f"expected iter {it} tasks"
        for t in tasks:
            aid = await rt.start_attempt(t.spec.task_key)
            await _ok(rt, arts, aid)

    assert loop_status(rt, "L1") == "capped"
    capped = getattr(rt, "_loop_capped", {})
    assert "L1" in capped
    assert capped["L1"]["iter"] == 2
    assert not current_loop_iter_tasks(rt, "L1", 3)

    aid = await rt.start_attempt("POST")
    await _ok(rt, arts, aid)

    fin = await _finalize_study(rt)
    events = await rt.store.read_all()
    lc = [e for e in events if e.type == "LoopCapped"]
    assert len(lc) == 1
    assert lc[0].payload["loop_id"] == "L1"
    assert lc[0].payload["iter"] == 2
    assert fin.payload["terminal_reason"] != TerminalReason.EXPANSION_LIMIT.value
    assert fin.payload.get("terminal_reason") != "iteration_cap"
    contrib = fin.payload.get("contributing_reasons") or []
    assert any("цикл остановлен лимитом" in str(x) for x in contrib) or any(
        x == "iteration_cap" for x in contrib
    )
    qdisp = fin.payload.get("question_dispositions") or {}
    assert "Q1" in qdisp
    assert qdisp["Q1"].get("reason") == TaskReason.ITERATION_CAP.value
    assert fin.payload.get("report_artifact_id") or fin.payload.get("minimal_report")
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_loop_infinite_guard_max_iter(tmp_path, clock, runtime_cls, monkeypatch):
    """Adversarial: until always false — never creates more than max_iter iters."""
    import coscientist.core.loop_predicates as lp

    monkeypatch.setattr(lp, "count_survivors", lambda *a, **k: 100)

    rt, arts = await _open_study(
        tmp_path, clock, "st_loop_inf", runtime_cls=runtime_cls
    )
    await rt.accept_plan(
        [],
        questions=[QuestionSpec(id="Q1", text="q")],
        loops=[
            _loop(
                max_iter=3,
                until=LoopPredicate(name="survivors_le", params={"n": 0}),
            )
        ],
    )
    for it in (1, 2, 3):
        for t in current_loop_iter_tasks(rt, "L1", it):
            aid = await rt.start_attempt(t.spec.task_key)
            await _ok(rt, arts, aid)
    assert not current_loop_iter_tasks(rt, "L1", 4)
    assert loop_status(rt, "L1") == "capped"
    await _finalize_study(rt)
    await close_runtime(rt)


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
    for t in current_loop_iter_tasks(rt, "L1", 1):
        aid = await rt.start_attempt(t.spec.task_key)
        await _ok(rt, arts, aid)
    assert current_loop_iter_tasks(rt, "L1", 2)
    for t in current_loop_iter_tasks(rt, "L1", 2):
        aid = await rt.start_attempt(t.spec.task_key)
        await _ok(rt, arts, aid)
    assert loop_status(rt, "L1") == "done"
    assert not current_loop_iter_tasks(rt, "L1", 3)
    await _finalize_study(rt)
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_loop_criterion_met(tmp_path, clock, runtime_cls):
    """criterion_met: MET check_results on a loop body attempt stops the loop."""
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
    for t in current_loop_iter_tasks(rt, "L1", 1):
        aid = await rt.start_attempt(t.spec.task_key)
        await _ok(rt, arts, aid)
    status = loop_status(rt, "L1")
    if status != "done":
        for _ in range(5):
            if loop_is_complete(rt, "L1"):
                break
            pending = [
                t
                for t in rt.tasks.values()
                if t.spec.loop_id == "L1" and t.state == TaskState.PENDING
            ]
            if not pending:
                await rt.tick()
                continue
            for t in pending:
                aid = await rt.start_attempt(t.spec.task_key)
                await _ok(rt, arts, aid)
    assert loop_is_complete(rt, "L1")
    await _finalize_study(rt)
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_loop_restart_idempotent(tmp_path, clock, runtime_cls, monkeypatch):
    import coscientist.core.loop_predicates as lp

    monkeypatch.setattr(lp, "count_survivors", lambda *a, **k: 99)

    rt, arts = await _open_study(
        tmp_path, clock, "st_loop_re", runtime_cls=runtime_cls
    )
    await rt.accept_plan(
        [],
        questions=[QuestionSpec(id="Q1", text="q")],
        loops=[
            _loop(
                max_iter=2,
                until=LoopPredicate(name="survivors_le", params={"n": 0}),
            )
        ],
    )
    for t in current_loop_iter_tasks(rt, "L1", 1):
        aid = await rt.start_attempt(t.spec.task_key)
        await _ok(rt, arts, aid)
    keys_before = {t.spec.task_key for t in current_loop_iter_tasks(rt, "L1", 2)}

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
    await _finalize_study(rt2)
    await close_runtime(rt2)


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


def test_validate_loop_predicate_builtins():
    validate_loop_predicate(LoopPredicate(name="survivors_le", params={"n": 1}))
    validate_loop_predicate(
        LoopPredicate(name="criterion_met", params={"criterion_id": "value_match"})
    )
    validate_loop_predicate(LoopPredicate(name="no_change", params={}))
    with pytest.raises(ValueError, match="unknown"):
        validate_loop_predicate(LoopPredicate(name="Nope", params={}))


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

    register_loop_predicate("test_always_true", always_true)
    loop = _loop(until=LoopPredicate(name="test_always_true", params={}))
    assert eval_loop_predicate(None, loop, iter_n=1) is True
