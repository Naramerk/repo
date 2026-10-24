"""DESIGN §3.6 continue_session / fork partition."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from coscientist.core.events.ids import is_study_id, study_id_from_idempotency_key
from coscientist.core.execution.domain import Clarification, TaskSpec
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


def test_partition_affected_inherited_and_untargeted_stale() -> None:
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
    u = partition_fork(
        parent_id="st_p",
        tasks=tasks,
        clarifications=[Clarification(target=None, text="all")],
    )
    assert u.affected == frozenset(tasks) and not u.inherited
    s = partition_fork(
        parent_id="st_p",
        tasks=tasks,
        clarifications=[Clarification(target="EXP-2", text="n")],
        stale_required_keys=["EXP-0"],
    )
    assert s.affected == frozenset({"EXP-0", "EXP-1", "EXP-2"})
    assert s.inherited == frozenset({"SPEC", "PLAN"})


def test_fork_idem_key_and_child_id_stable() -> None:
    c = [Clarification(target="EXP-1", text="once")]
    a = fork_idem_key("st_p", c, nonce="")
    assert a == fork_idem_key("st_p", c, nonce="") != fork_idem_key("st_p", c, nonce="n2")
    child = fork_child_id(a)
    assert child == fork_child_id(a) == study_id_from_idempotency_key(a)
    assert is_study_id(child) and child.startswith("st_idem_")
    assert child != fork_child_id(fork_idem_key("st_p", c, nonce="n2"))
    assert partition_fork(parent_id="st_p", tasks=_dag(), clarifications=c).idem_key == a


def test_continue_session_refuse_init_value_error() -> None:
    with pytest.raises(ValueError, match="non-blank code and detail"):
        ContinueSessionRefuse("", "detail")
    with pytest.raises(ValueError, match="non-blank code and detail"):
        ContinueSessionRefuse("code", "   ")


def test_fork_partition_disjoint_cover_value_error() -> None:
    with pytest.raises(ValueError, match="affected∪inherited"):
        ForkPartition(
            parent_id="st_p",
            affected=frozenset({"A"}),
            inherited=frozenset(),
            digests={"A": "d", "B": "d"},
            idem_key="fork:x",
        )
    with pytest.raises(ValueError, match="must be disjoint"):
        ForkPartition(
            parent_id="st_p",
            affected=frozenset({"A"}),
            inherited=frozenset({"A"}),
            digests={"A": "d"},
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
            "task_type",
            id="task_type_seq",
        ),
        pytest.param(
            {"tasks": _BAD_MAP, "clarifications": [_C0]},
            "task_type",
            id="task_type_map",
        ),
        pytest.param(
            {"tasks": "nope", "clarifications": [_C0]},
            "task_type",
            id="task_type_str",
        ),
        pytest.param(
            {"tasks": 1, "clarifications": [_C0]},
            "task_type",
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


def test_fork_helpers_refuse() -> None:
    c = [_C0]
    with pytest.raises(ContinueSessionRefuse, match="parent_id_required"):
        fork_idem_key("  ", c)
    bad_nonce: object = 1
    with pytest.raises(ContinueSessionRefuse, match="nonce_must_be_str"):
        fork_idem_key("st_p", c, nonce=bad_nonce)
    with pytest.raises(ContinueSessionRefuse, match="idem_key_required"):
        fork_child_id("x")
    part = partition_fork(parent_id="st_p", tasks=_dag(), clarifications=c)
    with pytest.raises(ContinueSessionRefuse, match="child_id_required"):
        ForkSessionRecord.from_partition(part, child_id="  ")
    assert not same_spec_for_inherit(_spec("EXP-0", goal="a"), _spec("EXP-0", goal="b"))


@pytest.mark.asyncio
async def test_experimental_fork_idempotent_across_reopen(tmp_path, clock) -> None:
    from coscientist.experimental.runtime import StudyRuntime as ExpStudyRuntime
    from tests.helpers import close_runtime, open_runtime

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
    c = [Clarification(target="PLAN", text="edit")]
    await rt.fork(c)
    first = rt.last_fork
    assert first is not None
    await rt.fork(c)
    assert rt.last_fork is not None and rt.last_fork.child_id == first.child_id
    await rt.fork(c, nonce="n2")
    assert rt.last_fork is not None and rt.last_fork.child_id != first.child_id
    owner = rt.owner
    await close_runtime(rt)
    clock.advance(timedelta(seconds=60))
    rt2 = await open_runtime(root, "st_fork", clock=clock, runtime_cls=ExpStudyRuntime, owner=owner)
    await rt2.fork(c)
    assert rt2.last_fork is not None and rt2.last_fork.child_id == first.child_id
    await close_runtime(rt2)
