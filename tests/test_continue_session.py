"""DESIGN §3.6 continue_session / fork partition + durable child journal."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from coscientist.core.events.clock import FakeClock
from coscientist.core.events.events import EventEnvelope, make_idem
from coscientist.core.events.ids import is_study_id, study_id_from_idempotency_key, task_id
from coscientist.core.execution.artifacts import LocalArtifactStore, make_tool_ref
from coscientist.core.execution.domain import Clarification, TaskOutcome, TaskSpec, TaskState
from coscientist.core.plan.continue_session import (
    ContinueSessionRefuse,
    ForkPartition,
    ForkSessionRecord,
    fork_child_id,
    fork_idem_key,
    partition_fork,
    same_spec_for_inherit,
    task_spec_digest,
)
from coscientist.core.plan.fork_journal import (
    collect_inherited_artifact_ids,
    link_inherited_artifact,
    resolve_inherit_done,
)
from coscientist.experimental.runtime import StudyRuntime as ExpStudyRuntime
from tests.helpers import close_runtime, open_runtime


def _spec(key: str, *deps: str, goal: str = "g") -> TaskSpec:
    return TaskSpec(task_key=key, title=key, goal=goal, route="r", depends_on=list(deps))


def _dag() -> dict[str, TaskSpec]:
    return {
        "SPEC": _spec("SPEC"),
        "PLAN": _spec("PLAN", "SPEC"),
        "EXP-0": _spec("EXP-0", "PLAN"),
        "EXP-1": _spec("EXP-1", "EXP-0"),
        "EXP-2": _spec("EXP-2", "PLAN"),
    }


def test_partition_targeted_affected_and_inherited() -> None:
    tasks = _dag()
    p = partition_fork(
        parent_id="st_p",
        tasks=tasks,
        clarifications=[Clarification(target="EXP-0", text="r")],
    )
    assert p.affected == frozenset({"EXP-0", "EXP-1"})
    assert p.inherited == frozenset({"SPEC", "PLAN", "EXP-2"})
    for k in p.inherited:
        assert same_spec_for_inherit(tasks[k], tasks[k])
        assert p.digests[k] == task_spec_digest(tasks[k])


def test_partition_untargeted_all_affected() -> None:
    tasks = _dag()
    u = partition_fork(
        parent_id="st_p",
        tasks=tasks,
        clarifications=[Clarification(target=None, text="all")],
    )
    assert u.affected == frozenset(tasks)
    assert not u.inherited


def test_partition_stale_required_keys_expand_affected() -> None:
    tasks = _dag()
    s = partition_fork(
        parent_id="st_p",
        tasks=tasks,
        clarifications=[Clarification(target="EXP-2", text="n")],
        stale_required_keys=["EXP-0"],
    )
    assert s.affected == frozenset({"EXP-0", "EXP-1", "EXP-2"})
    assert s.inherited == frozenset({"SPEC", "PLAN"})


def test_fork_idem_key_stable_empty_nonce() -> None:
    c = [Clarification(target="EXP-1", text="once")]
    assert fork_idem_key("st_p", c, nonce="") == fork_idem_key("st_p", c, nonce="")


def test_fork_idem_key_differs_with_nonce() -> None:
    c = [Clarification(target="EXP-1", text="once")]
    assert fork_idem_key("st_p", c, nonce="") != fork_idem_key("st_p", c, nonce="n2")


def test_fork_child_id_stable() -> None:
    key = fork_idem_key("st_p", [Clarification(target="EXP-1", text="once")], nonce="")
    assert fork_child_id(key) == fork_child_id(key)


def test_fork_child_id_matches_ids_generator() -> None:
    key = fork_idem_key("st_p", [Clarification(target="EXP-1", text="once")], nonce="")
    assert fork_child_id(key) == study_id_from_idempotency_key(key)


def test_fork_child_id_is_study_id_idem_prefix() -> None:
    key = fork_idem_key("st_p", [Clarification(target="EXP-1", text="once")], nonce="")
    child = fork_child_id(key)
    assert is_study_id(child)
    assert child.startswith("st_idem_")


def test_fork_child_id_differs_with_nonce() -> None:
    c = [Clarification(target="EXP-1", text="once")]
    a = fork_idem_key("st_p", c, nonce="")
    b = fork_idem_key("st_p", c, nonce="n2")
    assert fork_child_id(a) != fork_child_id(b)


def test_partition_fork_idem_key_matches_helper() -> None:
    c = [Clarification(target="EXP-1", text="once")]
    a = fork_idem_key("st_p", c, nonce="")
    assert partition_fork(parent_id="st_p", tasks=_dag(), clarifications=c).idem_key == a


@pytest.mark.parametrize(
    ("code", "detail"),
    [
        pytest.param("", "detail", id="blank_code"),
        pytest.param("code", "   ", id="blank_detail"),
    ],
)
def test_continue_session_refuse_init_value_error(code: str, detail: str) -> None:
    with pytest.raises(ValueError, match="non-blank code and detail"):
        ContinueSessionRefuse(code, detail)


@pytest.mark.parametrize(
    ("affected", "inherited", "digests", "match"),
    [
        pytest.param(
            frozenset({"A"}),
            frozenset(),
            {"A": "d", "B": "d"},
            "affected∪inherited",
            id="cover",
        ),
        pytest.param(
            frozenset({"A"}),
            frozenset({"A"}),
            {"A": "d"},
            "must be disjoint",
            id="disjoint",
        ),
    ],
)
def test_fork_partition_disjoint_cover_value_error(
    affected: frozenset[str],
    inherited: frozenset[str],
    digests: dict[str, str],
    match: str,
) -> None:
    with pytest.raises(ValueError, match=match):
        ForkPartition(
            parent_id="st_p",
            affected=affected,
            inherited=inherited,
            digests=digests,
            idem_key="fork:x",
        )


_C0 = Clarification(target="EXP-0", text="x")
_BAD_SEQ: list[object] = [_spec("EXP-0"), "bad"]
_BAD_MAP: dict[str, object] = {"EXP-0": "not-spec"}
_BAD_KEY_MAP: dict[object, object] = {1: _spec("EXP-0")}
_BAD_CLARS: list[object] = ["not-clarification"]


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        pytest.param(
            {"clarifications": [Clarification(target="EXP-99", text="x")]},
            "unknown_target",
            id="unknown_target",
        ),
        pytest.param(
            {"clarifications": [Clarification(target="EXP-0", text="   ")]},
            "clarification_text_blank",
            id="blank_text",
        ),
        pytest.param({"clarifications": []}, "clarifications_required", id="empty_clarifications"),
        pytest.param(
            {
                "tasks": {"EXP-0": _spec("EXP-0")},
                "clarifications": [Clarification(target=None, text="x")],
            },
            "untargeted_requires_spec_or_plan",
            id="untargeted",
        ),
        pytest.param(
            {"clarifications": [_C0], "stale_required_keys": ["NOPE"]},
            "unknown_stale_key",
            id="unknown_stale",
        ),
        pytest.param(
            {"tasks": [_spec("EXP-0"), _spec("EXP-0")], "clarifications": [_C0]},
            "duplicate_task_key",
            id="dup_key",
        ),
        pytest.param(
            {"tasks": {"WRONG": _spec("EXP-0")}, "clarifications": [_C0]},
            "task_key_mismatch",
            id="key_mismatch",
        ),
        pytest.param(
            {"tasks": _BAD_SEQ, "clarifications": [_C0]},
            r"task_type:expected TaskSpec",
            id="task_type_seq",
        ),
        pytest.param(
            {"tasks": _BAD_MAP, "clarifications": [_C0]},
            r"task_type:expected TaskSpec",
            id="task_type_map",
        ),
        pytest.param(
            {"tasks": "nope", "clarifications": [_C0]},
            r"task_type:expected Mapping/Sequence",
            id="task_type_str",
        ),
        pytest.param(
            {"tasks": 1, "clarifications": [_C0]},
            r"task_type:expected Mapping/Sequence",
            id="task_type_int",
        ),
        pytest.param(
            {"tasks": _BAD_KEY_MAP, "clarifications": [_C0]},
            "task_key_type",
            id="task_key_type",
        ),
        pytest.param(
            {"clarifications": "nope"},
            "clarifications_must_be_sequence",
            id="clarifications_not_seq",
        ),
        pytest.param(
            {"clarifications": _BAD_CLARS},
            "clarification_type",
            id="clarification_type",
        ),
        pytest.param(
            {"tasks": {}, "clarifications": [_C0]},
            "tasks_required",
            id="tasks_required",
        ),
        pytest.param(
            {"clarifications": [_C0], "stale_required_keys": "EXP-0"},
            "stale_required_keys_type",
            id="stale_str",
        ),
        pytest.param(
            {"clarifications": [_C0], "stale_required_keys": 1},
            "stale_required_keys_type",
            id="stale_int",
        ),
    ],
)
def test_fork_refuses(kwargs: dict[str, object], match: str) -> None:
    with pytest.raises(ContinueSessionRefuse, match=match):
        partition_fork(
            parent_id=kwargs.get("parent_id", "st_p"),
            tasks=kwargs.get("tasks", _dag()),
            clarifications=kwargs.get("clarifications", []),
            nonce=kwargs.get("nonce", ""),
            stale_required_keys=kwargs.get("stale_required_keys", ()),
        )


def test_fork_idem_key_parent_id_required() -> None:
    with pytest.raises(ContinueSessionRefuse, match="parent_id_required"):
        fork_idem_key("  ", [_C0])


def test_fork_idem_key_nonce_must_be_str() -> None:
    bad_nonce: object = 1
    with pytest.raises(ContinueSessionRefuse, match="nonce_must_be_str"):
        fork_idem_key("st_p", [_C0], nonce=bad_nonce)


def test_fork_child_id_idem_key_required() -> None:
    with pytest.raises(ContinueSessionRefuse, match="idem_key_required"):
        fork_child_id("x")


def test_fork_session_record_child_id_required() -> None:
    part = partition_fork(parent_id="st_p", tasks=_dag(), clarifications=[_C0])
    with pytest.raises(ContinueSessionRefuse, match="child_id_required"):
        ForkSessionRecord.from_partition(part, child_id="  ")


def test_same_spec_for_inherit_false_when_goal_differs() -> None:
    assert not same_spec_for_inherit(_spec("EXP-0", goal="a"), _spec("EXP-0", goal="b"))


async def _boot_fork_runtime(tmp_path: Path, clock: FakeClock) -> ExpStudyRuntime:
    root = Path(tmp_path)
    rt = await open_runtime(root, "st_fork", clock=clock, runtime_cls=ExpStudyRuntime)
    await rt.create_study(
        profile="research",
        request="q",
        catalog_version=3,
        deadline=clock.now() + timedelta(seconds=120),
        sid="st_fork",
        finalize_reserve_s=5,
        require_hypotheses=False,
    )
    return rt


@pytest.mark.asyncio
async def test_experimental_fork_replay_same_child_id(tmp_path: Path, clock: FakeClock) -> None:
    rt = await _boot_fork_runtime(tmp_path, clock)
    c = [Clarification(target="PLAN", text="edit")]
    await rt.fork(c)
    first = rt.last_fork
    assert first is not None
    await rt.fork(c)
    assert rt.last_fork is not None
    assert rt.last_fork.child_id == first.child_id
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_experimental_fork_new_nonce_different_child_id(tmp_path: Path, clock: FakeClock) -> None:
    rt = await _boot_fork_runtime(tmp_path, clock)
    c = [Clarification(target="PLAN", text="edit")]
    await rt.fork(c)
    first = rt.last_fork
    assert first is not None
    await rt.fork(c, nonce="n2")
    assert rt.last_fork is not None
    assert rt.last_fork.child_id != first.child_id
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_experimental_fork_idempotent_across_reopen(tmp_path: Path, clock: FakeClock) -> None:
    rt = await _boot_fork_runtime(tmp_path, clock)
    c = [Clarification(target="PLAN", text="edit")]
    await rt.fork(c)
    first = rt.last_fork
    assert first is not None
    owner = rt.owner
    root = Path(tmp_path)
    await close_runtime(rt)
    clock.advance(timedelta(seconds=60))
    rt2 = await open_runtime(root, "st_fork", clock=clock, runtime_cls=ExpStudyRuntime, owner=owner)
    await rt2.fork(c)
    assert rt2.last_fork is not None
    assert rt2.last_fork.child_id == first.child_id
    await close_runtime(rt2)


async def _force_task_done(rt: ExpStudyRuntime, task_key: str) -> None:
    matches = [t for t in rt.tasks.values() if t.spec.task_key == task_key]
    assert len(matches) == 1
    assert rt.study is not None
    tid = matches[0].task_id
    await rt.append_events(
        [
            EventEnvelope(
                study_id=rt.study.study_id,
                type="TaskDone",
                entity_key=tid,
                idem_key=make_idem("TaskDone", tid, "fork_test"),
                payload={
                    "task_id": tid,
                    "outcome": TaskOutcome.SUCCESS.value,
                    "reason": None,
                },
                ts=rt.clock.now(),
                schema_version=2,
            )
        ]
    )


@pytest.mark.asyncio
async def test_durable_fork_child_journal_lineage(tmp_path: Path, clock: FakeClock) -> None:
    rt = await _boot_fork_runtime(tmp_path, clock)
    await _force_task_done(rt, "SPEC")
    await _force_task_done(rt, "HYP")
    await _force_task_done(rt, "LIT")
    c = [Clarification(target="PLAN", text="edit")]
    await rt.fork(c)
    fork = rt.last_fork
    assert fork is not None
    child_db = Path(tmp_path) / fork.child_id / "events.db"
    assert child_db.is_file()
    parent_events = await rt.store.read_all()
    assert all(ev.study_id == "st_fork" for ev in parent_events)
    child = await open_runtime(tmp_path, fork.child_id, clock=clock, runtime_cls=ExpStudyRuntime, owner=rt.owner)
    try:
        assert child.study is not None
        assert child.study.study_id == fork.child_id
        assert child.study.parent_study_id == "st_fork"
        child_events = await child.store.read_all()
        types = [ev.type for ev in child_events]
        assert "StudyCreated" in types
        assert "ForkCreated" in types
        fork_ev = next(ev for ev in child_events if ev.type == "ForkCreated")
        assert fork_ev.idem_key == fork.idem_key
        assert fork_ev.payload["parent_id"] == "st_fork"
        assert fork_ev.payload["child_id"] == fork.child_id
        spec_tid = task_id(fork.child_id, "SPEC")
        assert child.tasks[spec_tid].state == TaskState.DONE
        assert child.tasks[spec_tid].inherited_from == task_id("st_fork", "SPEC")
        plan_tid = task_id(fork.child_id, "PLAN")
        assert child.tasks[plan_tid].state == TaskState.PENDING
        assert child.tasks[plan_tid].inherited_from is None
        report_tid = task_id(fork.child_id, "REPORT")
        assert child.tasks[report_tid].state == TaskState.PENDING
    finally:
        await close_runtime(child)
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_durable_fork_reruns_only_affected_subgraph(tmp_path: Path, clock: FakeClock) -> None:
    rt = await _boot_fork_runtime(tmp_path, clock)
    for key in ("SPEC", "HYP", "LIT"):
        await _force_task_done(rt, key)
    await rt.fork([Clarification(target="PLAN", text="edit")])
    fork = rt.last_fork
    assert fork is not None
    assert set(fork.affected) == {"PLAN", "REPORT"}
    assert set(fork.inherited) == {"SPEC", "HYP", "LIT"}
    child = await open_runtime(tmp_path, fork.child_id, clock=clock, runtime_cls=ExpStudyRuntime, owner=rt.owner)
    try:
        pending = {t.spec.task_key for t in child.tasks.values() if t.state == TaskState.PENDING}
        done = {t.spec.task_key for t in child.tasks.values() if t.state == TaskState.DONE}
        assert pending == {"PLAN", "REPORT"}
        assert done == {"SPEC", "HYP", "LIT"}
    finally:
        await close_runtime(child)
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_durable_fork_restart_safe_no_duplicate_events(tmp_path: Path, clock: FakeClock) -> None:
    rt = await _boot_fork_runtime(tmp_path, clock)
    c = [Clarification(target="PLAN", text="edit")]
    await rt.fork(c)
    fork = rt.last_fork
    assert fork is not None
    child1 = await open_runtime(tmp_path, fork.child_id, clock=clock, runtime_cls=ExpStudyRuntime, owner=rt.owner)
    try:
        n1 = len(await child1.store.read_all())
    finally:
        await close_runtime(child1)
    await rt.fork(c)
    assert rt.last_fork is not None
    assert rt.last_fork.child_id == fork.child_id
    child2 = await open_runtime(tmp_path, fork.child_id, clock=clock, runtime_cls=ExpStudyRuntime, owner=rt.owner)
    try:
        events = await child2.store.read_all()
        assert len(events) == n1
        study_created = [e for e in events if e.type == "StudyCreated"]
        fork_created = [e for e in events if e.type == "ForkCreated"]
        assert len(study_created) == 1
        assert len(fork_created) == 1
    finally:
        await close_runtime(child2)
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_durable_fork_inherited_artifacts_copied(tmp_path: Path, clock: FakeClock) -> None:
    rt = await _boot_fork_runtime(tmp_path, clock)
    assert rt.artifacts is not None
    art = rt.artifacts.put_core(
        b'{"spec": true}',
        make_tool_ref("call_fork_spec", tool="core"),
        name="spec.json",
        media_type="application/json",
        role="data",
    )
    await _force_task_done(rt, "SPEC")
    await _force_task_done(rt, "HYP")
    await _force_task_done(rt, "LIT")
    rt.dep_outputs["SPEC"] = {"main": art.artifact_id}
    await rt.fork([Clarification(target="PLAN", text="edit")])
    fork = rt.last_fork
    assert fork is not None
    child = await open_runtime(tmp_path, fork.child_id, clock=clock, runtime_cls=ExpStudyRuntime, owner=rt.owner)
    try:
        assert child.artifacts is not None
        linked = child.artifacts.get_meta(art.artifact_id)
        assert linked is not None
        assert linked.sha256 == art.sha256
        assert child.artifacts.read(art.artifact_id) == b'{"spec": true}'
        registered = [e for e in await child.store.read_all() if e.type == "ArtifactRegistered"]
        assert any(e.entity_key == art.artifact_id for e in registered)
        assert child.tasks[task_id(fork.child_id, "SPEC")].inherited_from == task_id("st_fork", "SPEC")
    finally:
        await close_runtime(child)
    await close_runtime(rt)


def test_link_inherited_artifact_refuses_missing(tmp_path: Path) -> None:
    parent = LocalArtifactStore(tmp_path / "fork_art_parent")
    child = LocalArtifactStore(tmp_path / "fork_art_child")
    with pytest.raises(ContinueSessionRefuse, match="inherited_artifact_missing"):
        link_inherited_artifact(parent, child, "art_missing")


def test_link_inherited_artifact_copies_cas(tmp_path: Path) -> None:
    parent = LocalArtifactStore(tmp_path / "p")
    child = LocalArtifactStore(tmp_path / "c")
    art = parent.put_core(
        b"payload-bytes",
        make_tool_ref("c1", tool="core"),
        name="x.bin",
        media_type="application/octet-stream",
        role="data",
    )
    linked = link_inherited_artifact(parent, child, art.artifact_id)
    assert linked.sha256 == art.sha256
    assert child.read(art.artifact_id) == b"payload-bytes"


def test_resolve_inherit_done_splits_pending(clock: FakeClock) -> None:
    from coscientist.core.execution.domain import Task, TaskKind

    tasks = _dag()
    part = partition_fork(
        parent_id="st_p",
        tasks=tasks,
        clarifications=[Clarification(target="EXP-0", text="r")],
    )
    record = ForkSessionRecord.from_partition(part, child_id="st_child")
    kinds = {
        "SPEC": TaskKind.SPEC,
        "PLAN": TaskKind.PLAN,
        "EXP-0": TaskKind.EXPERIMENT,
        "EXP-1": TaskKind.EXPERIMENT,
        "EXP-2": TaskKind.EXPERIMENT,
    }
    by_key = {
        k: Task(
            task_id=f"st_p/{k}",
            kind=kinds[k],
            spec=spec,
            state=TaskState.DONE if k == "SPEC" else TaskState.PENDING,
            outcome=TaskOutcome.SUCCESS if k == "SPEC" else None,
            deadline=clock.now() + timedelta(seconds=60),
        )
        for k, spec in tasks.items()
    }
    done, pending = resolve_inherit_done(record, by_key)
    assert done == frozenset({"SPEC"})
    assert pending == frozenset({"PLAN", "EXP-2"})


def test_collect_inherited_artifact_ids_refuses_blank() -> None:
    with pytest.raises(ContinueSessionRefuse, match="artifact_ref_id_type"):
        collect_inherited_artifact_ids(
            inherit_done=frozenset({"SPEC"}),
            dep_outputs={"SPEC": {"main": "  "}},
        )
