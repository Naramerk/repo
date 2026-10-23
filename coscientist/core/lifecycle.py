"""Study create + plan accept (extracted to keep machine.py ≤ 500 lines)."""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

import coscientist.core.finalize as finalize_mod
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
from coscientist.core.events import (
    PLAN_ACCEPTED_SCHEMA_VERSION,
    STUDY_CREATED_SCHEMA_VERSION,
    TASK_CREATED_SCHEMA_VERSION,
    EventEnvelope,
    make_idem,
)
from coscientist.core.graph import GraphNode
from coscientist.core.ids import criterion_node_id, plan_id, question_node_id, study_id, task_id
from coscientist.core.inputs import validate_input_specs, validate_plan_criteria
from coscientist.core.intake import IntakeRefuse, SafetyPort, intake
from coscientist.core.ports import EventStore
from coscientist.core.question_tasks import questions_from_spec_output

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
        supported_needed: int = 2,
        aggregate: str = "count",
        evalue_alpha: float = 0.05,
        max_expansions: int = 2,
        max_human_expansions: int = 10,
        replan_reserve: float = 0.1,
        max_tasks: int = 64,
        max_depth: int = 8,
        plan_timeout_s: int = 600,
        max_foreach_children: int = 32,
        max_loop_iter: int = 8,
        max_task_budget_share: float = 1.0,
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
            raise ValueError(f"confirmations_needed must be >= 1, got {confirmations_needed}")
        if refutations_needed < 1:
            raise ValueError(f"refutations_needed must be >= 1, got {refutations_needed}")
        if supported_needed < 2:
            raise ValueError(f"supported_needed must be >= 2, got {supported_needed}")
        if aggregate == "evalue":
            raise ValueError(
                "aggregate='evalue' is deferred past M1 (core does not yet compute p-values); use aggregate='count'"
            )
        if aggregate != "count":
            raise ValueError(f"aggregate must be 'count', got {aggregate!r}")
        if not (0.0 < float(evalue_alpha) <= 1.0):
            raise ValueError(f"evalue_alpha out of range: {evalue_alpha}")
        if max_expansions < 0:
            raise ValueError(f"max_expansions must be >= 0, got {max_expansions}")
        if max_human_expansions < 0:
            raise ValueError(f"max_human_expansions must be >= 0, got {max_human_expansions}")
        if not (0.0 <= float(replan_reserve) <= 1.0):
            raise ValueError(f"replan_reserve out of range: {replan_reserve}")
        if max_tasks < 1:
            raise ValueError(f"max_tasks must be >= 1, got {max_tasks}")
        if max_depth < 1:
            raise ValueError(f"max_depth must be >= 1, got {max_depth}")
        if plan_timeout_s < 1:
            raise ValueError(f"plan_timeout_s must be >= 1, got {plan_timeout_s}")
        if max_foreach_children < 1:
            raise ValueError(f"max_foreach_children must be >= 1, got {max_foreach_children}")
        if max_loop_iter < 1:
            raise ValueError(f"max_loop_iter must be >= 1, got {max_loop_iter}")
        if not (0.0 <= float(max_task_budget_share) <= 1.0):
            raise ValueError(f"max_task_budget_share out of range: {max_task_budget_share}")
        validate_budget_for_create(budget)  # delegated_costly ≤ max_cost_rub
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
            supported_needed=supported_needed,
            aggregate="count",
            evalue_alpha=float(evalue_alpha),
            max_expansions=max_expansions,
            max_human_expansions=max_human_expansions,
            replan_reserve=float(replan_reserve),
            max_tasks=max_tasks,
            max_depth=max_depth,
            plan_timeout_s=plan_timeout_s,
            max_foreach_children=max_foreach_children,
            max_loop_iter=max_loop_iter,
            max_task_budget_share=float(max_task_budget_share),
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
                schema_version=STUDY_CREATED_SCHEMA_VERSION,
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
                    attrs={"profile": profile},
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
            # Via module attr so tests can monkeypatch finalize.finalize.
            await finalize_mod.finalize(self, trigger="intake")  # type: ignore[arg-type]
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
                    schema_version=TASK_CREATED_SCHEMA_VERSION,
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
        subquestions: list[QuestionSpec] | None = None,
        rollup: str = "all",
        revision: int = 1,
        spec_output: dict[str, Any] | None = None,
        expand_after: list[str] | None = None,
        loops: list[Any] | None = None,
    ) -> str:
        """Accept a plan revision.

        Pass ``questions`` from SPEC (``ResearchSpecOut.questions[]``), or pass
        ``spec_output`` and questions are extracted via ``questions_from_spec_output``.
        ``subquestions`` / ``rollup`` are PlanOut fields (M1 PR16 / DESIGN_M1 §10).
        ``expand_after`` lists task_keys that trigger planner after TaskDone (H-g).
        ``loops`` — M1 PR11 ``LoopSpec`` list (body materialised by core).
        """
        assert self.study is not None
        sid = self.study.study_id
        now = self.clock.now()
        from pydantic import ValidationError

        from coscientist.core.decomposes import assert_decomposes_acyclic, parent_map_from_questions
        from coscientist.core.foreach import validate_foreach_specs
        from coscientist.core.graph import GraphEdge
        from coscientist.core.loop import is_loop_dep, normalize_loop_id, validate_loop_specs
        from coscientist.core.plan_validate import (
            PLAN_REFUSED_LINE,
            apply_plan_refused,
            freeze_plan_rubrics,
            merge_plan_questions,
            refuse_detail,
        )
        from coscientist.core.rules import PlanInvalid
        from coscientist.core.shares import critic_plan_shares

        LOOP_PLAN_REFUSED_LINE = "не выполнено: план отклонён (LoopSpec)"
        named_subs: list[QuestionSpec] = []
        # All plan validation → PlanRefused (never raw ValueError to callers).
        raw_loops = list(loops or [])
        loop_specs: list = []
        try:
            tasks = [
                t if isinstance(t, TaskSpec) else TaskSpec.model_validate(t)
                for t in (tasks or [])
            ]
            if questions is None and spec_output is not None:
                questions = [
                    QuestionSpec.model_validate(q)
                    for q in questions_from_spec_output(spec_output)
                ]
            questions = list(questions or [])
            raw_subs = [
                s if isinstance(s, QuestionSpec) else QuestionSpec.model_validate(s)
                for s in (subquestions or [])
            ]
            questions, named_subs = merge_plan_questions(questions, raw_subs)
            expand_after = list(expand_after or [])
            self._validate_dag(tasks)
            known_texts = await self._known_question_texts()
            self._validate_questions(questions, known_texts)
            known_texts = {**known_texts, **{q.id: q.text for q in questions}}
            self._validate_addresses(tasks, set(known_texts))
            task_keys = {t.task_key for t in tasks}
            for k in expand_after:
                if k not in task_keys:
                    raise ValueError(f"expand_after unknown task_key: {k!r}")
            known_keys = {t.spec.task_key for t in self.tasks.values()} | task_keys
            graph = getattr(self, "graph", None)
            known_hyps: set[str] = set()
            if graph is not None:
                for node in graph.nodes_by_kind("hypothesis"):
                    hid = str(getattr(node, "ref_id", None) or "")
                    if hid:
                        known_hyps.add(hid)
            loop_specs = validate_loop_specs(
                raw_loops,
                known_keys=known_keys,
                max_loop_iter=int(getattr(self.study, "max_loop_iter", 8) or 8),
                known_questions=set(known_texts),
                known_hypotheses=known_hyps,
            )
            validate_input_specs(tasks)
            validate_foreach_specs(
                tasks,
                known_keys={t.spec.task_key for t in self.tasks.values()},
            )
            share_err = critic_plan_shares(tasks, study=self.study)
            if share_err is not None:
                raise ValueError(share_err)
            catalog = getattr(self, "catalog", None) or getattr(
                getattr(self, "executor", None), "catalog", None
            )
            validate_plan_criteria(tasks, catalog)
            tasks, questions = freeze_plan_rubrics(
                tasks,
                questions,
                catalog=catalog,
                subquestions=named_subs,
                rollup=rollup,
                max_depth=int(getattr(self.study, "max_depth", 8) or 8),
                max_tasks=int(getattr(self.study, "max_tasks", 64) or 64),
            )
            assert_decomposes_acyclic(parent_map_from_questions(questions))
            loop_ids = {ls.loop_id for ls in loop_specs}
            # NFKC-normalize loop:<id> barriers and store normalized deps on tasks.
            rewritten: list[TaskSpec] = []
            for t in tasks:
                new_deps: list[str] = []
                changed = False
                for d in t.depends_on:
                    if not is_loop_dep(d):
                        new_deps.append(d)
                        continue
                    try:
                        lid = normalize_loop_id(d[len("loop:") :])
                    except PlanInvalid as e:
                        raise PlanInvalid(
                            f"depends_on invalid loop barrier {d!r}: {e}"
                        ) from e
                    if lid not in loop_ids:
                        raise PlanInvalid(f"depends_on unknown loop barrier {d!r}")
                    nd = f"loop:{lid}"
                    if nd != d:
                        changed = True
                    new_deps.append(nd)
                rewritten.append(
                    t.model_copy(update={"depends_on": new_deps}) if changed else t
                )
            tasks = rewritten
        except PlanInvalid as e:
            return await apply_plan_refused(
                self,
                detail=refuse_detail(e),
                report_line=LOOP_PLAN_REFUSED_LINE,
            )
        except (ValueError, ValidationError) as e:
            line = LOOP_PLAN_REFUSED_LINE if raw_loops else PLAN_REFUSED_LINE
            return await apply_plan_refused(
                self, detail=refuse_detail(e), report_line=line
            )
        except TypeError as e:
            # LoopSpec pydantic / type errors at accept → same refuse path.
            if raw_loops:
                return await apply_plan_refused(
                    self,
                    detail=refuse_detail(e),
                    report_line=LOOP_PLAN_REFUSED_LINE,
                )
            raise
        pid = plan_id()
        plan = Plan(
            plan_id=pid,
            study_id=sid,
            revision=revision,
            hypothesis_id=None,
            tasks=tasks,
            questions=questions,
            expand_after=expand_after,
            loops=loop_specs,
            subquestions=named_subs,
            rollup=rollup,  # type: ignore[arg-type]
        )
        events: list[EventEnvelope] = [
            EventEnvelope(
                study_id=sid,
                type="PlanAccepted",
                entity_key=pid,
                idem_key=make_idem("PlanAccepted", pid),
                payload=plan.model_dump(mode="json"),
                ts=now,
                schema_version=PLAN_ACCEPTED_SCHEMA_VERSION,
            )
        ]
        # Question nodes so claim ``about`` can bind without auto-creating targets.
        graph = getattr(self, "graph", None)
        for q in questions:
            qnid = question_node_id(sid, q.id)
            if graph is not None and graph.get_node(qnid) is not None:
                continue  # never clobber existing attrs
            q_attrs: dict[str, Any] = {
                "formulation": q.text,
                "final_type": getattr(q, "final_type", None) or "verdict",
            }
            if getattr(q, "parent_id", None):
                q_attrs["parent_id"] = q.parent_id
            if getattr(q, "origin", None):
                q_attrs["origin"] = q.origin
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
                        attrs=q_attrs,
                        created_at=now,
                    ).model_dump(mode="json"),
                    ts=now,
                    schema_version=1,
                )
            )
        # PR16: decomposes edges on accept (child → parent); acyclicity already checked.
        for q in questions:
            parent = getattr(q, "parent_id", None)
            if not parent:
                continue
            qnid = question_node_id(sid, q.id)
            pn = question_node_id(sid, str(parent))
            eid = f"decomposes:{qnid}:{pn}"
            origin = getattr(q, "origin", None) or "explicit"
            events.append(
                EventEnvelope(
                    study_id=sid,
                    type="GraphEdgeAdded",
                    entity_key=eid,
                    idem_key=make_idem("GraphEdgeAdded", eid),
                    payload=GraphEdge(
                        edge_id=eid,
                        kind="decomposes",
                        study_id=sid,
                        src_id=qnid,
                        dst_id=pn,
                        attrs={"origin": origin},
                        created_at=now,
                    ).model_dump(mode="json"),
                    ts=now,
                    schema_version=1,
                )
            )
        # Freeze rubric digests onto criterion-like nodes (frozen_at_seq via projection).
        seen_rubrics: set[str] = set()
        for spec in tasks:
            for o in spec.expected_outputs or []:
                rub = getattr(o, "rubric", None)
                if rub is None:
                    continue
                dig = str(getattr(rub, "digest", "") or "")
                if not dig or dig in seen_rubrics:
                    continue
                seen_rubrics.add(dig)
                rnid = criterion_node_id(sid, f"rubric:{dig}")
                if graph is not None and graph.get_node(rnid) is not None:
                    continue
                events.append(
                    EventEnvelope(
                        study_id=sid,
                        type="GraphNodeUpserted",
                        entity_key=rnid,
                        idem_key=make_idem("GraphNodeUpserted", rnid, pid),
                        payload=GraphNode(
                            node_id=rnid,
                            kind="criterion",
                            study_id=sid,
                            ref_id=f"rubric:{dig}",
                            attrs={
                                "frozen": True,
                                "template_id": f"rubric:{dig}",
                                "rubric_digest": dig,
                                "check": "rubric",
                                # Qualitative — never a stance source.
                                "affects_stance": False,
                            },
                            created_at=now,
                        ).model_dump(mode="json"),
                        ts=now,
                        schema_version=1,
                    )
                )
        # Freeze plan criteria at accept (DESIGN_M1 §6 / frozen_at_seq).
        seen_criteria: set[str] = set()
        for spec in tasks:
            for ref in spec.criteria or []:
                key = str(getattr(ref, "template_id", None) or ref)
                if not key or key in seen_criteria:
                    continue
                seen_criteria.add(key)
                cnid = criterion_node_id(sid, key)
                if graph is not None and graph.get_node(cnid) is not None:
                    existing = graph.get_node(cnid)
                    if existing is not None and (
                        (existing.attrs or {}).get("frozen_at_seq") is not None
                        or bool((existing.attrs or {}).get("frozen"))
                    ):
                        continue
                # SESOI / predicts_null from catalog template only — never params.
                cattrs: dict[str, Any] = {"frozen": True, "template_id": key}
                cattrs["alpha"] = 0.05
                if catalog is not None:
                    by_id = {t.id: t for t in getattr(catalog, "templates", []) or []}
                    tmpl = by_id.get(key)
                    if tmpl is not None:
                        if getattr(tmpl, "uses_sesoi", False) and tmpl.sesoi is not None:
                            cattrs["uses_sesoi"] = True
                            cattrs["sesoi"] = float(tmpl.sesoi)
                            cattrs["predicted_direction"] = str(getattr(tmpl, "predicted_direction", "two_sided"))
                        else:
                            cattrs["uses_sesoi"] = False
                        cattrs["predicts_null"] = bool(getattr(tmpl, "predicts_null", False))
                events.append(
                    EventEnvelope(
                        study_id=sid,
                        type="GraphNodeUpserted",
                        entity_key=cnid,
                        idem_key=make_idem("GraphNodeUpserted", cnid, pid),
                        payload=GraphNode(
                            node_id=cnid,
                            kind="criterion",
                            study_id=sid,
                            ref_id=key,
                            attrs=cattrs,
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
        from coscientist.checks.sandbox_limits import freeze_task_sandbox_limits

        for spec in tasks:
            frozen_spec = freeze_task_sandbox_limits(spec, catalog)
            tid = task_id(sid, frozen_spec.task_key)
            task = Task(
                task_id=tid,
                kind=TaskKind.EXPERIMENT,
                spec=frozen_spec,
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
                    schema_version=TASK_CREATED_SCHEMA_VERSION,
                )
            )
        await self._append(events)
        # B1: foreach templates whose of_task is already DONE (e.g. HYP-0).
        from coscientist.core.foreach import materialize_ready_foreach
        from coscientist.core.loop import maybe_advance_loops

        await materialize_ready_foreach(self)  # type: ignore[arg-type]
        await maybe_advance_loops(self)  # type: ignore[arg-type]
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
    def _validate_questions(questions: list[QuestionSpec], known_texts: dict[str, str]) -> None:
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
        stage = {"SPEC", "HYP", "LIT", "PLAN"}
        for t in tasks:
            for d in t.depends_on:
                # Allow stage keys, peers in this revision, and PLAN-n containers.
                # Expand revisions may depend on keys that only exist as runtime
                # tasks (already DONE) — callers pass those via merged still_kept.
                # PR11: loop:<id> barriers resolved by core after LoopCapped/until.
                if (
                    d in keys
                    or d in stage
                    or d.startswith("PLAN-")
                    or d.startswith("loop:")
                ):
                    continue
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
                if str(d).startswith("loop:"):
                    continue
                visit(d, stack)
            stack.remove(k)
            seen.add(k)

        for k in by:
            visit(k, set())

    async def expand_plan(self, **kwargs: Any) -> str | None:
        """PR9: apply PlanExpanded (see ``coscientist.core.expand.expand_plan``)."""
        from coscientist.core.expand import expand_plan as _expand

        return await _expand(self, **kwargs)  # type: ignore[arg-type]
