"""PR2 finalize totality, I2 replay, last-resort fallbacks, legacy upcasts."""

from __future__ import annotations

from datetime import timedelta

import pytest

from coscientist.core.artifacts import LocalArtifactStore
from coscientist.core.domain import HitlPolicy, QuestionSpec, TaskKind, TaskState
from coscientist.core.events import EventEnvelope, make_idem
from coscientist.core.finalize import last_resort_payload
from coscientist.core.finalize_nodes import (
    conclusion_id_for_question,
    conclusion_id_for_study,
)
from coscientist.core.runtime_factory import close_runtime, open_runtime
from coscientist.core.store import SqliteEventStore
from tests.test_finalize import _boot, _fin, _skip_all, _spec


async def _assert_finalized(rt, *, fallback: str | None = None):
    ev = _fin(await rt.store.read_all())
    assert ev.type == "StudyFinalized"
    if fallback is not None:
        assert ev.payload["fallback_reason"] == fallback
        assert ev.payload["minimal_report"] is not None
    return ev


@pytest.mark.asyncio
async def test_finalize_total_on_load_plans_failure(tmp_path, clock, monkeypatch):
    rt = await _boot(tmp_path, clock, "st_lp")
    await _skip_all(rt)

    async def boom(_rt):
        raise RuntimeError("plans_boom")

    monkeypatch.setattr("coscientist.core.finalize._load_plans", boom)
    await rt.finalize(trigger="all_done")
    await _assert_finalized(rt, fallback="RuntimeError")
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_finalize_total_on_dispositions_failure(tmp_path, clock, monkeypatch):
    rt = await _boot(tmp_path, clock, "st_dp")
    await _skip_all(rt)

    def boom(**_k):
        raise ValueError("disp_boom")

    monkeypatch.setattr("coscientist.core.finalize.dispositions", boom)
    await rt.finalize(trigger="all_done")
    await _assert_finalized(rt, fallback="ValueError")
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_finalize_total_on_read_all_failure(tmp_path, clock, monkeypatch):
    sid = "st_ra"
    rt = await _boot(tmp_path, clock, sid)
    await _skip_all(rt)
    real_read = rt.store.read_all

    async def boom():
        raise OSError("read_boom")

    monkeypatch.setattr(rt.store, "read_all", boom)
    await rt.finalize(trigger="deadline")
    monkeypatch.setattr(rt.store, "read_all", real_read)
    await _assert_finalized(rt, fallback="OSError")
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_finalize_total_on_study_view_failure(tmp_path, clock, monkeypatch):
    rt = await _boot(tmp_path, clock, "st_sv")
    await _skip_all(rt)

    def boom(*_a, **_k):
        raise RuntimeError("view_boom")

    monkeypatch.setattr("coscientist.core.finalize.study_view", boom)
    await rt.finalize(trigger="all_done")
    await _assert_finalized(rt, fallback="RuntimeError")
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_finalize_total_on_terminal_reason_failure(tmp_path, clock, monkeypatch):
    rt = await _boot(tmp_path, clock, "st_tr")
    await _skip_all(rt)

    def boom(*_a, **_k):
        raise RuntimeError("term_boom")

    monkeypatch.setattr("coscientist.core.finalize.terminal_reason_for", boom)
    await rt.finalize(trigger="cancel")
    await _assert_finalized(rt, fallback="RuntimeError")
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_finalize_total_on_minimal_report_failure(tmp_path, clock, monkeypatch):
    rt = await _boot(tmp_path, clock, "st_mr")
    await _skip_all(rt)

    def boom_render(*_a, **_k):
        raise RuntimeError("render")

    def boom_min(*_a, **_k):
        raise RuntimeError("minimal_boom")

    monkeypatch.setattr("coscientist.core.finalize.render_markdown_report", boom_render)
    monkeypatch.setattr("coscientist.core.finalize.build_minimal_report", boom_min)
    rt.artifacts = LocalArtifactStore(tmp_path / "arts")
    # primary fails on render; minimal fails → outer last-resort
    await rt.finalize(trigger="all_done")
    ev = await _assert_finalized(rt, fallback="RuntimeError")
    assert ev.payload["minimal_report"]["fallback_reason"] == "RuntimeError"
    await close_runtime(rt)


def test_last_resort_payload_constants_only():
    p = last_resort_payload(
        study_id="st_x", trigger="deadline", fallback_reason="Boom"
    )
    assert p["trigger"] == "deadline"
    assert p["terminal_reason"] == "deadline"
    assert p["fallback_reason"] == "Boom"
    assert p["question_dispositions"] == {}
    assert p["minimal_report"]["tasks"] == []


@pytest.mark.asyncio
async def test_i2_conclusions_after_restart(tmp_path, clock):
    sid = "st_i2"
    rt = await _boot(tmp_path, clock, sid)
    qs = [QuestionSpec(id="Q1", text="one"), QuestionSpec(id="Q2", text="two")]
    await rt.accept_plan([_spec("E1", ["Q1"]), _spec("E2", ["Q2"])], questions=qs)
    await _skip_all(rt)
    await rt.finalize(trigger="all_done")
    ev = _fin(await rt.store.read_all())
    expected = [
        conclusion_id_for_question(sid, "Q1"),
        conclusion_id_for_question(sid, "Q2"),
        conclusion_id_for_study(sid),
    ]
    assert ev.payload["conclusion_ids"] == expected
    await close_runtime(rt)
    rt2 = await open_runtime(tmp_path, sid, clock=clock, owner="t2")
    conclusions = {n.node_id for n in rt2.graph.nodes_by_kind("conclusion")}
    assert set(expected) <= conclusions
    # every question has answers edge from its conclusion
    for qid in ("Q1", "Q2"):
        cid = conclusion_id_for_question(sid, qid)
        outs = [e for e in rt2.graph.edges_from(cid) if e.kind == "answers"]
        assert outs
    await close_runtime(rt2)


@pytest.mark.asyncio
async def test_crash_mid_finalize_recover_idempotent(tmp_path, clock):
    sid = "st_crash"
    rt = await _boot(tmp_path, clock, sid)
    await _skip_all(rt)
    await rt.finalize(trigger="all_done")
    n = sum(1 for e in await rt.store.read_all() if e.type == "StudyFinalized")
    assert n == 1
    await close_runtime(rt)
    rt2 = await open_runtime(tmp_path, sid, clock=clock, owner="t2")
    await rt2.tick()
    await rt2.finalize(trigger="cancel")
    n2 = sum(1 for e in await rt2.store.read_all() if e.type == "StudyFinalized")
    assert n2 == 1
    await close_runtime(rt2)


@pytest.mark.asyncio
async def test_tick_ensure_lease_after_finalized(tmp_path, clock):
    """Preferred CI fix: tick fences even when already finalized."""
    rt = await _boot(tmp_path, clock, "st_fence")
    await _skip_all(rt)
    await rt.finalize(trigger="all_done")
    assert rt._finalized
    # still renews / does not raise when lease is valid
    await rt.tick()
    # stale epoch still raises
    rt.store.epoch = 0
    from coscientist.core.errors import LeaseLost

    with pytest.raises(LeaseLost):
        await rt.tick()
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_replay_legacy_planaccepted_v1_taskcreated_v2(tmp_path, clock):
    """Old journal PlanAccepted v1 + TaskCreated v2 upcast and finalize."""
    sid = "st_leg"
    root = tmp_path / sid
    root.mkdir()
    store = SqliteEventStore(sid, root / "events.db", clock=clock)
    await store.acquire_lease("seeder")
    now = clock.now()
    dl = now + timedelta(seconds=120)
    await store.append_many(
        [
            EventEnvelope(
                study_id=sid,
                type="StudyCreated",
                entity_key=sid,
                idem_key=make_idem("StudyCreated", sid),
                payload={
                    "study_id": sid,
                    "profile": "research",
                    "request": "legacy",
                    "catalog_version": "0.1.0",
                    "hitl_policy": HitlPolicy().model_dump(mode="json"),
                    "deadline": dl.isoformat(),
                    "created_at": now.isoformat(),
                    "finalize_reserve_s": 5,
                },
                ts=now,
                schema_version=2,
            ),
            EventEnvelope(
                study_id=sid,
                type="PlanAccepted",
                entity_key="pl_old",
                idem_key=make_idem("PlanAccepted", "pl_old"),
                payload={
                    "plan_id": "pl_old",
                    "study_id": sid,
                    "revision": 1,
                    "hypothesis_id": None,
                    "tasks": [
                        {
                            "task_key": "E1",
                            "title": "e1",
                            "goal": "g",
                            "route": "experiment",
                            "depends_on": ["PLAN"],
                        }
                    ],
                    # no questions / addresses → upcast v1→v2
                },
                ts=now,
                schema_version=1,
            ),
            EventEnvelope(
                study_id=sid,
                type="TaskCreated",
                entity_key=f"{sid}/E1",
                idem_key=make_idem("TaskCreated", f"{sid}/E1"),
                payload={
                    "task_id": f"{sid}/E1",
                    "kind": TaskKind.EXPERIMENT.value,
                    "spec": {
                        "task_key": "E1",
                        "title": "e1",
                        "goal": "g",
                        "route": "experiment",
                        "depends_on": ["PLAN"],
                    },
                    "state": TaskState.PENDING.value,
                    "outcome": None,
                    "reason": None,
                    "deadline": dl.isoformat(),
                    "attempts": [],
                    "inherited_from": None,
                },
                ts=now,
                schema_version=2,  # upcast → addresses=[]
            ),
        ]
    )
    await store.release_lease("seeder", store.epoch)
    await store.close()
    rt = await open_runtime(tmp_path, sid, clock=clock, owner="t1")
    plan = next(iter(rt.plans.values()))
    assert plan.get("questions") == []
    assert rt.tasks[f"{sid}/E1"].spec.addresses == []
    await rt.ensure_lease(ttl_s=300)
    clock.advance(timedelta(seconds=200))
    await rt.tick()
    assert rt.view().phase == "closed"
    await close_runtime(rt)


def test_pick_trigger_matches_refmodel():
    import refmodel

    from coscientist.core.finalize import pick_finalize_trigger as core_pick

    assert core_pick(cancelled=True, budget=True, deadline=True) == (
        refmodel.pick_finalize_trigger(cancelled=True, budget=True, deadline=True)
    )
    assert core_pick(budget=True, deadline=True) == refmodel.pick_finalize_trigger(
        budget=True, deadline=True
    )


def test_terminal_reason_matches_refmodel():
    from datetime import UTC, datetime

    import refmodel

    from coscientist.core.domain import (
        Task,
        TaskKind,
        TaskOutcome,
        TaskSpec,
        TaskState,
    )
    from coscientist.core.finalize import terminal_reason_for
    from coscientist.core.reasons import TaskReason

    now = datetime(2026, 1, 1, tzinfo=UTC)
    t = Task(
        task_id="st/A",
        kind=TaskKind.EXPERIMENT,
        spec=TaskSpec(task_key="A", title="a", goal="g", route="experiment"),
        state=TaskState.DONE,
        outcome=TaskOutcome.FAILED,
        reason=TaskReason.HUMAN_TIMEOUT,
        deadline=now,
    )
    core = terminal_reason_for("all_done", tasks={"a": t}, question_dispositions={})
    ref = refmodel.terminal_reason_for(
        "all_done", tasks={"a": t}, question_dispositions={}
    )
    assert core.value == ref
