"""PR2 finalize totality, I2 replay, last-resort fallbacks, legacy journal reopen."""

from __future__ import annotations

from coscientist.core.events.digest import payload_digest

from datetime import timedelta

import pytest

from coscientist.core.events.events import EventEnvelope, make_idem
from coscientist.core.events.store import SqliteEventStore
from coscientist.core.execution.artifacts import LocalArtifactStore
from coscientist.core.execution.domain import HitlPolicy, QuestionSpec, TaskKind, TaskState
from coscientist.core.finalize.finalize import last_resort_payload
from coscientist.core.finalize.finalize_nodes import (
    conclusion_id_for_question,
    conclusion_id_for_study,
    conclusion_ids_for,
)
from tests.helpers import close_runtime, open_runtime
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

    monkeypatch.setattr("coscientist.core.finalize.finalize._load_plans", boom)
    await rt.finalize(trigger="all_done")
    await _assert_finalized(rt, fallback="RuntimeError")
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_finalize_total_on_dispositions_failure(tmp_path, clock, monkeypatch):
    rt = await _boot(tmp_path, clock, "st_dp")
    await _skip_all(rt)

    def boom(**_k):
        raise ValueError("disp_boom")

    monkeypatch.setattr("coscientist.core.finalize.finalize.dispositions", boom)
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

    monkeypatch.setattr("coscientist.core.finalize.finalize.study_view", boom)
    await rt.finalize(trigger="all_done")
    await _assert_finalized(rt, fallback="RuntimeError")
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_finalize_total_on_terminal_reason_failure(tmp_path, clock, monkeypatch):
    rt = await _boot(tmp_path, clock, "st_tr")
    await _skip_all(rt)

    def boom(*_a, **_k):
        raise RuntimeError("term_boom")

    monkeypatch.setattr("coscientist.core.finalize.finalize.terminal_reason_for", boom)
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

    monkeypatch.setattr("coscientist.core.finalize.finalize.render_markdown_report", boom_render)
    monkeypatch.setattr("coscientist.core.finalize.finalize.build_minimal_report", boom_min)
    rt.artifacts = LocalArtifactStore(tmp_path / "arts")
    # primary fails on render; minimal fails → outer last-resort
    await rt.finalize(trigger="all_done")
    ev = await _assert_finalized(rt, fallback="RuntimeError")
    assert ev.payload["minimal_report"]["fallback_reason"] == "RuntimeError"
    await close_runtime(rt)


def test_last_resort_payload_constants_only():
    from coscientist.core.finalize.finalize_nodes import conclusion_ids_for

    sid = "st_x"
    p = last_resort_payload(
        study_id=sid, trigger="deadline", fallback_reason="Boom"
    )
    assert p["trigger"] == "deadline"
    assert p["terminal_reason"] == "deadline"
    assert p["fallback_reason"] == "Boom"
    assert p["question_dispositions"] == {}
    assert p["minimal_report"]["tasks"] == []
    assert p["minimal_report"]["questions"] == []
    # Matches fold ids; I2 deviation: no per-question conclusions.
    assert p["conclusion_ids"] == conclusion_ids_for(sid, {})


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
    """Retry after a full finalize is still idempotent (one StudyFinalized)."""
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
async def test_crash_between_cancelled_and_finalized(tmp_path, clock, monkeypatch):
    """Crash after StudyCancelled, before StudyFinalized; reopen finalizes."""
    sid = "st_cx_mid"
    rt = await _boot(tmp_path, clock, sid)

    async def crash_finalize(_rt, *, trigger):
        return  # simulate process death before StudyFinalized

    # cancel_study imports finalize by name each call — patch the module attr.
    monkeypatch.setattr("coscientist.core.finalize.finalize.finalize", crash_finalize)
    await rt.cancel_study(by="user")
    types = [e.type for e in await rt.store.read_all()]
    assert "StudyCancelled" in types
    assert "StudyFinalized" not in types
    await close_runtime(rt)
    rt2 = await open_runtime(tmp_path, sid, clock=clock, owner="t2")
    await rt2.tick()
    assert _fin(await rt2.store.read_all()).payload["trigger"] == "cancel"
    await close_runtime(rt2)


@pytest.mark.asyncio
async def test_crash_between_cas_put_and_append(tmp_path, clock, monkeypatch):
    """CAS put succeeds, then append fails → last-resort; reopen still finalized."""
    sid = "st_cas_mid"
    rt = await _boot(tmp_path, clock, sid)
    rt.artifacts = LocalArtifactStore(tmp_path / "arts")
    await _skip_all(rt)
    calls = {"n": 0}
    real_append = rt._append

    async def boom_then_ok(events):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("append_after_cas")
        return await real_append(events)

    monkeypatch.setattr(rt, "_append", boom_then_ok)
    await rt.finalize(trigger="all_done")
    await close_runtime(rt)
    rt2 = await open_runtime(tmp_path, sid, clock=clock, owner="t2")
    assert rt2._finalized and rt2.view().phase == "closed"
    ev = _fin(await rt2.store.read_all())
    assert ev.payload["fallback_reason"] == "OSError"
    assert ev.payload["conclusion_ids"] == conclusion_ids_for(sid, {})
    await close_runtime(rt2)


@pytest.mark.asyncio
async def test_finalize_conflict_after_commit_is_success(tmp_path, clock, monkeypatch):
    """Append commits StudyFinalized then fold fails; Conflict retry = success."""
    sid = "st_conf"
    rt = await _boot(tmp_path, clock, sid)
    await _skip_all(rt)
    real_project = rt._project_local
    blown = {"v": False}

    def boom_fold(ev):
        if ev.type == "StudyFinalized" and not blown["v"]:
            blown["v"] = True
            raise RuntimeError("fold_after_commit")
        return real_project(ev)

    monkeypatch.setattr(rt, "_project_local", boom_fold)
    await rt.finalize(trigger="all_done")
    assert rt._finalized
    assert _fin(await rt.store.read_all()).type == "StudyFinalized"
    assert rt.view().phase == "closed"
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_finalize_conflict_without_event_reraises(tmp_path, clock, monkeypatch):
    """Conflict with no StudyFinalized in store must not mark finalized."""
    from coscientist.core.events.errors import Conflict

    sid = "st_conf_neg"
    rt = await _boot(tmp_path, clock, sid)
    await _skip_all(rt)

    async def boom_conflict(_events):
        raise Conflict("idem mismatch without prior commit")

    monkeypatch.setattr(rt, "_append", boom_conflict)
    with pytest.raises(Conflict):
        await rt.finalize(trigger="all_done")
    assert not rt._finalized
    assert not any(e.type == "StudyFinalized" for e in await rt.store.read_all())
    assert rt.view().phase != "closed"
    await close_runtime(rt)


def test_rich_payload_bound_truncates_or_raises():
    from coscientist.core.events.digest import canonical_json, payload_digest
    from coscientist.core.finalize.finalize import (
        MAX_FINALIZED_PAYLOAD_BYTES,
        _bound_rich_payload,
    )

    mid = {
        "trigger": "all_done",
        "terminal_reason": "completed",
        "contributing_reasons": [f"r{i}" for i in range(50)],
        "verdict": "partial",
        "question_dispositions": {
            f"Q{i}": {
                "disposition": "not_attempted",
                "reason": "no_task",
                "formulation": "x" * 5000,
            }
            for i in range(40)
        },
        "report_artifact_id": None,
        "conclusion_ids": [],
        "minimal_report": None,
        "fallback_reason": None,
    }
    out = _bound_rich_payload(mid)
    assert len(canonical_json(out)) <= MAX_FINALIZED_PAYLOAD_BYTES
    assert all(
        len(str(d.get("formulation", ""))) <= 80
        for d in out["question_dispositions"].values()
    )
    huge = {
        **mid,
        "question_dispositions": {
            f"Q{i}": {
                "disposition": "not_attempted",
                "reason": "no_task",
                "formulation": "y" * 200,
            }
            for i in range(2000)
        },
    }
    with pytest.raises(RuntimeError, match="finalized_payload_too_large"):
        _bound_rich_payload(huge)


def test_timeout_budget_ru_survives_contributing_reasons_truncation():
    """«не проверено в пределах бюджета» must not be dropped by [:10] truncation."""
    from coscientist.core.events.digest import canonical_json
    from coscientist.core.finalize.finalize import (
        IDLE_TIMEOUT_RU,
        MAX_FINALIZED_PAYLOAD_BYTES,
        TIMEOUT_BUDGET_RU,
        _bound_rich_payload,
        truncate_contributing_reasons,
    )

    # RU lines buried past the old [:10] cut must survive.
    buried = [f"reason_{i}" for i in range(15)] + [TIMEOUT_BUDGET_RU, IDLE_TIMEOUT_RU]
    kept = truncate_contributing_reasons(buried, limit=10)
    assert TIMEOUT_BUDGET_RU in kept
    assert IDLE_TIMEOUT_RU in kept
    assert len(kept) <= 10
    # Protected lines come first (non-truncatable), then other reasons.
    assert kept[0] == TIMEOUT_BUDGET_RU
    assert kept[1] == IDLE_TIMEOUT_RU

    mid = {
        "trigger": "all_done",
        "terminal_reason": "completed",
        "contributing_reasons": buried,
        "verdict": "partial",
        "question_dispositions": {
            f"Q{i}": {
                "disposition": "not_attempted",
                "reason": "no_task",
                "formulation": "x" * 5000,
            }
            for i in range(40)
        },
        "report_artifact_id": None,
        "conclusion_ids": [],
        "minimal_report": None,
        "fallback_reason": None,
    }
    out = _bound_rich_payload(mid)
    assert len(canonical_json(out)) <= MAX_FINALIZED_PAYLOAD_BYTES
    contrib = out["contributing_reasons"]
    assert TIMEOUT_BUDGET_RU in contrib
    assert IDLE_TIMEOUT_RU in contrib


def test_conclusion_not_tested_marker_for_refute_unresolved():
    """rule=refute_unresolved → conclusion attrs.not_tested (not just inconclusive)."""
    from coscientist.core.claims.verdict import hypothesis_report_row
    from coscientist.core.finalize.finalize_nodes import (
        conclusion_id_for_question,
        conclusion_id_for_study,
        conclusion_ids_for,
        graph_rows_from_finalized,
    )

    row = hypothesis_report_row(
        hypothesis_id="H1",
        question_id="Q1",
        claims=[],
        verdict_value="inconclusive",
        rule="refute_unresolved",
    )
    assert row["verdict"] == "inconclusive"
    assert row["rule"] == "refute_unresolved"
    assert row.get("not_tested") is True

    sid = "st_nt"
    qdisp = {
        "Q1": {
            "disposition": "inconclusive",
            "reason": None,
            "formulation": "q",
            "verdict": "inconclusive",
        }
    }
    ids = conclusion_ids_for(sid, qdisp)
    assert conclusion_id_for_question(sid, "Q1") in ids
    assert conclusion_id_for_study(sid) in ids

    nodes, _edges = graph_rows_from_finalized(
        sid,
        {
            "question_dispositions": qdisp,
            "terminal_reason": "completed",
            "hypothesis_verdicts": {
                "H1": {
                    "hypothesis_id": "H1",
                    "question_id": "Q1",
                    "verdict": "inconclusive",
                    "rule": "refute_unresolved",
                    "not_tested": True,
                }
            },
        },
        created_at="2026-01-01T00:00:00+00:00",
    )
    q_cid = conclusion_id_for_question(sid, "Q1")
    s_cid = conclusion_id_for_study(sid)
    q_attrs = nodes[q_cid]["attrs"]
    assert q_attrs.get("not_tested") is True
    assert q_attrs.get("rule") == "refute_unresolved"
    assert "not_tested" in str(q_attrs.get("synthesis") or "")
    s_attrs = nodes[s_cid]["attrs"]
    assert s_attrs.get("not_tested") is True
    assert "not_tested:refute_unresolved" in str(s_attrs.get("synthesis") or "")


@pytest.mark.asyncio
async def test_recover_after_finalize(tmp_path, clock):
    """recover() after StudyFinalized is a no-op success (not cut by writable)."""
    from coscientist.core.execution.recover import recover

    sid = "st_rec_fin"
    rt = await _boot(tmp_path, clock, sid)
    await _skip_all(rt)
    await rt.finalize(trigger="all_done")
    out = await recover(rt)
    assert out["closed"] == 0
    assert _fin(await rt.store.read_all()).type == "StudyFinalized"
    await close_runtime(rt)


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
    from coscientist.core.events.errors import LeaseLost

    with pytest.raises(LeaseLost):
        await rt.tick()
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_replay_legacy_planaccepted_taskcreated_finalize(tmp_path, clock):
    """Seeded complete v1 PlanAccepted + TaskCreated reopen and finalize."""
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
                schema_version=1,
epoch=0, payload_digest=payload_digest({
                    "study_id": sid,
                    "profile": "research",
                    "request": "legacy",
                    "catalog_version": "0.1.0",
                    "hitl_policy": HitlPolicy().model_dump(mode="json"),
                    "deadline": dl.isoformat(),
                    "created_at": now.isoformat(),
                    "finalize_reserve_s": 5,
                }),
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
                            "addresses": [],
                        }
                    ],
                    "questions": [],
                },
                ts=now,
                schema_version=1,
epoch=0, payload_digest=payload_digest({
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
                            "addresses": [],
                        }
                    ],
                    "questions": [],
                }),
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
                        "addresses": [],
                    },
                    "state": TaskState.PENDING.value,
                    "outcome": None,
                    "reason": None,
                    "deadline": dl.isoformat(),
                    "attempts": [],
                    "inherited_from": None,
                },
                ts=now,
                schema_version=1,
epoch=0, payload_digest=payload_digest({
                    "task_id": f"{sid}/E1",
                    "kind": TaskKind.EXPERIMENT.value,
                    "spec": {
                        "task_key": "E1",
                        "title": "e1",
                        "goal": "g",
                        "route": "experiment",
                        "depends_on": ["PLAN"],
                        "addresses": [],
                    },
                    "state": TaskState.PENDING.value,
                    "outcome": None,
                    "reason": None,
                    "deadline": dl.isoformat(),
                    "attempts": [],
                    "inherited_from": None,
                }),
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


def test_pick_finalize_trigger(impl):
    assert impl.pick_finalize_trigger(
        cancelled=True, budget=True, deadline=True
    ) == "cancel"
    assert impl.pick_finalize_trigger(budget=True, deadline=True) == "budget"
    assert impl.pick_finalize_trigger(refused=True, deadline=True) == "intake"
    assert impl.pick_finalize_trigger(deadline=True, all_done=True) == "deadline"
    assert impl.pick_finalize_trigger(all_done=True) == "all_done"
    assert impl.pick_finalize_trigger() is None


def test_terminal_reason_for(impl):
    from datetime import UTC, datetime

    from coscientist.core.claims.reasons import TaskReason
    from coscientist.core.execution.domain import (
        Task,
        TaskKind,
        TaskOutcome,
        TaskSpec,
        TaskState,
    PlanRole,
)

    now = datetime(2026, 1, 1, tzinfo=UTC)
    t = Task(
        task_id="st/A",
        kind=TaskKind.EXPERIMENT,
        spec=TaskSpec(task_key="A", title="a", goal="g", route="experiment"),
        state=TaskState.DONE,
        outcome=TaskOutcome.FAILED,
        reason=TaskReason.HUMAN_TIMEOUT,
        deadline=now,
        plan_role=PlanRole.NONE,
)
    assert (
        impl.terminal_reason_for(
            "all_done", tasks={"a": t}, question_dispositions={}
        )
        == "human_timeout"
    )
    assert impl.terminal_reason_for("budget", tasks={}, question_dispositions={}) == (
        "budget"
    )
    assert impl.reject_write_after_finalized(True, "TaskDone") == "StudyClosedError"
