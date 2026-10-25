"""Study create + plan accept (extracted to keep machine.py ≤ 500 lines)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from coscientist.core.execution.executor import Executor

import coscientist.core.finalize.finalize as finalize_mod
from coscientist.core.claims.graph import (
    GraphNode,
    GraphSchemaError,
    GraphStore,
    default_execution_registry,
    required_attr_names,
)
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
    Catalog,
    CodeCheck,
    HitlPolicy,
    LoopSpec,
    Need,
    OutputSpec,
    Plan,
    QuestionSpec,
    RequirementRef,
    RequirementTemplate,
    ReviewCheckMeasured,
    ReviewCheckPlain,
    Study,
    Task,
    TaskKind,
    TaskOutcome,
    TaskSpec,
    TaskState,
    validate_budget_for_create,
)
from coscientist.core.execution.input_limits import UserInput
from coscientist.core.execution.inputs import validate_input_specs, validate_plan_criteria
from coscientist.core.plan.continue_session import study_kind_attrs
from coscientist.core.plan.expand_validate import task_kind_for_route
from coscientist.core.plan.intake import IntakeAccept, IntakeRefuse, SafetyPort, intake
from coscientist.core.plan.need import assert_hyp_n_when_needed, resolve_plan_needs
from coscientist.core.plan.question_tasks import questions_from_spec_output

# Infra / fencing — must not be swallowed by intake fail-closed refuse.
_INTAKE_RERAISE = (LeaseLost, CASUnavailable, Conflict, SandboxInfraError)

# Catalog template that gates SPEC SUCCESS via artifact_exists (single source for output name).
_ARTIFACT_PRESENT_TEMPLATE_ID = "artifact_present"


def _artifact_present_output_name(catalog: Catalog) -> str:
    """``artifact_present`` / ``CodeCheck.output`` from the bound catalog (no literals)."""
    tmpl = None
    for candidate in catalog.templates:
        if candidate.id == _ARTIFACT_PRESENT_TEMPLATE_ID:
            tmpl = candidate
            break
    if tmpl is None:
        raise CatalogError(
            f"create_study:catalog_missing_template:{_ARTIFACT_PRESENT_TEMPLATE_ID}"
        )
    check = tmpl.check
    if not isinstance(check, CodeCheck):
        raise CatalogError(
            f"create_study:{_ARTIFACT_PRESENT_TEMPLATE_ID}_check_must_be_CodeCheck"
        )
    if check.kind != "artifact_exists":
        raise CatalogError(
            f"create_study:{_ARTIFACT_PRESENT_TEMPLATE_ID}_kind_must_be_artifact_exists_"
            f"got_{check.kind}"
        )
    name = check.output
    if type(name) is not str or not name.strip():
        raise CatalogError(
            f"create_study:{_ARTIFACT_PRESENT_TEMPLATE_ID}_output_must_be_non_empty_str"
        )
    return name

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
    catalog: Catalog | None,
    graph: GraphStore | None,
) -> tuple[
    list[tuple[str, str, dict[str, object]]],
    list[tuple[str, str, dict[str, object]]],
]:
    """Build rubric + criterion freeze attr rows for PlanAccepted GraphNodeUpserted.

    Requiredness comes from ``graph_schema`` KindSpec variants (via
    ``required_attr_names``) — no parallel waive/require list in this function.
    Raises ``CatalogError`` when authored Rubric / catalog check / params cannot
    supply those fields (no invent, no fail-open).
    """
    from coscientist.core.claims.rubric import freeze_rubric

    criterion_spec = default_execution_registry().require_node_kind("criterion")

    rubric_rows: list[tuple[str, str, dict[str, object]]] = []
    seen_rubrics: set[str] = set()
    for spec in tasks:
        outs = spec.expected_outputs
        if outs is None:
            continue
        for o in outs:
            rub = o.rubric
            if rub is None:
                continue
            dig = rub.digest
            if not isinstance(dig, str) or not dig:
                raise CatalogError(f"rubric_freeze_digest_required:output={o.name!r}")
            if dig in seen_rubrics:
                continue
            aggregate = rub.aggregate
            if not isinstance(aggregate, str) or not aggregate:
                raise CatalogError(f"rubric_freeze_aggregate_required:{dig}")
            seen_rubrics.add(dig)
            rnid = criterion_node_id(sid, f"rubric:{dig}")
            if graph is not None and graph.get_node(rnid) is not None:
                continue
            # PR13 qualitative (DESIGN_M1 §6.2): identity is rubric_digest;
            # KindSpec rubric+affects_stance=false does not require metric/op/target.
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
                    },
                )
            )

    criterion_rows: list[tuple[str, str, dict[str, object]]] = []
    seen_criteria: set[str] = set()
    by_id: dict[str, RequirementTemplate] = {}
    if catalog is not None:
        templates = catalog.templates
        if not isinstance(templates, list):
            raise CatalogError(f"criterion_freeze_catalog_templates_required:got_{type(templates).__name__}")
        by_id = {t.id: t for t in templates}
    for spec in tasks:
        crit_list = spec.criteria
        if crit_list is None:
            continue
        for ref in crit_list:
            if not isinstance(ref, RequirementRef):
                raise CatalogError(f"criterion_freeze_bad_ref:{type(ref).__name__}")
            key = ref.template_id
            if not isinstance(key, str) or not key:
                raise CatalogError("criterion_freeze_empty_template_id")
            if key in seen_criteria:
                continue
            seen_criteria.add(key)
            cnid = criterion_node_id(sid, key)
            if graph is not None:
                existing = graph.get_node(cnid)
                if existing is not None:
                    raw_attrs = existing.attrs
                    if not isinstance(raw_attrs, dict):
                        raise CatalogError(
                            f"criterion_freeze_existing_attrs_not_mapping:{key}:{type(raw_attrs).__name__}"
                        )
                    ea: dict[str, object] = dict(raw_attrs)
                    if ea.get("frozen_at_seq") is not None or bool(ea.get("frozen")):
                        continue
            cattrs: dict[str, object] = {"frozen": True, "template_id": key}
            params = ref.params
            if isinstance(params, dict):
                for pk in ("metric", "op", "target"):
                    if pk not in params or params[pk] is None:
                        continue
                    if pk == "op":
                        cattrs[pk] = params[pk]
                    else:
                        cattrs[pk] = str(params[pk])
            tmpl = by_id.get(key)
            if tmpl is not None and tmpl.exempt_from_freeze:
                # Catalog-authored: not a KindSpec freeze target (e.g. PLAN-n markers).
                continue
            if tmpl is not None:
                raw_check = tmpl.check
                if isinstance(raw_check, (ReviewCheckPlain, ReviewCheckMeasured)):
                    cattrs["check"] = "review"
                    if isinstance(raw_check, ReviewCheckMeasured):
                        if "metric" not in cattrs:
                            cattrs["metric"] = raw_check.metric
                        if "op" not in cattrs:
                            cattrs["op"] = raw_check.op
                        if "target" not in cattrs:
                            cattrs["target"] = raw_check.target
                elif isinstance(raw_check, CodeCheck):
                    if raw_check.kind == "rubric":
                        cattrs["check"] = "rubric"
                        cattrs["affects_stance"] = False
                        rub = raw_check.rubric
                        if rub is None:
                            raise CatalogError(f"criterion_freeze_rubric_required:{key}")
                        dig = freeze_rubric(rub).digest
                        if not isinstance(dig, str) or not dig:
                            raise CatalogError(f"criterion_freeze_rubric_digest_required:{key}")
                        cattrs["rubric_digest"] = dig
                    else:
                        cattrs["check"] = "code"
                    for pk in ("metric", "op", "target"):
                        if pk in cattrs:
                            continue
                        if pk == "metric":
                            cv = raw_check.metric
                        elif pk == "op":
                            cv = raw_check.op
                        else:
                            cv = raw_check.target
                        if cv is None:
                            continue
                        if pk == "op":
                            cattrs[pk] = cv
                        else:
                            cattrs[pk] = str(cv)
                else:
                    raise CatalogError(f"criterion_freeze_unknown_check_type:{key}")
                if tmpl.uses_sesoi and tmpl.sesoi is not None:
                    cattrs["uses_sesoi"] = True
                    cattrs["sesoi"] = float(tmpl.sesoi)
                    cattrs["predicted_direction"] = str(tmpl.predicted_direction)
                else:
                    cattrs["uses_sesoi"] = False
                cattrs["predicts_null"] = bool(tmpl.predicts_null)
            if cattrs.get("check") not in ("code", "review", "rubric"):
                raise CatalogError(f"criterion_freeze_check_required:{key}")
            try:
                required = required_attr_names("criterion", cattrs, criterion_spec)
            except GraphSchemaError as e:
                raise CatalogError(f"criterion_freeze_variant:{key}:{e}") from e
            missing = sorted(name for name in required if name not in cattrs)
            if missing:
                raise CatalogError(f"criterion_freeze_incomplete:{key}:missing_" + ",".join(missing))
            criterion_rows.append((cnid, key, cattrs))
    return rubric_rows, criterion_rows


class LifecycleMixin:
    """Attribute stubs so mypy accepts mixin use on StudyRuntime."""

    store: EventStore
    clock: Clock
    study: Study | None
    tasks: dict[str, Task]
    graph: GraphStore
    catalog: Catalog
    plans: dict[str, dict[str, Any]]
    executor: Executor | None
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
        decision = IntakeRefuse(refuse_kind, str(reason)[:200], classifier_error=classifier_error)
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
        confirmations_needed: int,
        refutations_needed: int,
        refutation_priority: bool,
        supported_needed: int,
        aggregate: str = "count",
        evalue_alpha: float = 0.05,
        max_expansions: int,
        max_attempts: int,
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
        inputs: Sequence[UserInput | Mapping[str, object] | bytes | str],
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
        if catalog_version is None or isinstance(catalog_version, bool) or not isinstance(catalog_version, int):
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
        if type(max_attempts) is not int or isinstance(max_attempts, bool) or max_attempts < 1:
            raise ValueError(f"max_attempts must be int >= 1, got {max_attempts!r}")
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
        if input_limits is not None:
            profile_limits = input_limits
        else:
            profile_limits = default_input_limits()
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
                    intake_inputs = list(inputs)
                    decision = await intake(
                        request,
                        intake_inputs,
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
                if isinstance(e.kind, RefusalKind):
                    decision = IntakeRefuse(e.kind, str(e.reason)[:200])
                else:
                    decision = IntakeRefuse(RefusalKind.INPUT_INVALID, str(e.reason)[:200])
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
            max_attempts=max_attempts,
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
                    attrs=study_kind_attrs(study),
                    created_at=now,
                ).model_dump(mode="json"),
                ts=now,
                schema_version=writer_schema_version("GraphNodeUpserted"),
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
        catalog = getattr(self, "catalog", None)
        if not isinstance(catalog, Catalog):
            raise CatalogError("create_study:catalog_required_for_stage_outputs")
        spec_output_name = _artifact_present_output_name(catalog)
        for key, kind in STAGE_KEYS:
            if kind == TaskKind.REPORT:
                deps = ["PLAN"]
            elif kind == TaskKind.SPEC:
                deps = []
            else:
                deps = [{"HYP": "SPEC", "LIT": "HYP", "PLAN": "LIT"}[key]]
            # SPEC expected output name = catalog artifact_present.check.output only.
            if kind == TaskKind.SPEC:
                stage_outputs = [
                    OutputSpec(
                        name=spec_output_name,
                        media_type="application/json",
                        schema_ref=None,
                        required=True,
                    )
                ]
            else:
                stage_outputs = []
            spec = TaskSpec(
                task_key=key,
                title=key,
                goal=key,
                route=kind.value,
                depends_on=deps,
                criteria=[],
                expected_outputs=stage_outputs,
                # DESIGN §2: hypotheses/literature optional (planner Need); REPORT
                # already optional. Intake HYP/LIT stay in the DAG for deps but
                # SKIPPED (Need off / role not wired) must not block fulfilled.
                required=(kind not in {TaskKind.REPORT, TaskKind.HYPOTHESES, TaskKind.LITERATURE}),
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
        questions: list[QuestionSpec],
        subquestions: Sequence[QuestionSpec],
        rollup: str = "all",
        revision: int = 1,
        spec_output: dict[str, object] | None,
        expand_after: Sequence[str],
        loops: Sequence[LoopSpec],
        hypotheses: Need | None = None,
        literature: Need | None = None,
    ) -> str:
        """Accept a plan revision.

        ``questions`` come from SPEC (``ResearchSpecOut.questions[]``). When
        ``spec_output`` is set, questions are extracted via
        ``questions_from_spec_output`` and ``questions`` must be empty.
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
        raw_loops = loops
        loop_specs: list[LoopSpec] = []
        expand_after_keys = list(expand_after)
        plan_refuse = None
        try:
            parsed_tasks: list[TaskSpec] = []
            for t in tasks:
                if isinstance(t, TaskSpec):
                    parsed_tasks.append(t)
                else:
                    parsed_tasks.append(TaskSpec.model_validate(t))
            tasks = parsed_tasks
            if spec_output is not None:
                if questions:
                    raise ValueError("accept_plan: questions and spec_output are mutually exclusive")
                questions = [QuestionSpec.model_validate(q) for q in questions_from_spec_output(spec_output)]
            questions = list(questions)
            parsed_subs: list[QuestionSpec] = []
            for s in subquestions:
                if isinstance(s, QuestionSpec):
                    parsed_subs.append(s)
                else:
                    parsed_subs.append(QuestionSpec.model_validate(s))
            questions, named_subs = merge_plan_questions(questions, parsed_subs)
            from coscientist.core.plan.decomposes import stamp_question_origins

            questions = stamp_question_origins(questions, named_sub_ids={s.id for s in named_subs})
            named_from_parent = [q for q in questions if q.parent_id is not None]
            if named_from_parent:
                named_subs = named_from_parent
            self._validate_dag(tasks)
            known_texts = await self._known_question_texts()
            self._validate_questions(questions, known_texts)
            known_texts = {**known_texts, **{q.id: q.text for q in questions}}
            self._validate_addresses(tasks, set(known_texts))
            task_keys = {t.task_key for t in tasks}
            for k in expand_after_keys:
                if k not in task_keys:
                    raise ValueError(f"expand_after unknown task_key: {k!r}")
            known_keys = {t.spec.task_key for t in self.tasks.values()} | task_keys
            known_hyps: set[str] = set()
            for node in self.graph.nodes_by_kind("hypothesis"):
                hid = node.ref_id
                if isinstance(hid, str) and hid:
                    known_hyps.add(hid)
            loop_specs = validate_loop_specs(
                raw_loops,
                known_keys=known_keys,
                max_loop_iter=int(self.study.max_loop_iter),
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
            validate_plan_criteria(tasks, catalog)
            tasks, questions = freeze_plan_rubrics(
                tasks,
                questions,
                catalog=catalog,
                subquestions=named_subs,
                rollup=rollup,
                max_depth=int(self.study.max_depth),
                max_tasks=int(self.study.max_tasks),
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
                        raise PlanInvalid(f"depends_on invalid loop barrier {d!r}: {e}") from e
                    if lid not in loop_ids:
                        raise PlanInvalid(f"depends_on unknown loop barrier {d!r}")
                    nd = f"loop:{lid}"
                    if nd != d:
                        changed = True
                    new_deps.append(nd)
                if changed:
                    rewritten.append(t.model_copy(update={"depends_on": new_deps}))
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
            # RC4: Need.needed (forced when require_hypotheses) → ≥1 HYP-n.
            assert_hyp_n_when_needed(tasks, hyp_need)
            # Criterion / rubric GraphNode attrs built here so CatalogError maps
            # to PlanRefused via plan_refuse below (no return-in-except).
            rubric_freeze_rows, criterion_freeze_rows = _plan_accept_freeze_rows(
                sid=sid,
                tasks=tasks,
                catalog=catalog,
                graph=self.graph,
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
            return await apply_plan_refused(self, detail=detail, report_line=report_line)
        # try-success path always binds these (mypy: narrow after refuse return).
        assert isinstance(questions, list)
        pid = plan_id()
        plan = Plan(
            plan_id=pid,
            study_id=sid,
            revision=revision,
            hypothesis_id=None,
            tasks=tasks,
            questions=questions,
            expand_after=list(expand_after_keys),
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
        for q in questions:
            qnid = question_node_id(sid, q.id)
            if self.graph.get_node(qnid) is not None:
                continue  # never clobber existing attrs
            q_attrs: dict[str, object] = {
                "formulation": q.text,
                "final_type": q.final_type,
            }
            if q.parent_id is not None:
                q_attrs["parent_id"] = q.parent_id
            if q.origin is not None:
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
            parent = q.parent_id
            if parent is None:
                continue
            qnid = question_node_id(sid, q.id)
            pn = question_node_id(sid, str(parent))
            eid = f"decomposes:{qnid}:{pn}"
            origin = q.origin
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
                kind=task_kind_for_route(frozen_spec.route),
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
        from coscientist.core.events.payload_schemas import (
            PlanAcceptedPayload,
            PlanExpandedPayload,
        )

        texts: dict[str, str] = {}
        raw_rows: list[object]
        if self.plans:
            raw_rows = list(self.plans.values())
        else:
            raw_rows = list(await self.store.get_projection("plans"))
        plan_rows: list[PlanAcceptedPayload | PlanExpandedPayload] = []
        for row in raw_rows:
            if not isinstance(row, dict):
                raise ValueError(f"plan projection must be a mapping, got {type(row).__name__}")
            raw_qs = row.get("questions")
            if raw_qs is None:
                raise ValueError("plan questions required (got None)")
            if not isinstance(raw_qs, list):
                raise ValueError(f"plan questions must be a list, got {type(raw_qs).__name__}")
            if "added_tasks" in row or "plan_rev" in row:
                plan_rows.append(PlanExpandedPayload.model_validate(row))
            else:
                plan_rows.append(PlanAcceptedPayload.model_validate(row))
        for plan in plan_rows:
            for q in plan.questions:
                if qid := q.id:
                    if qid in texts and texts[qid] != q.text:
                        raise ValueError(f"question id {qid!r} text conflict")
                    texts[qid] = q.text
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
                if d in keys or d in stage or d.startswith("PLAN-") or d.startswith("loop:"):
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
