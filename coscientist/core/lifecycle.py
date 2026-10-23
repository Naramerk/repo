"""Study create + plan accept (extracted to keep machine.py ≤ 500 lines)."""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from coscientist.core.clock import Clock
from coscientist.core.domain import (
    Budget,
    HitlPolicy,
    Plan,
    QuestionSpec,
    Study,
    Task,
    TaskKind,
    TaskOutcome,
    TaskSpec,
    TaskState,
    validate_budget_for_create,
)
from coscientist.core.events import EventEnvelope, make_idem
from coscientist.core.graph import GraphNode
from coscientist.core.ids import plan_id, study_id, task_id
from coscientist.core.inputs import validate_input_specs
from coscientist.core.intake import IntakeRefuse, SafetyPort, intake
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
    _intake_refusal: IntakeRefuse | None

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
        finalize_reserve_s: int = 30,
        confirmations_needed: int = 1,
        refutations_needed: int = 1,
        refutation_priority: bool = False,
        inputs: list[Any] | None = None,
        min_plan_cost: Decimal | float | int = 0,
        safety: SafetyPort | None = None,
    ) -> str:
        sid = sid or self.store.study_id or study_id()
        now = self.clock.now()
        dl = deadline or (now + timedelta(hours=2))
        if finalize_reserve_s < 0:
            raise ValueError(f"finalize_reserve_s must be >= 0, got {finalize_reserve_s}")
        if confirmations_needed < 1:
            raise ValueError(
                f"confirmations_needed must be >= 1, got {confirmations_needed}"
            )
        if refutations_needed < 1:
            raise ValueError(
                f"refutations_needed must be >= 1, got {refutations_needed}"
            )
        validate_budget_for_create(budget)
        duration_s = (dl - now).total_seconds()
        decision = await intake(
            request,
            list(inputs or []),
            profile,
            budget,
            min_plan_cost=min_plan_cost,
            safety=safety,
            duration_s=duration_s,
            finalize_reserve_s=float(finalize_reserve_s),
        )
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
            finalize_reserve_s=finalize_reserve_s,
            confirmations_needed=confirmations_needed,
            refutations_needed=refutations_needed,
            refutation_priority=refutation_priority,
        )
        effective = study.effective_deadline()
        events: list[EventEnvelope] = [
            EventEnvelope(
                study_id=sid,
                type="StudyCreated",
                entity_key=sid,
                idem_key=make_idem("StudyCreated", sid),
                payload=study.model_dump(mode="json"),
                ts=now,
                schema_version=3,
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
        if isinstance(decision, IntakeRefuse):
            # Same transaction as StudyCreated so crash/reopen keeps refusal.
            events.append(
                EventEnvelope(
                    study_id=sid,
                    type="StudyRefused",
                    entity_key=sid,
                    idem_key=make_idem("StudyRefused", sid),
                    payload={
                        "kind": decision.kind.value,
                        "reason": decision.reason,
                        "classifier_error": decision.classifier_error,
                    },
                    ts=now,
                    schema_version=1,
                )
            )
            await self._append(events)
            self.store.study_id = sid
            from coscientist.core.finalize import finalize

            await finalize(self, trigger="intake")  # type: ignore[arg-type]
            return sid
        plan_deadline = min(now + timedelta(seconds=600), effective)
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
                required=(kind != TaskKind.REPORT),
            )
            tid = task_id(sid, key)
            task = Task(
                task_id=tid,
                kind=kind,
                spec=spec,
                state=TaskState.PENDING,
                deadline=plan_deadline if kind != TaskKind.REPORT else effective,
            )
            events.append(
                EventEnvelope(
                    study_id=sid,
                    type="TaskCreated",
                    entity_key=tid,
                    idem_key=make_idem("TaskCreated", tid),
                    payload=task.model_dump(mode="json"),
                    ts=now,
                    schema_version=5,
                )
            )
            # Graph nodes for stages: only schema kinds. SPEC/HYP/LIT are DAG
            # tasks; experiment/plan/report nodes are added when those stages
            # produce research entities (GraphBuilder in later milestones).
        await self._append(events)
        self.store.study_id = sid
        return sid

    async def accept_plan(
        self,
        tasks: list[TaskSpec],
        *,
        questions: list[QuestionSpec] | None = None,
        revision: int = 1,
        spec_output: dict[str, Any] | None = None,
    ) -> str:
        """Accept a plan revision.

        Pass ``questions`` from SPEC (``ResearchSpecOut.questions[]``), or pass
        ``spec_output`` and questions are extracted via ``questions_from_spec_output``.
        """
        assert self.study is not None
        sid = self.study.study_id
        now = self.clock.now()
        if questions is None and spec_output is not None:
            from coscientist.core.question_tasks import questions_from_spec_output

            questions = [
                QuestionSpec(**q) for q in questions_from_spec_output(spec_output)
            ]
        questions = list(questions or [])
        self._validate_dag(tasks)
        known_texts = await self._known_question_texts()
        self._validate_questions(questions, known_texts)
        known_texts = {**known_texts, **{q.id: q.text for q in questions}}
        self._validate_addresses(tasks, set(known_texts))
        validate_input_specs(tasks)
        pid = plan_id()
        plan = Plan(
            plan_id=pid,
            study_id=sid,
            revision=revision,
            hypothesis_id=None,
            tasks=tasks,
            questions=questions,
        )
        events: list[EventEnvelope] = [
            EventEnvelope(
                study_id=sid,
                type="PlanAccepted",
                entity_key=pid,
                idem_key=make_idem("PlanAccepted", pid),
                payload=plan.model_dump(mode="json"),
                ts=now,
                schema_version=2,
            )
        ]
        # Question nodes so claim ``about`` can bind without auto-creating targets.
        from coscientist.core.graph import GraphNode
        from coscientist.core.ids import question_node_id

        graph = getattr(self, "graph", None)
        for q in questions:
            qnid = question_node_id(sid, q.id)
            if graph is not None and graph.get_node(qnid) is not None:
                continue  # never clobber existing attrs
            events.append(
                EventEnvelope(
                    study_id=sid,
                    type="GraphNodeUpserted",
                    entity_key=qnid,
                    idem_key=make_idem("GraphNodeUpserted", qnid, pid),
                    payload=GraphNode(
                        node_id=qnid,
                        kind="question",
                        study_id=sid,
                        ref_id=q.id,
                        attrs={"formulation": q.text},
                        created_at=now,
                    ).model_dump(mode="json"),
                    ts=now,
                    schema_version=1,
                )
            )
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
                    schema_version=2,
                )
            )
        effective = self.study.effective_deadline()
        for spec in tasks:
            tid = task_id(sid, spec.task_key)
            task = Task(
                task_id=tid,
                kind=TaskKind.EXPERIMENT,
                spec=spec,
                state=TaskState.PENDING,
                deadline=effective,
            )
            events.append(
                EventEnvelope(
                    study_id=sid,
                    type="TaskCreated",
                    entity_key=tid,
                    idem_key=make_idem("TaskCreated", tid),
                    payload=task.model_dump(mode="json"),
                    ts=now,
                    schema_version=5,
                )
            )
        await self._append(events)
        return pid

    async def _known_question_texts(self) -> dict[str, str]:
        texts: dict[str, str] = {}
        local = getattr(self, "plans", None)
        plans = list(local.values()) if isinstance(local, dict) else []
        if not plans:
            store = getattr(self, "store", None)
            if store is not None:
                plans = await store.get_projection("plans")
        for plan in plans:
            for q in plan.get("questions") or []:
                if not isinstance(q, dict):
                    continue
                qid = str(q.get("id", ""))
                text = str(q.get("text", ""))
                if not qid:
                    continue
                if qid in texts and texts[qid] != text:
                    raise ValueError(f"question id {qid!r} text conflict")
                texts[qid] = text
        return texts

    @staticmethod
    def _validate_questions(
        questions: list[QuestionSpec], known_texts: dict[str, str]
    ) -> None:
        seen: dict[str, str] = {}
        for q in questions:
            if q.id in seen and seen[q.id] != q.text:
                raise ValueError(f"question id {q.id!r} text conflict")
            if q.id in known_texts and known_texts[q.id] != q.text:
                raise ValueError(f"question id {q.id!r} text conflict")
            seen[q.id] = q.text

    @staticmethod
    def _validate_addresses(tasks: list[TaskSpec], known: set[str]) -> None:
        for t in tasks:
            for qid in t.addresses:
                if qid not in known:
                    raise ValueError(f"unknown question id in addresses: {qid!r}")

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
