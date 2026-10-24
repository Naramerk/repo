"""Study create + plan accept (extracted to keep machine.py ≤ 500 lines)."""

from __future__ import annotations

def _or_empty_str(value: object) -> str:
    if value is None:
        return ""  # audit-ok: structural absent → empty str
    if not isinstance(value, str):
        return str(value)  # audit-ok: coerce after None gate for wire display
    return value


def _or_zero_int(value: object) -> int:
    if value is None:
        return 0  # audit-ok: structural absent → 0
    if type(value) is bool or not isinstance(value, int):
        raise TypeError(f"expected int|None, got {type(value).__name__}")
    return value



_DEFAULT_MIN_PLAN_COST = 0
_DEFAULT_ROLLUP = "all"
_DEFAULT_REVISION = 1
_EMPTY_LIST = []
_EMPTY_DICT = {}
_CRITERION_ALPHA = 0.05


def _or_empty_list(value: object) -> list:
    if value is None:
        return []  # audit-ok: structural absent → empty list
    if isinstance(value, list):
        return value
    return list(value)  # type: ignore[arg-type]


def _or_empty_dict(value: object) -> dict:
    if value is None:
        return {}  # audit-ok: structural absent → empty dict
    if isinstance(value, dict):
        return value
    raise TypeError(f"expected dict|None, got {type(value).__name__}")

from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

import coscientist.core.finalize.finalize as finalize_mod
from coscientist.core.claims.graph import GraphNode
from coscientist.core.events.clock import Clock
from coscientist.core.events.errors import CASUnavailable, Conflict, LeaseLost, SandboxInfraError
from coscientist.core.events.events import (
    EventEnvelope,
    make_idem,
)
from coscientist.core.events.ids import criterion_node_id, plan_id, question_node_id, study_id, task_id
from coscientist.core.events.ports import EventStore
from coscientist.core.events.schema_versions import (
    GRAPH_EDGE_ADDED_SCHEMA_VERSION,
    GRAPH_NODE_UPSERTED_SCHEMA_VERSION,
    PLAN_ACCEPTED_SCHEMA_VERSION,
    STUDY_CREATED_SCHEMA_VERSION,
    STUDY_REFUSED_SCHEMA_VERSION,
    TASK_CREATED_SCHEMA_VERSION,
    TASK_DONE_SCHEMA_VERSION,
)
from coscientist.core.execution.domain import (
    STAGE_DEP_KEYS,
    STAGE_TASK_KEYS,
    Budget,
    HitlPolicy,
    Plan,
    PlanRole,
    QuestionSpec,
    Route,
    Study,
    StudyConfig,
    Task,
    TaskKind,
    TaskOutcome,
    TaskSpec,
    TaskState,
    validate_budget_for_create,
)
from coscientist.core.execution.inputs import validate_input_specs, validate_plan_criteria
from coscientist.core.plan.intake import IntakeAccept, IntakeRefuse, SafetyPort, intake
from coscientist.core.plan.question_tasks import questions_from_spec_output

# Infra / fencing — must not be swallowed by intake fail-closed refuse.
_INTAKE_RERAISE = (LeaseLost, CASUnavailable, Conflict, SandboxInfraError)

STAGE_KEYS = STAGE_TASK_KEYS

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
        config: StudyConfig,
        hitl_policy: HitlPolicy,
        deadline: datetime,
        parent_study_id: str | None = None,
        sid: str | None = None,
        budget: Budget | None = None,
        inputs: list[Any] | None = None,
        min_plan_cost: Decimal | float | int = _DEFAULT_MIN_PLAN_COST,
        safety: SafetyPort | None = None,
        as_of: datetime | None = None,
        input_limits: Any | None = None,
        client_input_limits: Any | None = None,
        intake_refuse: IntakeRefuse | None = None,
        user_put_capability: Any | None = None,
    ) -> str:
        """Create a study. Domain numbers come only from ``config`` / ``hitl_policy``.

        No silent defaults for alpha / max_tasks / HITL timeouts — callers
        (API body or ``tests.helpers``) must pass a typed ``StudyConfig`` and
        an explicit ``HitlPolicy`` with concrete gate timeouts.
        """
        if not isinstance(config, StudyConfig):
            raise TypeError(
                f"create_study requires StudyConfig, got {type(config).__name__}"
            )
        if not isinstance(hitl_policy, HitlPolicy):
            raise TypeError(
                f"create_study requires HitlPolicy, got {type(hitl_policy).__name__}"
            )
        if not isinstance(deadline, datetime):
            raise TypeError(
                f"create_study requires datetime deadline, got {type(deadline).__name__}"
            )
        sid = sid or self.store.study_id or study_id()
        now = self.clock.now()
        dl = deadline
        finalize_reserve_s = config.finalize_reserve_s
        confirmations_needed = config.confirmations_needed
        refutations_needed = config.refutations_needed
        refutation_priority = config.refutation_priority
        supported_needed = config.supported_needed
        aggregate = config.aggregate
        evalue_alpha = config.evalue_alpha
        max_expansions = config.max_expansions
        max_human_expansions = config.max_human_expansions
        replan_reserve = config.replan_reserve
        max_tasks = config.max_tasks
        max_depth = config.max_depth
        plan_timeout_s = config.plan_timeout_s
        max_foreach_children = config.max_foreach_children
        max_loop_iter = config.max_loop_iter
        max_task_budget_share = config.max_task_budget_share
        max_proposals = config.max_proposals
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
        if not (0.0 < evalue_alpha <= 1.0):
            raise ValueError(f"evalue_alpha out of range: {evalue_alpha}")
        if max_expansions < 0:
            raise ValueError(f"max_expansions must be >= 0, got {max_expansions}")
        if max_human_expansions < 0:
            raise ValueError(f"max_human_expansions must be >= 0, got {max_human_expansions}")
        if not (0.0 <= replan_reserve <= 1.0):
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
        if not (0.0 <= max_task_budget_share <= 1.0):
            raise ValueError(f"max_task_budget_share out of range: {max_task_budget_share}")
        if max_proposals < 0:
            raise ValueError(f"max_proposals must be >= 0, got {max_proposals}")
        validate_budget_for_create(budget)  # delegated_costly ≤ max_cost_rub
        duration_s = (dl - now).total_seconds()
        from coscientist.core.claims.reasons import RefusalKind
        from coscientist.core.execution._ingest_gate import (
            UserInputRegistrationError,
            register_user_input_artifacts,
        )
        from coscientist.core.execution.input_limits import (
            default_input_limits,
            merge_input_limits,
        )

        # Profile owns caps; optional client_input_limits may only LOWER.
        profile_limits = (
            input_limits if input_limits is not None else default_input_limits()
        )
        decision: IntakeAccept | IntakeRefuse
        limits = profile_limits
        # Structural: any escape from intake validation → Refuse + StudyFinalized
        # (never a bare 500 without journal).
        if intake_refuse is not None:
            decision = intake_refuse
        else:
            try:
                merged = merge_input_limits(profile_limits, client_input_limits)
                if isinstance(merged, tuple):
                    refuse_kind, refuse_reason = merged
                    decision = IntakeRefuse(refuse_kind, str(refuse_reason)[:200])
                else:
                    limits = merged
                    decision = await intake(
                        request,
                        _or_empty_list(inputs),
                        profile,
                        budget,
                        min_plan_cost=min_plan_cost,
                        safety=safety,
                        duration_s=duration_s,
                        finalize_reserve_s=finalize_reserve_s,
                        input_limits=limits,
                    )
            except _INTAKE_RERAISE:
                raise
            except Exception as e:  # noqa: BLE001  # audit-ok: fail-closed intake → StudyFinalized refuse
                decision = IntakeRefuse(
                    RefusalKind.INPUT_INVALID,
                    f"intake_error:{type(e).__name__}"[:200],
                )
                limits = profile_limits
        user_ids: list[str] = []  # audit-ok: structural empty = no user inputs yet
        user_events: list[EventEnvelope] = []  # audit-ok: structural empty = no user events yet
        if not isinstance(decision, IntakeRefuse) and inputs:
            try:
                user_ids, user_events = register_user_input_artifacts(
                    list(inputs),
                    study_id=sid,
                    now=now,
                    artifacts=self.artifacts,
                    capability=user_put_capability,
                )
            except _INTAKE_RERAISE:
                raise
            except UserInputRegistrationError as e:
                # Fail-closed: never Accept with silently dropped inputs.
                decision = IntakeRefuse(
                    e.kind
                    if isinstance(e.kind, RefusalKind)
                    else RefusalKind.INPUT_INVALID,
                    _or_empty_str(e.reason)[:200],
                )
                user_ids, user_events = [], []
            except Exception as e:  # noqa: BLE001  # audit-ok: fail-closed register → StudyFinalized refuse
                decision = IntakeRefuse(
                    RefusalKind.INPUT_INVALID,
                    f"register_error:{type(e).__name__}"[:200],
                )
                user_ids, user_events = [], []
        study = Study(
            study_id=sid,
            profile=profile,
            request=request,
            catalog_version=catalog_version,
            hitl_policy=hitl_policy,
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
            evalue_alpha=evalue_alpha,
            max_expansions=max_expansions,
            max_human_expansions=max_human_expansions,
            replan_reserve=replan_reserve,
            max_tasks=max_tasks,
            max_depth=max_depth,
            plan_timeout_s=plan_timeout_s,
            max_foreach_children=max_foreach_children,
            max_loop_iter=max_loop_iter,
            max_task_budget_share=max_task_budget_share,
            as_of=as_of,
            user_input_ids=list(user_ids),
            max_proposals=max_proposals,
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
                schema_version=GRAPH_NODE_UPSERTED_SCHEMA_VERSION,
                ),
        ]
        events.extend(user_events)
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
                    schema_version=STUDY_REFUSED_SCHEMA_VERSION,
)
            )
            await self._append(events)
            self.store.study_id = sid
            # Via module attr so tests can monkeypatch finalize.finalize.
            await finalize_mod.finalize(self, trigger="intake")  # type: ignore[arg-type]
            return sid
        plan_deadline = min(now + timedelta(seconds=plan_timeout_s), effective)
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
                route=Route(kind.value),
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
                plan_role=PlanRole.BOOTSTRAP if kind == TaskKind.PLAN else PlanRole.NONE,
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
        rollup: str = _DEFAULT_ROLLUP,
        revision: int = _DEFAULT_REVISION,
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
        if not (self.study is not None):
            raise RuntimeError('assertion failed: self.study is not None')
        sid = self.study.study_id
        now = self.clock.now()
        from pydantic import ValidationError

        from coscientist.core.claims.graph import GraphEdge
        from coscientist.core.claims.rules import PlanInvalid
        from coscientist.core.plan.decomposes import assert_decomposes_acyclic, parent_map_from_questions
        from coscientist.core.plan.foreach import validate_foreach_specs
        from coscientist.core.plan.loop import is_loop_dep, normalize_loop_id, validate_loop_specs
        from coscientist.core.plan.plan_validate import (
            PLAN_REFUSED_LINE,
            apply_plan_refused,
            freeze_plan_rubrics,
            merge_plan_questions,
            refuse_detail,
        )
        from coscientist.core.plan.shares import critic_plan_shares

        LOOP_PLAN_REFUSED_LINE = "не выполнено: план отклонён (LoopSpec)"
        named_subs: list[QuestionSpec] = []  # audit-ok: structural empty = collecting named subquestions
        # All plan validation → PlanRefused (never raw ValueError to callers).
        raw_loops = _or_empty_list(loops)
        loop_specs: list = []  # audit-ok: structural empty = collecting LoopSpecs
        try:
            tasks = [
                t if isinstance(t, TaskSpec) else TaskSpec.model_validate(t)
                for t in _or_empty_list(tasks)
            ]
            if questions is None and spec_output is not None:
                questions = [
                    QuestionSpec.model_validate(q)
                    for q in questions_from_spec_output(spec_output)
                ]
            questions = _or_empty_list(questions)
            raw_subs = [
                s if isinstance(s, QuestionSpec) else QuestionSpec.model_validate(s)
                for s in _or_empty_list(subquestions)
            ]
            questions, named_subs = merge_plan_questions(questions, raw_subs)
            from coscientist.core.plan.decomposes import stamp_question_origins

            questions = stamp_question_origins(
                questions, named_sub_ids={s.id for s in named_subs}
            )
            named_subs = [
                q for q in questions if q.parent_id
            ] or named_subs
            expand_after = _or_empty_list(expand_after)
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
            graph = self.graph
            known_hyps: set[str] = set()
            if graph is not None:
                for node in graph.nodes_by_kind("hypothesis"):
                    hid = _or_empty_str(node.ref_id)
                    if hid:
                        known_hyps.add(hid)
            loop_specs = validate_loop_specs(
                raw_loops,
                known_keys=known_keys,
                max_loop_iter=self.study.max_loop_iter,
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
            catalog = self.catalog
            if catalog is None:
                executor = self.executor
                catalog = None if executor is None else executor.catalog
            validate_plan_criteria(tasks, catalog)
            tasks, questions = freeze_plan_rubrics(
                tasks,
                questions,
                catalog=catalog,
                subquestions=named_subs,
                rollup=rollup,
                max_depth=self.study.max_depth,
                max_tasks=self.study.max_tasks,
            )
            assert_decomposes_acyclic(parent_map_from_questions(questions))
            loop_ids = {ls.loop_id for ls in loop_specs}
            # NFKC-normalize loop:<id> barriers and store normalized deps on tasks.
            rewritten: list[TaskSpec] = []  # audit-ok: structural empty = rewritten TaskSpecs
            for t in tasks:
                new_deps: list[str] = []  # audit-ok: structural empty = building depends_on
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
                detail=refuse_detail(e, truncate=False),
                report_line=LOOP_PLAN_REFUSED_LINE,
            )
        except (ValueError, ValidationError) as e:
            line = LOOP_PLAN_REFUSED_LINE if raw_loops else PLAN_REFUSED_LINE
            return await apply_plan_refused(  # audit-ok: fail-closed PlanRefused on validation error
                self, detail=refuse_detail(e, truncate=False), report_line=line
            )
        except TypeError as e:
            # LoopSpec pydantic / type errors at accept → same refuse path.
            if raw_loops:
                return await apply_plan_refused(
                    self,
                    detail=refuse_detail(e, truncate=False),
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
        graph = self.graph
        for q in questions:
            qnid = question_node_id(sid, q.id)
            if graph is not None and graph.get_node(qnid) is not None:
                continue  # never clobber existing attrs
            q_attrs: dict[str, Any] = {
                "formulation": q.text,
                "final_type": q.final_type,
            }
            if q.parent_id:
                q_attrs["parent_id"] = q.parent_id
            if q.origin:
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
                    schema_version=GRAPH_NODE_UPSERTED_SCHEMA_VERSION,
                )
            )
        # PR16: decomposes edges on accept (child → parent); acyclicity already checked.
        for q in questions:
            parent = q.parent_id
            if not parent:
                continue
            qnid = question_node_id(sid, q.id)
            pn = question_node_id(sid, str(parent))
            eid = f"decomposes:{qnid}:{pn}"
            origin = q.origin
            if origin is None:
                origin = 'explicit'  # audit-ok: structural absent default
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
                    schema_version=GRAPH_EDGE_ADDED_SCHEMA_VERSION,
                )
            )
        # Freeze rubric digests onto criterion-like nodes (frozen_at_seq via projection).
        seen_rubrics: set[str] = set()
        for spec in tasks:
            for o in spec.expected_outputs:
                rub = o.rubric
                if rub is None:
                    continue
                dig = _or_empty_str(rub.digest)
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
                        schema_version=GRAPH_NODE_UPSERTED_SCHEMA_VERSION,
                )
                )
        # Freeze plan criteria at accept (DESIGN_M1 §6 / frozen_at_seq).
        seen_criteria: set[str] = set()
        for spec in tasks:
            for ref in spec.criteria:
                key = ref.template_id
                if not key or key in seen_criteria:
                    continue
                seen_criteria.add(key)
                cnid = criterion_node_id(sid, key)
                if graph is not None and graph.get_node(cnid) is not None:
                    existing = graph.get_node(cnid)
                    if existing is not None and (
                        existing.attrs.get("frozen_at_seq") is not None
                        or existing.attrs.get("frozen") is True
                    ):
                        continue
                # SESOI / predicts_null from catalog template only — never params.
                # Frozen criteria always stamp uses_sesoi / predicts_null /
                # predicted_direction (executor fail-closed requires them).
                cattrs: dict[str, Any] = {"frozen": True, "template_id": key}
                cattrs["alpha"] = _CRITERION_ALPHA
                if catalog is not None:
                    by_id = {t.id: t for t in catalog.templates}
                    tmpl = by_id.get(key)
                    if tmpl is not None:
                        uses = tmpl.uses_sesoi
                        cattrs["uses_sesoi"] = uses
                        cattrs["predicts_null"] = tmpl.predicts_null
                        cattrs["predicted_direction"] = tmpl.predicted_direction
                        if uses and tmpl.sesoi is not None:
                            cattrs["sesoi"] = tmpl.sesoi
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
                        schema_version=GRAPH_NODE_UPSERTED_SCHEMA_VERSION,
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
                    schema_version=TASK_DONE_SCHEMA_VERSION,
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
                plan_role=PlanRole.NONE,
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
        from coscientist.core.plan.foreach import materialize_ready_foreach
        from coscientist.core.plan.loop import maybe_advance_loops

        await materialize_ready_foreach(self)  # type: ignore[arg-type]
        await maybe_advance_loops(self)  # type: ignore[arg-type]
        return pid

    async def _known_question_texts(self) -> dict[str, str]:
        texts: dict[str, str] = {}  # audit-ok: structural empty = collecting question texts
        local = self.plans
        if isinstance(local, dict):
            plans = list(local.values())
        else:
            plans = []  # audit-ok: structural absent plans bag
        if not plans:
            store = self.store
            if store is not None:
                plans = await store.get_projection("plans")
        for plan in plans:
            if "questions" not in plan:
                continue
            raw_qs = plan["questions"]
            if not isinstance(raw_qs, list):
                raise ValueError("plan.questions must be list")
            for q in raw_qs:
                if not isinstance(q, dict):
                    continue
                if "id" not in q:
                    raise ValueError("plan question missing id")
                qid = _or_empty_str(q["id"])
                if not qid:
                    continue
                if "text" not in q:
                    raise ValueError(f"plan question {qid!r} missing text")
                text = _or_empty_str(q["text"])
                if qid in texts and texts[qid] != text:
                    raise ValueError(f"question id {qid!r} text conflict")
                texts[qid] = text
        return texts

    @staticmethod
    def _validate_questions(questions: list[QuestionSpec], known_texts: dict[str, str]) -> None:
        seen: dict[str, str] = {}  # audit-ok: structural empty = collecting seen question ids
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
        from coscientist.core.plan.loop import is_loop_dep

        keys = {t.task_key for t in tasks}
        for t in tasks:
            for d in t.depends_on:
                # Allow stage keys, peers in this revision, and loop:<id> barriers.
                # Expand revisions may depend on keys that only exist as runtime
                # tasks (already DONE) — callers pass those via merged still_kept
                # (including PLAN-n task_keys). No string-prefix classification.
                if d in keys or d in STAGE_DEP_KEYS or is_loop_dep(d):
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
                if is_loop_dep(d):
                    continue
                visit(d, stack)
            stack.remove(k)
            seen.add(k)

        for k in by:
            visit(k, set())

    async def expand_plan(self, **kwargs: Any) -> str | None:
        """PR9: apply PlanExpanded (see ``coscientist.core.plan.expand.expand_plan``)."""
        from coscientist.core.plan.expand import expand_plan as _expand

        return await _expand(self, **kwargs)  # type: ignore[arg-type]

    async def dispose_question(self, disposal: Any) -> str | None:
        """PR15: explicit needs_more_data|infeasible → QuestionDisposed."""
        from coscientist.core.claims.dispose import dispose_question as _dispose

        return await _dispose(self, disposal)
