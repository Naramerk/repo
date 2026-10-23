"""Study create + plan accept (extracted to keep machine.py ≤ 500 lines)."""

from __future__ import annotations

from datetime import datetime, timedelta

from coscientist.core.clock import Clock
from coscientist.core.domain import (
    Budget,
    HitlPolicy,
    Plan,
    Study,
    Task,
    TaskKind,
    TaskOutcome,
    TaskSpec,
    TaskState,
)
from coscientist.core.events import EventEnvelope, make_idem
from coscientist.core.graph import GraphNode
from coscientist.core.ids import plan_id, study_id, task_id
from coscientist.core.ports import EventStore

STAGE_KEYS = [
    ("SPEC", TaskKind.SPEC),
    ("HYP", TaskKind.HYPOTHESES),
    ("LIT", TaskKind.LITERATURE),
    ("PLAN", TaskKind.PLAN),
    ("REPORT", TaskKind.REPORT),
]


class LifecycleMixin:
    """Attribute stubs so mypy accepts mixin use on StudyRuntime."""

    store: EventStore
    clock: Clock
    study: Study | None
    tasks: dict[str, Task]

    async def _append(self, events: list[EventEnvelope]) -> None:
        raise NotImplementedError

    async def create_study(
        self,
        *,
        profile: str,
        request: str,
        catalog_version: str,
        hitl_policy: HitlPolicy | None = None,
        deadline: datetime | None = None,
        parent_study_id: str | None = None,
        sid: str | None = None,
        budget: Budget | None = None,
    ) -> str:
        sid = sid or self.store.study_id or study_id()
        now = self.clock.now()
        dl = deadline or (now + timedelta(hours=2))
        study = Study(
            study_id=sid,
            profile=profile,
            request=request,
            catalog_version=catalog_version,
            hitl_policy=hitl_policy or HitlPolicy(),
            deadline=dl,
            created_at=now,
            parent_study_id=parent_study_id,
            budget=budget,
        )
        events: list[EventEnvelope] = [
            EventEnvelope(
                study_id=sid,
                type="StudyCreated",
                entity_key=sid,
                idem_key=make_idem("StudyCreated", sid),
                payload=study.model_dump(mode="json"),
                ts=now,
            ),
            EventEnvelope(
                study_id=sid,
                type="GraphNodeUpserted",
                entity_key=f"{sid}:study",
                idem_key=make_idem("GraphNodeUpserted", f"{sid}:study"),
                payload=GraphNode(
                    node_id=f"{sid}:study",
                    kind="study",
                    study_id=sid,
                    ref_id=sid,
                    created_at=now,
                ).model_dump(mode="json"),
                ts=now,
            ),
        ]
        plan_deadline = min(now + timedelta(seconds=600), dl)
        for key, kind in STAGE_KEYS:
            if kind == TaskKind.REPORT:
                deps = ["PLAN"]
            elif kind == TaskKind.SPEC:
                deps = []
            else:
                deps = [{"HYP": "SPEC", "LIT": "HYP", "PLAN": "LIT"}[key]]
            spec = TaskSpec(
                task_key=key,
                title=key,
                goal=key,
                route=kind.value,
                depends_on=deps,
                criteria=[],
            )
            tid = task_id(sid, key)
            task = Task(
                task_id=tid,
                kind=kind,
                spec=spec,
                state=TaskState.PENDING,
                deadline=plan_deadline if kind != TaskKind.REPORT else dl,
            )
            events.append(
                EventEnvelope(
                    study_id=sid,
                    type="TaskCreated",
                    entity_key=tid,
                    idem_key=make_idem("TaskCreated", tid),
                    payload=task.model_dump(mode="json"),
                    ts=now,
                )
            )
            # Graph nodes for stages: only schema kinds. SPEC/HYP/LIT are DAG
            # tasks; experiment/plan/report nodes are added when those stages
            # produce research entities (GraphBuilder in later milestones).
        await self._append(events)
        self.store.study_id = sid
        return sid

    async def accept_plan(
        self, tasks: list[TaskSpec], *, revision: int = 1
    ) -> str:
        assert self.study is not None
        sid = self.study.study_id
        now = self.clock.now()
        self._validate_dag(tasks)
        pid = plan_id()
        plan = Plan(
            plan_id=pid,
            study_id=sid,
            revision=revision,
            hypothesis_id=None,
            tasks=tasks,
        )
        events: list[EventEnvelope] = [
            EventEnvelope(
                study_id=sid,
                type="PlanAccepted",
                entity_key=pid,
                idem_key=make_idem("PlanAccepted", pid),
                payload=plan.model_dump(mode="json"),
                ts=now,
            )
        ]
        plan_tid = task_id(sid, "PLAN")
        if plan_tid in self.tasks and self.tasks[plan_tid].state != TaskState.DONE:
            events.append(
                EventEnvelope(
                    study_id=sid,
                    type="TaskDone",
                    entity_key=plan_tid,
                    idem_key=make_idem("TaskDone", plan_tid),
                    payload={
                        "task_id": plan_tid,
                        "outcome": TaskOutcome.SUCCESS.value,
                        "reason": None,
                    },
                    ts=now,
                )
            )
        for spec in tasks:
            tid = task_id(sid, spec.task_key)
            task = Task(
                task_id=tid,
                kind=TaskKind.EXPERIMENT,
                spec=spec,
                state=TaskState.PENDING,
                deadline=self.study.deadline,
            )
            events.append(
                EventEnvelope(
                    study_id=sid,
                    type="TaskCreated",
                    entity_key=tid,
                    idem_key=make_idem("TaskCreated", tid),
                    payload=task.model_dump(mode="json"),
                    ts=now,
                )
            )
        await self._append(events)
        return pid

    @staticmethod
    def _validate_dag(tasks: list[TaskSpec]) -> None:
        keys = {t.task_key for t in tasks}
        for t in tasks:
            for d in t.depends_on:
                if d not in keys and d not in {"SPEC", "HYP", "LIT", "PLAN"}:
                    raise ValueError(f"unknown dependency {d}")
        by = {t.task_key: t.depends_on for t in tasks}
        seen: set[str] = set()

        def visit(k: str, stack: set[str]) -> None:
            if k in stack:
                raise ValueError("cyclic plan DAG")
            if k in seen or k not in by:
                return
            stack.add(k)
            for d in by[k]:
                visit(d, stack)
            stack.remove(k)
            seen.add(k)

        for k in by:
            visit(k, set())
