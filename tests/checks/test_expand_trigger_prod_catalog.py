"""Real expand_trigger e2e with production-shaped catalog (no PLAN-n rows)."""

from __future__ import annotations

from datetime import timedelta

import pytest

from coscientist.checks.catalog import load_catalog
from coscientist.core.claims.graph import default_execution_registry
from coscientist.core.execution.domain import (
    AgentTaskOutput,
    ExpansionRequest,
    QuestionSpec,
    RequirementRef,
    TaskKind,
    TaskOutcome,
    TaskSpec,
    TaskState,
)
from coscientist.core.plan.expand_critic import PLAN_N_CRITIC_CRITERIA
from coscientist.core.plan.expand_trigger import maybe_expand_after_task_done
from tests.helpers import attach_executor, close_runtime, open_runtime, TEST_NEED_KW

PLAN_N_FREEZE_SKIP_IDS = frozenset(r.template_id for r in PLAN_N_CRITIC_CRITERIA)

def _prod_catalog():
    return load_catalog(
        {
            "catalog_version": 3,
            "edition": "production",
            "sources_sha256": {},
            "rules": [
                {
                    "rule_id": "R23",
                    "source": "reviewed-rules",
                    "excerpt": "x",
                    "digest": "abc",
                }
            ],
            "templates": [
                {
                    "id": "artifact_present",
                    "title": "Artifact exists",
                    "text_ru": "есть",
                    "method_sources": ["m"],
                    "rule_ids": ["R23"],
                    "applies_to": ["experiment"],
                    "check": {
                        "kind": "artifact_exists",
                        "output": "main",
                        "allow_network": False,
                        "allow_cgroup_degraded": False,
                    },
                    "role": "validity",
                    "required": True,
                    "version": 1,
                    "uses_sesoi": False,
                    "predicts_null": False,
                    "predicted_direction": "two_sided",
                    "exempt_from_freeze": False,
                }
            ],
            "defaults": {"experiment": ["artifact_present"]},
            "code_output_names": [],
        },
        kind_registry=default_execution_registry(),
    )


@pytest.mark.asyncio
async def test_expand_trigger_prod_catalog_plan_n_and_resume(tmp_path, clock):
    """Prod catalog (no plan_expansion_* rows) → PLAN-n freeze skip → critic → PlanExpanded.

    Also asserts resume ``maybe_expand_after_task_done`` is a no-op after handle.
    """
    from coscientist.core.events.events import EventEnvelope, make_idem

    prod = _prod_catalog()
    assert not any(t.id in PLAN_N_FREEZE_SKIP_IDS for t in prod.templates)
    sid = "st_exp_prod_plan_n"
    rt = await open_runtime(tmp_path, sid, clock=clock, owner="t1", catalog=prod)
    try:
        await rt.create_study(
            profile="research",
            request="q",
            catalog_version=3,
            deadline=clock.now() + timedelta(seconds=300),
            sid=sid,
            finalize_reserve_s=5,
            max_expansions=3,
        )
        attach_executor(rt, tmp_path)
        await rt.accept_plan(
            [
                TaskSpec(
                    task_key="E0",
                    title="t",
                    goal="g",
                    route="experiment",
                    depends_on=["PLAN"],
                    addresses=["Q1"],
                    criteria=[RequirementRef(template_id="artifact_present")],
                )
            ],
            questions=[QuestionSpec(id="Q1", text="why")],
            expand_after=["E0"],
         **TEST_NEED_KW)
        tid = f"{sid}/E0"
        await rt._append(
            [
                EventEnvelope(
                    study_id=sid,
                    type="TaskDone",
                    entity_key=tid,
                    idem_key=make_idem("TaskDone", tid, "prod_expand"),
                    payload={
                        "task_id": tid,
                        "outcome": TaskOutcome.SUCCESS.value,
                        "reason": None,
                    },
                    ts=clock.now(),
                    schema_version=2,
                )
            ]
        )
        assert rt.tasks[tid].state == TaskState.DONE
        plan_n = [
            t
            for t in rt.tasks.values()
            if t.kind == TaskKind.PLAN
            and {r.template_id for r in t.spec.criteria}
            == set(PLAN_N_FREEZE_SKIP_IDS)
        ]
        assert len(plan_n) == 1
        # Freeze skip: no catalog rows for markers → empty freeze map.
        assert plan_n[0].spec.sandbox_check_limits == {}
        # Resume: already handled → no-op.
        assert await maybe_expand_after_task_done(rt, tid) is None

        paid = await rt.start_attempt(plan_n[0].spec.task_key)
        await rt.executor.record(
            paid,
            AgentTaskOutput(
                summary="replan",
                expansion_request=ExpansionRequest(
                    tasks=[
                        TaskSpec(
                            task_key="agent_E1",
                            title="follow",
                            goal="g",
                            route="experiment",
                            depends_on=["E0"],
                            addresses=["Q1"],
                            criteria=[
                                RequirementRef(template_id="artifact_present")
                            ],
                        )
                    ],
                    reason="prod expand apply",
                ),
            ),
        )
        events = await rt.store.read_all()
        assert any(e.type == "PlanExpanded" for e in events)
        # Resume after PlanExpanded still no-ops for the trigger task.
        assert await maybe_expand_after_task_done(rt, tid) is None
    finally:
        await close_runtime(rt)
