"""PR3 runtime no-hardcodes: each fixed item fails without the fix."""

from __future__ import annotations

import inspect
from types import SimpleNamespace
from typing import Any

import pytest

from coscientist.checks import evaluate as ev_mod
from coscientist.checks.evaluate import artifact_belongs_to_attempt, evaluate_task_checks
from coscientist.checks.sandbox_limits import PLAN_N_CRITIC_TEMPLATE_IDS
from coscientist.core.claims.verdict import (
    _intent,
    call_id_belongs_to_attempt,
    recompute_effect_provenance,
)
from coscientist.core.events.errors import JournalFormatError
from coscientist.core.execution.domain import (
    AgentTaskOutput,
    Catalog,
    RequirementRef,
    ReqStatus,
    TaskSpec,
)
from coscientist.core.plan import plan_n_alloc
from coscientist.core.plan.expand_trigger import ensure_plan_n_task
from coscientist.core.plan.expand_validate import assign_core_keys, next_kind_index
from coscientist.core.plan.plan_n_alloc import allocate_plan_n_key, is_replan_container


def test_artifact_belongs_to_attempt_no_startswith():
    """Belonging uses structural attempt_id — never call_id startswith."""
    src = inspect.getsource(ev_mod.artifact_belongs_to_attempt)
    assert ".startswith(" not in src

    aid = "st/E1#1"
    meta_suffix = SimpleNamespace(
        produced_by=SimpleNamespace(call_id=f"{aid}:sandbox:c")
    )
    assert not artifact_belongs_to_attempt(meta_suffix, aid)

    meta_ok = SimpleNamespace(
        produced_by=SimpleNamespace(call_id=f"{aid}:sandbox:c", attempt_id=aid)
    )
    assert artifact_belongs_to_attempt(meta_ok, aid)

    meta_wrong = SimpleNamespace(
        produced_by=SimpleNamespace(call_id=f"{aid}:1", attempt_id="other")
    )
    assert not artifact_belongs_to_attempt(meta_wrong, aid)


def test_call_belonging_uses_effect_attempt_id_not_startswith():
    """Belonging authority is structural attempt_id — never call_id startswith."""
    aid = "st/E1#1"
    assert not call_id_belongs_to_attempt(f"{aid}:sandbox:c", aid)
    assert call_id_belongs_to_attempt(aid, aid)
    assert call_id_belongs_to_attempt(
        {"attempt_id": aid, "produced_by_call_id": f"{aid}:sandbox:c"},
        aid,
    )
    assert not call_id_belongs_to_attempt(
        {"attempt_id": "other", "produced_by_call_id": f"{aid}:1"},
        aid,
    )
    assert not call_id_belongs_to_attempt("st/E1#10:1", "st/E1#1")

    effect = {
        "est": 1.0,
        "kind": "point",
        "producer": "core",
        "core_evaluated": True,
        "produced_by_call_id": f"{aid}:1",
        "attempt_id": aid,
    }
    out = recompute_effect_provenance(effect, attempt_id=aid)
    assert out is not None and out["countable"] is True
    bare = {k: v for k, v in effect.items() if k != "attempt_id"}
    out2 = recompute_effect_provenance(bare, attempt_id=aid)
    assert out2 is not None and out2["countable"] is False


def test_intent_rejects_invalid_and_missing():
    with pytest.raises(JournalFormatError, match="intent") as ei:
        _intent({})
    assert ei.value.field == "intent"
    with pytest.raises(JournalFormatError, match="invalid claim intent") as ei2:
        _intent(SimpleNamespace(intent="maybe"))
    assert ei2.value.field == "intent"
    assert _intent(SimpleNamespace(intent="refute")) == "refute"


def test_evaluate_plan_expansion_uses_frozenset_not_prefix():
    """plan_expansion markers matched by frozenset membership, not startswith."""
    src = inspect.getsource(ev_mod.evaluate_task_checks)
    assert 'startswith("plan_expansion_' not in src
    assert 'startswith("PLAN-' not in src
    assert "PLAN_N_CRITIC_TEMPLATE_IDS" in src
    assert "plan_expansion_schema" in PLAN_N_CRITIC_TEMPLATE_IDS


def test_evaluate_plan_n_marker_requires_plan_role(tmp_path):
    """Markers MET only when plan_role=replan — not via PLAN- key prefix."""
    from tests.checks.minimal_test_catalog import minimal_test_catalog
    from coscientist.core.execution.artifacts import LocalArtifactStore

    cat = minimal_test_catalog()
    arts = LocalArtifactStore(tmp_path)
    spec = TaskSpec(
        task_key="PLAN-1",
        title="t",
        goal="g",
        route="planner",
        criteria=[RequirementRef(template_id="plan_expansion_schema")],
    )
    out = AgentTaskOutput(summary="x")
    denied = evaluate_task_checks(
        cat, spec, out, arts, task_kind="plan", task_key="PLAN-1"
    )
    assert denied and denied[0].status == ReqStatus.FAILED
    assert denied[0].detail == "invalid_plan_expansion_marker"

    allowed = evaluate_task_checks(
        cat,
        spec,
        out,
        arts,
        task_kind="plan",
        task_key="PLAN-1",
        plan_role="replan",
    )
    assert allowed and allowed[0].status == ReqStatus.MET


def test_plan_n_allocator_is_single_module():
    """expand_trigger and expand_validate share plan_n_alloc.next_kind_index."""
    trigger_src = inspect.getsource(
        __import__(
            "coscientist.core.plan.expand_trigger", fromlist=["ensure_plan_n_task"]
        ).ensure_plan_n_task
    )
    assert "allocate_plan_n_key" in trigger_src
    assert "startswith(\"PLAN-\")" not in trigger_src
    assert "startswith('PLAN-')" not in trigger_src

    validate_src = inspect.getsource(next_kind_index)
    assert "_shared_next_kind_index" in validate_src or "plan_n_alloc" in inspect.getsource(
        __import__("coscientist.core.plan.expand_validate", fromlist=["next_kind_index"])
    )

    # Dual-counter formula must not appear.
    assert "_agent_expansions" not in trigger_src or "existing_plan_n" not in trigger_src


def test_allocate_plan_n_key_monotonic():
    """Allocator uses max existing PLAN-n index + 1 (not count+_agent_expansions)."""
    t0 = SimpleNamespace(spec=SimpleNamespace(task_key="PLAN-0"), kind="plan")
    t5 = SimpleNamespace(spec=SimpleNamespace(task_key="PLAN-5"), kind="plan")
    rt = SimpleNamespace(tasks={"a": t0, "b": t5}, _agent_expansions=99)
    assert allocate_plan_n_key(rt) == "PLAN-6"
    assert next_kind_index(rt, "PLAN", []) == 6


def test_is_replan_container_uses_plan_role_not_key():
    from coscientist.core.execution.domain import TaskKind

    plain = SimpleNamespace(
        kind=TaskKind.PLAN,
        plan_role=None,
        spec=SimpleNamespace(task_key="PLAN-3"),
    )
    assert not is_replan_container(plain)
    stamped = SimpleNamespace(
        kind=TaskKind.PLAN,
        plan_role="replan",
        spec=SimpleNamespace(task_key="PLAN-3"),
    )
    assert is_replan_container(stamped)
    # Non-PLAN with a PLAN- looking key is not a replan container.
    fake = SimpleNamespace(
        kind=TaskKind.EXPERIMENT,
        plan_role=None,
        spec=SimpleNamespace(task_key="PLAN-9"),
    )
    assert not is_replan_container(fake)


def test_assign_core_keys_and_allocate_share_counter():
    """assign_core_keys PLAN indices align with allocate_plan_n_key."""
    from coscientist.core.execution.domain import TaskKind

    existing = SimpleNamespace(
        spec=SimpleNamespace(task_key="PLAN-2"),
        kind=TaskKind.PLAN,
        plan_role="replan",
    )
    rt = SimpleNamespace(tasks={"t": existing})
    assert allocate_plan_n_key(rt) == "PLAN-3"
    added = [
        TaskSpec(task_key="x", title="t", goal="g", route="planner"),
    ]
    out = assign_core_keys(rt, added)
    assert not isinstance(out, str)
    assert out[0].task_key == "PLAN-3"


@pytest.mark.asyncio
async def test_ensure_plan_n_stamps_plan_role(tmp_path, clock, runtime_cls):
    """ensure_plan_n_task stamps plan_role=replan and uses shared allocator."""
    from datetime import timedelta

    from tests.helpers import close_runtime, open_runtime

    sid = "st_planrole"
    rt = await open_runtime(tmp_path, sid, clock=clock, owner="t1", runtime_cls=runtime_cls)
    await rt.create_study(
        profile="research",
        request="q",
        catalog_version=3,
        deadline=clock.now() + timedelta(seconds=300),
        sid=sid,
        finalize_reserve_s=5,
        max_expansions=3,
    )
    # Seed a PLAN-7 so allocator must pick PLAN-8 (not count-based).
    from coscientist.core.execution.domain import Task, TaskKind, TaskState
    from coscientist.core.events.ids import task_id
    from datetime import datetime, timezone

    tid7 = task_id(sid, "PLAN-7")
    rt.tasks[tid7] = Task(
        task_id=tid7,
        kind=TaskKind.PLAN,
        spec=TaskSpec(task_key="PLAN-7", title="seed", goal="g", route="planner"),
        state=TaskState.DONE,
        deadline=datetime.now(timezone.utc),
        plan_role="replan",
    )
    key = await ensure_plan_n_task(rt, after_task="E0", pending=None)
    assert key == "PLAN-8"
    created = rt.tasks[task_id(sid, key)]
    assert created.plan_role == "replan"
    assert is_replan_container(created)
    await close_runtime(rt)


def test_plan_n_alloc_module_has_no_dual_counter():
    src = inspect.getsource(plan_n_alloc)
    assert "existing_plan_n" not in src
    assert "_agent_expansions" not in src
