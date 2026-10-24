"""Study create + plan accept (extracted to keep machine.py ≤ 500 lines)."""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

import coscientist.core.finalize.finalize as finalize_mod
from coscientist.core.claims.graph import GraphNode
from coscientist.core.events.clock import Clock
from coscientist.core.events.errors import CASUnavailable, CatalogError, Conflict, LeaseLost, SandboxInfraError
from coscientist.core.events.events import (
    PLAN_ACCEPTED_SCHEMA_VERSION,
    STUDY_CREATED_SCHEMA_VERSION,
    STUDY_REFUSED_SCHEMA_VERSION,
    TASK_CREATED_SCHEMA_VERSION,
    EventEnvelope,
    make_idem,
)
from coscientist.core.events.ids import criterion_node_id, plan_id, question_node_id, study_id, task_id
from coscientist.core.events.ports import EventStore
from coscientist.core.events.upcasters import writer_schema_version
from coscientist.core.execution.domain import (
    Budget,
    HitlPolicy,
    Need,
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
from coscientist.core.execution.inputs import validate_input_specs, validate_plan_criteria
from coscientist.core.plan.intake import IntakeAccept, IntakeRefuse, SafetyPort, intake
from coscientist.core.plan.need import resolve_plan_needs
from coscientist.core.plan.question_tasks import questions_from_spec_output

# Infra / fencing — must not be swallowed by intake fail-closed refuse.
_INTAKE_RERAISE = (LeaseLost, CASUnavailable, Conflict, SandboxInfraError)

STAGE_KEYS = [
    ("SPEC", TaskKind.SPEC),
    ("HYP", TaskKind.HYPOTHESES),
    ("LIT", TaskKind.LITERATURE),
    ("PLAN", TaskKind.PLAN),
    ("REPORT", TaskKind.REPORT),
]


def _plan_accept_freeze_rows(
    *,
    sid: str,
    tasks: list[TaskSpec],
    catalog: Any,
    graph: Any,
) -> tuple[list[tuple[str, str, dict[str, Any]]], list[tuple[str, str, dict[str, Any]]]]:
    """Build rubric + criterion freeze attr rows for PlanAccepted GraphNodeUpserted.

    Raises ``CatalogError`` when KindSpec-required metric/op/target/check cannot
    be taken from Rubric / catalog CodeCheck|ReviewCheck / RequirementRef.params
    (no invent, no schema weaken).
    """
    from coscientist.core.execution.domain import CodeCheck, ReviewCheck

    rubric_rows: list[tuple[str, str, dict[str, Any]]] = []
    seen_rubrics: set[str] = set()
    for spec in tasks:
        outs = spec.expected_outputs
        if outs is None:
            continue
        for o in outs:
            rub = getattr(o, "rubric", None)
            if rub is None:
                continue
            dig = getattr(rub, "digest", None)
            if not isinstance(dig, str) or not dig or dig in seen_rubrics:
                continue
            aggregate = getattr(rub, "aggregate", None)
            if not isinstance(aggregate, str) or not aggregate:
                raise CatalogError(f"rubric_freeze_aggregate_required:{dig}")
            seen_rubrics.add(dig)
            rnid = criterion_node_id(sid, f"rubric:{dig}")
            if graph is not None and graph.get_node(rnid) is not None:
                continue
            # PR13 qualitative: check/affects_stance structural; target from Rubric;
            # metric = frozen digest identity (real Rubric field, not invented).
            rubric_rows.append(
                (
                    rnid,
                    dig,
                    {
                        "frozen": True,
                        "template_id": f"rubric:{dig}",
                        "rubric_digest": dig,
                        "check": "rubric",
                        "affects_stance": False,
                        "metric": dig,
                        "op": "==",
                        "target": aggregate,
                    },
                )
            )

    criterion_rows: list[tuple[str, str, dict[str, Any]]] = []
    seen_criteria: set[str] = set()
    by_id: dict[str, Any] = {}
    if catalog is not None:
        raw_tmpls = getattr(catalog, "templates", None)
        if isinstance(raw_tmpls, list):
            by_id = {t.id: t for t in raw_tmpls}
    for spec in tasks:
        crit_list = spec.criteria
        if crit_list is None:
            continue
        for ref in crit_list:
            tid = getattr(ref, "template_id", None)
            key = str(tid) if tid is not None else str(ref)
            if not key or key in seen_criteria:
                continue
            seen_criteria.add(key)
            cnid = criterion_node_id(sid, key)
            if graph is not None and graph.get_node(cnid) is not None:
                existing = graph.get_node(cnid)
                if existing is not None:
                    raw_attrs = existing.attrs
                    ea: dict[str, Any] = (
                        dict(raw_attrs) if isinstance(raw_attrs, dict) else {}
                    )
                    if ea.get("frozen_at_seq") is not None or bool(ea.get("frozen")):
                        continue
            cattrs: dict[str, Any] = {"frozen": True, "template_id": key}
            params = getattr(ref, "params", None)
            if isinstance(params, dict):
                for pk in ("metric", "op", "target"):
                    if pk not in params or params[pk] is None:
                        continue
                    if pk == "op":
                        cattrs[pk] = params[pk]
                    else:
                        cattrs[pk] = str(params[pk])
            tmpl = by_id.get(key)
            if tmpl is not None:
                raw_check = getattr(tmpl, "check", None)
                if isinstance(raw_check, ReviewCheck):
                    cattrs["check"] = "review"
                elif isinstance(raw_check, CodeCheck):
                    if raw_check.kind == "rubric":
                        cattrs["check"] = "rubric"
                    else:
                        cattrs["check"] = "code"
                elif raw_check is not None:
                    raise CatalogError(f"criterion_freeze_unknown_check_type:{key}")
                if raw_check is not None:
                    for pk in ("metric", "op", "target"):
                        if pk in cattrs:
                            continue
                        cv = getattr(raw_check, pk, None)
                        if cv is None:
                            continue
                        if pk == "op":
                            cattrs[pk] = cv
                        else:
                            cattrs[pk] = str(cv)
                if getattr(tmpl, "uses_sesoi", False) and tmpl.sesoi is not None:
                    cattrs["uses_sesoi"] = True
                    cattrs["sesoi"] = float(tmpl.sesoi)
                    pred = getattr(tmpl, "predicted_direction", None)
                    if pred is not None:
                        cattrs["predicted_direction"] = str(pred)
                else:
                    cattrs["uses_sesoi"] = False
                if getattr(tmpl, "predicts_null", None) is not None:
                    cattrs["predicts_null"] = bool(tmpl.predicts_null)
            if cattrs.get("check") not in ("code", "review", "rubric"):
                raise CatalogError(f"criterion_freeze_check_required:{key}")
            missing = [
                pk
                for pk in ("metric", "op", "target")
                if pk not in cattrs or cattrs[pk] is None
            ]
            if missing:
                raise CatalogError(
                    f"criterion_freeze_incomplete:{key}:missing_" + ",".join(missing)
                )
            criterion_rows.append((cnid, key, cattrs))
    return rubric_rows, criterion_rows


class LifecycleMixin:
    """Attribute stubs so mypy accepts mixin use on StudyRuntime."""

    store: EventStore
    clock: Clock
    study: Study | None
    tasks: dict[str, Task]
    _intake_refusal: IntakeRefuse | None

    async def append_events(self, events: list[EventEnvelope]) -> None:
        raise NotImplementedError

    async def refuse_study_without_catalog(
        self,
        *,
        kind: Any,
        reason: str,
        sid: str | None = None,
        classifier_error: bool = False,
    ) -> str:
        """Journal StudyRefused(catalog_version=null) + StudyFinalized (no StudyCreated).

        Used when POST /studies refuses without a client StrictInt catalog_version.
        Accepted Study / StudyCreated require StrictInt — never invent one here.
        """
        from coscientist.core.claims.reasons import RefusalKind

        sid = sid or self.store.study_id or study_id()
        now = self.clock.now()
        refuse_kind = kind if isinstance(kind, RefusalKind) else RefusalKind(kind)
        decision = IntakeRefuse(
            refuse_kind, str(reason)[:200], classifier_error=classifier_error
        )
        events = [
            EventEnvelope(
                study_id=sid,
                type="StudyRefused",
                entity_key=sid,
                idem_key=make_idem("StudyRefused", sid),
                payload={
                    "kind": decision.kind.value,
                    "reason": decision.reason,
                    "classifier_error": decision.classifier_error,
                    "catalog_version": None,
                },
                ts=now,
                schema_version=STUDY_REFUSED_SCHEMA_VERSION,
            )
        ]
        await self.append_events(events)
        self.store.study_id = sid
        self._intake_refusal = decision
        await finalize_mod.finalize(self, trigger="intake")  # type: ignore[arg-type]
        return sid

    async def create_study(
        self,
        *,
        profile: str,
        request: str,
        catalog_version: int,
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
        max_proposals: int = 3,
        require_hypotheses: bool,
        inputs: list[Any] | None = None,
        min_plan_cost: Decimal | float | int = 0,
        safety: SafetyPort | None = None,
        as_of: datetime | None = None,
        input_limits: Any | None = None,
        client_input_limits: Any | None = None,
        intake_refuse: IntakeRefuse | None = None,
        user_put_capability: Any | None = None,
    ) -> str:
        sid = sid or self.store.study_id or study_id()
        # Accepted Study / StudyCreated require StrictInt — never None / bool / coerce.
        if catalog_version is None or isinstance(catalog_version, bool) or not isinstance(
            catalog_version, int
        ):
            raise CatalogError(
                "create_study:catalog_version_must_be_strict_int_got_"
                f"{type(catalog_version).__name__}:{catalog_version!r}"
            )
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
                        list(inputs or []),
                        profile,
                        budget,
                        min_plan_cost=min_plan_cost,
                        safety=safety,
                        duration_s=duration_s,
                        finalize_reserve_s=float(finalize_reserve_s),
                        input_limits=limits,
                    )
            except _INTAKE_RERAISE:
                raise
            except Exception as e:  # noqa: BLE001 — fail-closed to StudyFinalized
                decision = IntakeRefuse(
                    RefusalKind.INPUT_INVALID,
                    f"intake_error:{type(e).__name__}"[:200],
                )
                limits = profile_limits
        user_ids: list[str] = []
        user_events: list[EventEnvelope] = []
        if not isinstance(decision, IntakeRefuse) and inputs:
            try:
                user_ids, user_events = register_user_input_artifacts(
                    list(inputs),
                    study_id=sid,
                    now=now,
                    artifacts=getattr(self, "artifacts", None),
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
                    str(e.reason)[:200],
                )
                user_ids, user_events = [], []
            except Exception as e:  # noqa: BLE001 — registration bugs → refuse
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
            as_of=as_of,
            user_input_ids=list(user_ids),
            max_proposals=max_proposals,
            require_hypotheses=require_hypotheses,
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
                    attrs={
                        "request": study.request,
                        "goal": {"statement": study.request},
                        "profile": profile,
                        "catalog_version": str(study.catalog_version),
                    },
                    created_at=now,
                ).model_dump(mode="json"),
                ts=now, schema_version=writer_schema_version("GraphNodeUpserted"),
            ),
        ]
        events.extend(user_events)
        if isinstance(decision, IntakeRefuse):
            # Same transaction as StudyCreated so crash/reopen keeps refusal.
            # StudyRefused.catalog_version mirrors the StrictInt on StudyCreated.
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
                        "catalog_version": catalog_version,
                    },
                    ts=now,
                    schema_version=STUDY_REFUSED_SCHEMA_VERSION,
                )
            )
            await self.append_events(events)
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
        await self.append_events(events)
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
        hypotheses: Need | None = None,
        literature: Need | None = None,
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
        named_subs: list[QuestionSpec] = []
        # All plan validation → PlanRefused (never raw ValueError to callers).
        raw_loops = list(loops or [])
        loop_specs: list = []
        plan_refuse = None
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
            from coscientist.core.plan.decomposes import stamp_question_origins

            questions = stamp_question_origins(
                questions, named_sub_ids={s.id for s in named_subs}
            )
            named_subs = [
                q for q in questions if getattr(q, "parent_id", None)
            ] or named_subs
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
                if changed:
                    rewritten.append(
                        t.model_copy(update={"depends_on": new_deps})
                    )
                else:
                    rewritten.append(t)
            tasks = rewritten
            # Freeze inside try → CatalogError becomes PlanRefused (not crash).
            from coscientist.checks.sandbox_limits import freeze_task_sandbox_limits

            tasks = [freeze_task_sandbox_limits(spec, catalog) for spec in tasks]
            # G4: omit → PlanNeedRefuse (PlanNeedRefuse ⊂ ValueError → PlanRefused).
            hyp_need, lit_need = resolve_plan_needs(
                hypotheses=hypotheses,
                literature=literature,
                require_hypotheses=self.study.require_hypotheses,
            )
            # Criterion / rubric GraphNode attrs built here so CatalogError maps
            # to PlanRefused via plan_refuse below (no return-in-except).
            rubric_freeze_rows, criterion_freeze_rows = (
                _plan_accept_freeze_rows(
                    sid=sid,
                    tasks=tasks,
                    catalog=catalog,
                    graph=getattr(self, "graph", None),
                )
            )
        except PlanInvalid as e:
            plan_refuse = (refuse_detail(e), LOOP_PLAN_REFUSED_LINE)
        except CatalogError as e:
            plan_refuse = (refuse_detail(e), PLAN_REFUSED_LINE)
        except (ValueError, ValidationError) as e:
            if raw_loops:
                plan_refuse = (refuse_detail(e), LOOP_PLAN_REFUSED_LINE)
            else:
                plan_refuse = (refuse_detail(e), PLAN_REFUSED_LINE)
        except TypeError as e:
            # LoopSpec pydantic / type errors at accept → same refuse path.
            if not raw_loops:
                raise
            plan_refuse = (refuse_detail(e), LOOP_PLAN_REFUSED_LINE)
        if isinstance(plan_refuse, tuple):
            detail, report_line = plan_refuse
            return await apply_plan_refused(
                self, detail=detail, report_line=report_line
            )
        # try-success path always binds these (mypy: narrow after refuse return).
        assert isinstance(questions, list)
        assert isinstance(expand_after, list)
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
            hypotheses=hyp_need,
            literature=lit_need,
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
            final_type = getattr(q, "final_type", None)
            if final_type is None:
                final_type = "verdict"
            q_attrs: dict[str, Any] = {
                "formulation": q.text,
                "final_type": final_type,
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
            origin = getattr(q, "origin", None)
            if origin is None:
                origin = "explicit"
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
        # Freeze rubric digests onto criterion nodes (PR13 / DESIGN_M1 §6.2).
        for rnid, dig, rattrs in rubric_freeze_rows:
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
                        attrs=rattrs,
                        created_at=now,
                    ).model_dump(mode="json"),
                    ts=now,
                    schema_version=1,
                )
            )
        # Freeze plan criteria at accept (DESIGN_M1 §6 / frozen_at_seq).
        for cnid, key, cattrs in criterion_freeze_rows:
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
        # tasks already freeze_task_sandbox_limits'd inside the validation try.
        for frozen_spec in tasks:
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
        await self.append_events(events)
        # B1: foreach templates whose of_task is already DONE (e.g. HYP-0).
        from coscientist.core.plan.foreach import materialize_ready_foreach
        from coscientist.core.plan.loop import maybe_advance_loops

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
        """PR9: apply PlanExpanded (see ``coscientist.core.plan.expand.expand_plan``)."""
        from coscientist.core.plan.expand import expand_plan as _expand

        return await _expand(self, **kwargs)  # type: ignore[arg-type]

    async def dispose_question(self, disposal: Any) -> str | None:
        """PR15: explicit needs_more_data|infeasible → QuestionDisposed."""
        from coscientist.core.claims.dispose import dispose_question as _dispose

        return await _dispose(self, disposal)
