"""Fail-closed EventType payload schemas (writer wire shapes). No silent defaults."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, JsonValue, StrictInt, ValidationError

from coscientist.core.events.errors import JournalPayloadError
from coscientist.core.execution.domain import (
    Budget,
    CheckResult,
    HitlPolicy,
    InputBinding,
    LoopSpec,
    QuestionSpec,
    TaskSpec,
    ToolCallRef,
)

_WIRE = ConfigDict(frozen=True, extra="forbid", strict=True)


def _refuse(event_type: str, exc: ValidationError) -> None:
    first = exc.errors()[0]
    raise JournalPayloadError(
        f"{event_type}: payload schema refuse ({first['type']})",
        expected_store_format=None,
        found_store_format=None,
        loc=tuple(first["loc"]),
        error_type=str(first["type"]),
    ) from exc


def _check(event_type: str, model: type[BaseModel], raw: dict[str, Any]) -> dict[str, Any]:
    """Validate then return ``raw`` unchanged (no defaults written back)."""
    try:
        model.model_validate(raw)
    except ValidationError as e:
        _refuse(event_type, e)
    return raw


# --- nested wire helpers (strict) -------------------------------------------------

class UntilSpec(BaseModel):
    model_config = _WIRE
    name: str
    params: dict[str, JsonValue]


class LimitationRow(BaseModel):
    model_config = _WIRE
    text: str
    ref: str | None = None  # audit-ok[field_default]: LimitationRow.ref optional on wire


class SupersededQuestion(BaseModel):
    model_config = _WIRE
    new_id: str
    old_id: str


class QuestionDisposition(BaseModel):
    """Loose wire row under StudyFinalized.question_dispositions values."""

    model_config = ConfigDict(frozen=True, extra="allow", strict=True)
    disposition: str | None = None  # audit-ok[field_default]: QuestionDisposition.disposition optional on wire
    reason: str | None = None  # audit-ok[field_default]: QuestionDisposition.reason optional on wire


# --- per-event wire models --------------------------------------------------------

class StudyCreatedPayload(BaseModel):
    """Wire shape after upcast; sparse journals may omit Study dump keys."""

    model_config = _WIRE
    study_id: str
    catalog_version: StrictInt
    profile: str | None = None  # audit-ok[field_default]: StudyCreatedPayload.profile optional on wire
    request: str | None = None  # audit-ok[field_default]: StudyCreatedPayload.request optional on wire
    hitl_policy: HitlPolicy | None = None  # audit-ok[field_default]: StudyCreatedPayload.hitl_policy optional on wire
    deadline: str | None = None  # audit-ok[field_default]: StudyCreatedPayload.deadline optional on wire
    created_at: str | None = None  # audit-ok[field_default]: StudyCreatedPayload.created_at optional on wire
    parent_study_id: str | None = None  # audit-ok[field_default]: StudyCreatedPayload.parent_study_id optional on wire
    budget: Budget | None = None  # audit-ok[field_default]: StudyCreatedPayload.budget optional on wire
    finalize_reserve_s: StrictInt | None = None  # audit-ok[field_default]: StudyCreatedPayload.finalize_reserve_s optional on wire
    confirmations_needed: StrictInt | None = None  # audit-ok[field_default]: StudyCreatedPayload.confirmations_needed optional on wire
    refutations_needed: StrictInt | None = None  # audit-ok[field_default]: StudyCreatedPayload.refutations_needed optional on wire
    refutation_priority: bool | None = None  # audit-ok[field_default]: StudyCreatedPayload.refutation_priority optional on wire
    supported_needed: StrictInt | None = None  # audit-ok[field_default]: StudyCreatedPayload.supported_needed optional on wire
    aggregate: Literal["count"] | None = None  # audit-ok[field_default]: StudyCreatedPayload.aggregate optional on wire
    evalue_alpha: float | None = None  # audit-ok[field_default]: StudyCreatedPayload.evalue_alpha optional on wire
    max_expansions: StrictInt | None = None  # audit-ok[field_default]: StudyCreatedPayload.max_expansions optional on wire
    max_human_expansions: StrictInt | None = None  # audit-ok[field_default]: StudyCreatedPayload.max_human_expansions optional on wire
    replan_reserve: float | None = None  # audit-ok[field_default]: StudyCreatedPayload.replan_reserve optional on wire
    max_tasks: StrictInt | None = None  # audit-ok[field_default]: StudyCreatedPayload.max_tasks optional on wire
    max_depth: StrictInt | None = None  # audit-ok[field_default]: StudyCreatedPayload.max_depth optional on wire
    plan_timeout_s: StrictInt | None = None  # audit-ok[field_default]: StudyCreatedPayload.plan_timeout_s optional on wire
    max_foreach_children: StrictInt | None = None  # audit-ok[field_default]: StudyCreatedPayload.max_foreach_children optional on wire
    max_loop_iter: StrictInt | None = None  # audit-ok[field_default]: StudyCreatedPayload.max_loop_iter optional on wire
    max_task_budget_share: float | None = None  # audit-ok[field_default]: StudyCreatedPayload.max_task_budget_share optional on wire
    as_of: str | None = None  # audit-ok[field_default]: StudyCreatedPayload.as_of optional on wire
    user_input_ids: list[str] | None = None  # audit-ok[field_default]: StudyCreatedPayload.user_input_ids optional on wire
    max_proposals: StrictInt | None = None  # audit-ok[field_default]: StudyCreatedPayload.max_proposals optional on wire


class StudyRefusedPayload(BaseModel):
    model_config = _WIRE
    kind: str
    reason: str
    classifier_error: bool
    catalog_version: StrictInt | None = None  # audit-ok[field_default]: StudyRefusedPayload.catalog_version optional on wire


class StudyCancelledPayload(BaseModel):
    model_config = _WIRE
    study_id: str
    by: str
    at: str | None = None  # audit-ok[field_default]: StudyCancelledPayload.at optional on wire


class StudyFinalizedPayload(BaseModel):
    model_config = _WIRE
    trigger: str
    terminal_reason: str
    contributing_reasons: list[str]
    verdict: str
    question_dispositions: dict[str, JsonValue]
    report_artifact_id: str | None
    conclusion_ids: list[str]
    minimal_report: dict[str, JsonValue] | None
    fallback_reason: str | None
    refusal: str | None = None  # audit-ok[field_default]: StudyFinalizedPayload.refusal optional on wire
    hypothesis_verdicts: dict[str, JsonValue] | None = None  # audit-ok[field_default]: StudyFinalizedPayload.hypothesis_verdicts optional on wire
    hypotheses_unavailable: str | None = None  # audit-ok[field_default]: StudyFinalizedPayload.hypotheses_unavailable optional on wire
    hypothesis_report: list[JsonValue] | None = None  # audit-ok[field_default]: StudyFinalizedPayload.hypothesis_report optional on wire
    custom_checks: list[JsonValue] | None = None  # audit-ok[field_default]: StudyFinalizedPayload.custom_checks optional on wire
    observations: list[JsonValue] | None = None  # audit-ok[field_default]: StudyFinalizedPayload.observations optional on wire


class TaskCreatedPayload(BaseModel):
    """Task dump on the wire (datetimes as ISO strings)."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    task_id: str
    kind: str
    spec: TaskSpec
    state: str
    outcome: str | None = None  # audit-ok[field_default]: TaskCreatedPayload.outcome optional on wire
    reason: str | None = None  # audit-ok[field_default]: TaskCreatedPayload.reason optional on wire
    deadline: str
    attempts: list[str]
    inherited_from: str | None = None  # audit-ok[field_default]: TaskCreatedPayload.inherited_from optional on wire
    expand_after_task: str | None = None  # audit-ok[field_default]: TaskCreatedPayload.expand_after_task optional on wire
    pending_expansion: dict[str, JsonValue] | None = None  # audit-ok[field_default]: TaskCreatedPayload.pending_expansion optional on wire
    replan_allocated_s: float | None = None  # audit-ok[field_default]: TaskCreatedPayload.replan_allocated_s optional on wire


class TaskInheritedPayload(TaskCreatedPayload):
    pass


class AttemptStartedPayload(BaseModel):
    model_config = _WIRE
    attempt_id: str
    n: StrictInt
    provenance: str
    study_id: str
    task_id: str
    dispatch_id: str | None = None  # audit-ok[field_default]: AttemptStartedPayload.dispatch_id optional on wire
    route_returned: bool | None = None  # audit-ok[field_default]: AttemptStartedPayload.route_returned optional on wire
    closed: bool | None = None  # audit-ok[field_default]: AttemptStartedPayload.closed optional on wire
    close_reason: str | None = None  # audit-ok[field_default]: AttemptStartedPayload.close_reason optional on wire
    agent_output_digest: str | None = None  # audit-ok[field_default]: AttemptStartedPayload.agent_output_digest optional on wire
    deadline: str | None = None  # audit-ok[field_default]: AttemptStartedPayload.deadline optional on wire
    inputs: list[InputBinding] | None = None  # audit-ok[field_default]: AttemptStartedPayload.inputs optional on wire
    inputs_digest: str | None = None  # audit-ok[field_default]: AttemptStartedPayload.inputs_digest optional on wire
    tainted_context: bool | None = None  # audit-ok[field_default]: AttemptStartedPayload.tainted_context optional on wire
    limitations: list[LimitationRow] | None = None  # audit-ok[field_default]: AttemptStartedPayload.limitations optional on wire
    attempt_role: str | None = None  # audit-ok[field_default]: AttemptStartedPayload.attempt_role optional on wire
    allocated_budget_rub: str | None = None  # audit-ok[field_default]: AttemptStartedPayload.allocated_budget_rub optional on wire
    budget_limit_rub: str | None = None  # audit-ok[field_default]: AttemptStartedPayload.budget_limit_rub optional on wire
    share_renorm: bool | None = None  # audit-ok[field_default]: AttemptStartedPayload.share_renorm optional on wire


class AttemptClosedPayload(BaseModel):
    model_config = _WIRE
    attempt_id: str
    close_reason: str
    closed: bool | None = None  # audit-ok[field_default]: AttemptClosedPayload.closed optional on wire
    task_id: str | None = None  # audit-ok[field_default]: AttemptClosedPayload.task_id optional on wire
    retryable: bool | None = None  # audit-ok[field_default]: AttemptClosedPayload.retryable optional on wire
    retry_at: str | None = None  # audit-ok[field_default]: AttemptClosedPayload.retry_at optional on wire
    charged: bool | None = None  # audit-ok[field_default]: AttemptClosedPayload.charged optional on wire


class RouteReturnedPayload(BaseModel):
    model_config = _WIRE
    attempt_id: str
    agent_output_digest: str
    route: str | None = None  # audit-ok[field_default]: RouteReturnedPayload.route optional on wire


class ResultRecordedPayload(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    result_id: str
    attempt_id: str
    task_id: str
    outcome: str
    computed_outcome: str
    reason: str | None
    provenance: str
    check_results: list[CheckResult]
    unassessed: list[str]
    artifacts: list[str]
    agent_claim: str | None
    inputs: list[JsonValue]
    payload_digest: str
    charged: bool
    result_class: str
    output_kinds: dict[str, str] | None = None  # audit-ok[field_default]: ResultRecordedPayload.output_kinds optional on wire
    output_map: dict[str, str] | None = None  # audit-ok[field_default]: ResultRecordedPayload.output_map optional on wire
    downgrade: dict[str, JsonValue] | None = None  # audit-ok[field_default]: ResultRecordedPayload.downgrade optional on wire
    expansion_request: dict[str, JsonValue] | None = None  # audit-ok[field_default]: ResultRecordedPayload.expansion_request optional on wire
    detail: str | None = None  # audit-ok[field_default]: ResultRecordedPayload.detail optional on wire
    claims_dropped: list[JsonValue] | dict[str, JsonValue] | None = None  # audit-ok[field_default]: ResultRecordedPayload.claims_dropped optional on wire
    budget_overrun: bool | None = None  # audit-ok[field_default]: ResultRecordedPayload.budget_overrun optional on wire
    budget_limit_rub: str | None = None  # audit-ok[field_default]: ResultRecordedPayload.budget_limit_rub optional on wire
    agent_output_digest: str | None = None  # audit-ok[field_default]: ResultRecordedPayload.agent_output_digest optional on wire


class TaskDonePayload(BaseModel):
    model_config = _WIRE
    task_id: str
    outcome: str
    reason: str | None
    detail: str | None = None  # audit-ok[field_default]: TaskDonePayload.detail optional on wire
    budget_overrun: bool | None = None  # audit-ok[field_default]: TaskDonePayload.budget_overrun optional on wire


class LoopWire(BaseModel):
    """PlanAccepted.loops row; ``body`` may be corrupt str in restart fixtures."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    loop_id: str
    body: list[TaskSpec] | str
    until: UntilSpec | dict[str, JsonValue]
    max_iter: StrictInt
    scope_hypotheses: list[str] | None = None  # audit-ok[field_default]: LoopWire.scope_hypotheses optional on wire
    scope_questions: list[str] | None = None  # audit-ok[field_default]: LoopWire.scope_questions optional on wire
    depends_on: list[str] | None = None  # audit-ok[field_default]: LoopWire.depends_on optional on wire


class PlanAcceptedPayload(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    plan_id: str
    study_id: str
    revision: StrictInt
    hypothesis_id: str | None
    tasks: list[TaskSpec]
    questions: list[QuestionSpec]
    expand_after: list[str]
    loops: list[LoopWire | LoopSpec]
    subquestions: list[QuestionSpec] | None = None  # audit-ok[field_default]: PlanAcceptedPayload.subquestions optional on wire
    rollup: str | None = None  # audit-ok[field_default]: PlanAcceptedPayload.rollup optional on wire


class PlanExpandedPayload(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    plan_id: str
    study_id: str
    revision: StrictInt
    hypothesis_id: str | None
    tasks: list[TaskSpec]
    questions: list[QuestionSpec]
    expand_after: list[str]
    loops: list[LoopSpec]
    subquestions: list[QuestionSpec]
    rollup: str
    after_task: str | None
    plan_rev: StrictInt
    added_tasks: list[TaskSpec]
    added_questions: list[QuestionSpec]
    superseded_tasks: list[str]
    superseded_questions: list[SupersededQuestion | dict[str, str]]
    origin: str
    reason: str
    delegated_by: str | None
    replan_allocated_s: float
    prev_plan_id: str | None
    hitl_request_id: str | None
    plan_key: str | None
    foreach: bool | None = None  # audit-ok[field_default]: PlanExpandedPayload.foreach optional on wire
    foreach_template: str | None = None  # audit-ok[field_default]: PlanExpandedPayload.foreach_template optional on wire
    foreach_items: list[JsonValue] | None = None  # audit-ok[field_default]: PlanExpandedPayload.foreach_items optional on wire
    foreach_kind: str | None = None  # audit-ok[field_default]: PlanExpandedPayload.foreach_kind optional on wire
    foreach_capped: bool | None = None  # audit-ok[field_default]: PlanExpandedPayload.foreach_capped optional on wire
    foreach_cap: StrictInt | None = None  # audit-ok[field_default]: PlanExpandedPayload.foreach_cap optional on wire
    foreach_total: StrictInt | None = None  # audit-ok[field_default]: PlanExpandedPayload.foreach_total optional on wire
    foreach_dropped: list[JsonValue] | None = None  # audit-ok[field_default]: PlanExpandedPayload.foreach_dropped optional on wire
    foreach_rank_notes: list[JsonValue] | None = None  # audit-ok[field_default]: PlanExpandedPayload.foreach_rank_notes optional on wire
    foreach_refute: bool | None = None  # audit-ok[field_default]: PlanExpandedPayload.foreach_refute optional on wire
    loop: bool | None = None  # audit-ok[field_default]: PlanExpandedPayload.loop optional on wire
    loop_id: str | None = None  # audit-ok[field_default]: PlanExpandedPayload.loop_id optional on wire
    loop_iter: StrictInt | None = None  # audit-ok[field_default]: PlanExpandedPayload.loop_iter optional on wire
    loop_max_iter: StrictInt | None = None  # audit-ok[field_default]: PlanExpandedPayload.loop_max_iter optional on wire
    loop_template_keys: list[str] | None = None  # audit-ok[field_default]: PlanExpandedPayload.loop_template_keys optional on wire
    loop_until: UntilSpec | dict[str, JsonValue] | None = None  # audit-ok[field_default]: PlanExpandedPayload.loop_until optional on wire


class ExpansionRefusedPayload(BaseModel):
    model_config = _WIRE
    after_task: str | None
    reason: str
    detail: str
    plan_key: str | None
    loop: bool | None = None  # audit-ok[field_default]: ExpansionRefusedPayload.loop optional on wire
    loop_id: str | None = None  # audit-ok[field_default]: ExpansionRefusedPayload.loop_id optional on wire
    loop_reason: str | None = None  # audit-ok[field_default]: ExpansionRefusedPayload.loop_reason optional on wire
    report_line: str | None = None  # audit-ok[field_default]: ExpansionRefusedPayload.report_line optional on wire


class PlanRefusedPayload(BaseModel):
    model_config = _WIRE
    reason: str
    detail: str
    report_line: str


class LoopCappedPayload(BaseModel):
    model_config = _WIRE
    loop_id: str
    iter: StrictInt
    max_iter: StrictInt
    survivors: StrictInt | None
    scope_hypotheses: list[str]
    scope_questions: list[str]
    until: UntilSpec | dict[str, JsonValue]
    reason: str
    detail: str | None = None  # audit-ok[field_default]: LoopCappedPayload.detail optional on wire
    refuse_detail: str | None = None  # audit-ok[field_default]: LoopCappedPayload.refuse_detail optional on wire


class LoopCompletedPayload(BaseModel):
    model_config = _WIRE
    loop_id: str
    iter: StrictInt
    max_iter: StrictInt
    survivors: StrictInt
    scope_hypotheses: list[str]
    scope_questions: list[str]
    until: UntilSpec | dict[str, JsonValue]
    reason: str


class LoopRefusedPayload(BaseModel):
    model_config = _WIRE
    loop_id: str
    iter: StrictInt
    max_iter: StrictInt
    reason: str
    detail: str
    scope_hypotheses: list[str]
    scope_questions: list[str]
    until: UntilSpec | dict[str, JsonValue]


class HitlOpenedPayload(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    request_id: str
    kind: str
    open: bool
    task_id: str
    cost_rub: float
    payload_ref: str | None = None  # audit-ok[field_default]: HitlOpenedPayload.payload_ref optional on wire
    deadline: str | None = None  # audit-ok[field_default]: HitlOpenedPayload.deadline optional on wire
    study_id: str | None = None  # audit-ok[field_default]: HitlOpenedPayload.study_id optional on wire
    expand_after_task: str | None = None  # audit-ok[field_default]: HitlOpenedPayload.expand_after_task optional on wire


class HitlDecidedPayload(BaseModel):
    model_config = _WIRE
    request_id: str
    decision: str
    by: str
    reason: str
    spent_rub: float
    approved_by: str | None = None  # audit-ok[field_default]: HitlDecidedPayload.approved_by optional on wire
    decided_by: str | None = None  # audit-ok[field_default]: HitlDecidedPayload.decided_by optional on wire
    delegated_by: str | None = None  # audit-ok[field_default]: HitlDecidedPayload.delegated_by optional on wire
    proposal_id: str | None = None  # audit-ok[field_default]: HitlDecidedPayload.proposal_id optional on wire
    keep_open: bool | None = None  # audit-ok[field_default]: HitlDecidedPayload.keep_open optional on wire
    effect: str | None = None  # audit-ok[field_default]: HitlDecidedPayload.effect optional on wire
    human_message: str | None = None  # audit-ok[field_default]: HitlDecidedPayload.human_message optional on wire
    cost_estimate_rub: float | None = None  # audit-ok[field_default]: HitlDecidedPayload.cost_estimate_rub optional on wire
    cost_rub: float | None = None  # audit-ok[field_default]: HitlDecidedPayload.cost_rub optional on wire


class HitlTimedOutPayload(BaseModel):
    model_config = _WIRE
    request_id: str
    reason: str
    spent_rub: float
    proposal_id: str | None = None  # audit-ok[field_default]: HitlTimedOutPayload.proposal_id optional on wire
    decided_by: str | None = None  # audit-ok[field_default]: HitlTimedOutPayload.decided_by optional on wire
    delegated_by: str | None = None  # audit-ok[field_default]: HitlTimedOutPayload.delegated_by optional on wire
    human_message: str | None = None  # audit-ok[field_default]: HitlTimedOutPayload.human_message optional on wire
    effect: str | None = None  # audit-ok[field_default]: HitlTimedOutPayload.effect optional on wire


class HitlDeadlineExtendedPayload(BaseModel):
    model_config = _WIRE
    request_id: str
    deadline: str


class BudgetSnapshot(BaseModel):
    model_config = _WIRE
    blocked: bool
    limit_rub: float
    spent_rub: float


class BudgetDebitedPayload(BaseModel):
    model_config = _WIRE
    request_id: str
    spent_rub: str
    task_id: str | None = None  # audit-ok[field_default]: BudgetDebitedPayload.task_id optional on wire
    actual_rub: str | float | None = None  # audit-ok[field_default]: BudgetDebitedPayload.actual_rub optional on wire
    budget: BudgetSnapshot | None = None  # audit-ok[field_default]: BudgetDebitedPayload.budget optional on wire
    cost_estimate_rub: float | None = None  # audit-ok[field_default]: BudgetDebitedPayload.cost_estimate_rub optional on wire
    decided_by: str | None = None  # audit-ok[field_default]: BudgetDebitedPayload.decided_by optional on wire
    delegated_by: str | None = None  # audit-ok[field_default]: BudgetDebitedPayload.delegated_by optional on wire
    reason: str | None = None  # audit-ok[field_default]: BudgetDebitedPayload.reason optional on wire


class BudgetExceededPayload(BaseModel):
    model_config = _WIRE
    scope: str


class BudgetSharesAdjustedPayload(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    request_id: str
    plan_id: str
    budget_shares: dict[str, float]
    deadline_shares: dict[str, float]
    tasks: list[TaskSpec]
    approved_by: str


class BudgetSharesRefusedPayload(BaseModel):
    model_config = _WIRE
    request_id: str
    approved_by: str
    detail: str
    reason: str


class ArtifactRegisteredPayload(BaseModel):
    """Full Artifact dump or kind-stamp subset."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    artifact_id: str
    name: str
    sha256: str
    role: str
    kind: str | None
    media_type: str | None = None  # audit-ok[field_default]: ArtifactRegisteredPayload.media_type optional on wire
    size: StrictInt | None = None  # audit-ok[field_default]: ArtifactRegisteredPayload.size optional on wire
    uri: str | None = None  # audit-ok[field_default]: ArtifactRegisteredPayload.uri optional on wire
    produced_by: ToolCallRef | None = None  # audit-ok[field_default]: ArtifactRegisteredPayload.produced_by optional on wire
    schema_ref: str | None = None  # audit-ok[field_default]: ArtifactRegisteredPayload.schema_ref optional on wire
    producer: str | None = None  # audit-ok[field_default]: ArtifactRegisteredPayload.producer optional on wire
    net_isolation: str | None = None  # audit-ok[field_default]: ArtifactRegisteredPayload.net_isolation optional on wire
    fs_isolation: str | None = None  # audit-ok[field_default]: ArtifactRegisteredPayload.fs_isolation optional on wire
    tainted: bool | None = None  # audit-ok[field_default]: ArtifactRegisteredPayload.tainted optional on wire
    source_uri: str | None = None  # audit-ok[field_default]: ArtifactRegisteredPayload.source_uri optional on wire
    stamped: bool | None = None  # audit-ok[field_default]: ArtifactRegisteredPayload.stamped optional on wire


class LateWriteRejectedPayload(BaseModel):
    model_config = _WIRE
    attempt_id: str
    reason: str


class DowngradeRejectedPayload(BaseModel):
    model_config = _WIRE
    attempt_id: str
    reason: str


class CatalogErrorRaisedPayload(BaseModel):
    model_config = _WIRE
    error: str
    reason: str


class SandboxInfraRaisedPayload(BaseModel):
    model_config = _WIRE
    error: str
    reason: str


class SameDigestRetryRejectedPayload(BaseModel):
    model_config = _WIRE
    error: str
    reason: str


class LeaseAcquiredPayload(BaseModel):
    model_config = _WIRE
    owner: str
    epoch: StrictInt
    owner_nonce: str | None = None  # audit-ok[field_default]: LeaseAcquiredPayload.owner_nonce optional on wire
    ttl_s: float | StrictInt | None = None  # audit-ok[field_default]: LeaseAcquiredPayload.ttl_s optional on wire


class ResultPendingPayload(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    attempt_id: str
    task_id: str
    agent_output_digest: str
    check_results: list[CheckResult]
    reason: str


class ReviewScheduledPayload(BaseModel):
    model_config = _WIRE
    kind: str
    task_id: str
    attempt_id: str | None = None  # audit-ok[field_default]: ReviewScheduledPayload.attempt_id optional on wire
    check_id: str | None = None  # audit-ok[field_default]: ReviewScheduledPayload.check_id optional on wire
    item_id: str | None = None  # audit-ok[field_default]: ReviewScheduledPayload.item_id optional on wire
    output_digest: str | None = None  # audit-ok[field_default]: ReviewScheduledPayload.output_digest optional on wire
    review_key: str | None = None  # audit-ok[field_default]: ReviewScheduledPayload.review_key optional on wire
    rubric_digest: str | None = None  # audit-ok[field_default]: ReviewScheduledPayload.rubric_digest optional on wire
    reviewer_task_id: str | None = None  # audit-ok[field_default]: ReviewScheduledPayload.reviewer_task_id optional on wire
    reviewer_attempt_id: str | None = None  # audit-ok[field_default]: ReviewScheduledPayload.reviewer_attempt_id optional on wire
    status: str | None = None  # audit-ok[field_default]: ReviewScheduledPayload.status optional on wire
    proposal_id: str | None = None  # audit-ok[field_default]: ReviewScheduledPayload.proposal_id optional on wire
    deadline: str | None = None  # audit-ok[field_default]: ReviewScheduledPayload.deadline optional on wire
    hitl_request_id: str | None = None  # audit-ok[field_default]: ReviewScheduledPayload.hitl_request_id optional on wire
    proposer_attempt_id: str | None = None  # audit-ok[field_default]: ReviewScheduledPayload.proposer_attempt_id optional on wire


class ReviewRecordedPayload(BaseModel):
    model_config = _WIRE
    attempt_id: str
    check_id: str
    failure_kind: str | None
    item_id: str
    output_digest: str
    retry: bool | StrictInt | dict[str, JsonValue]
    review_key: str
    status: str
    task_id: str


class JudgeCasCorruptPayload(BaseModel):
    model_config = _WIRE
    detail: str
    report_line: str


class ForkCreatedPayload(BaseModel):
    model_config = _WIRE
    parent_study_id: str
    study_id: str
    clarifications: list[JsonValue] | None = None  # audit-ok[field_default]: ForkCreatedPayload.clarifications optional on wire
    digest: str | None = None  # audit-ok[field_default]: ForkCreatedPayload.digest optional on wire


class VerdictSnapshotsRecordedPayload(BaseModel):
    model_config = _WIRE
    attempt_id: str
    scope: str
    snapshots: dict[str, JsonValue]
    study_id: str


class GraphNodeUpsertedPayload(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    node_id: str
    kind: str
    study_id: str
    attrs: dict[str, JsonValue]
    ref_id: str | None = None  # audit-ok[field_default]: GraphNodeUpsertedPayload.ref_id optional on wire
    parent_id: str | None = None  # audit-ok[field_default]: GraphNodeUpsertedPayload.parent_id optional on wire
    created_at: str | None = None  # audit-ok[field_default]: GraphNodeUpsertedPayload.created_at optional on wire


class GraphEdgeAddedPayload(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    edge_id: str
    kind: str
    study_id: str
    src_id: str
    dst_id: str
    attrs: dict[str, JsonValue]
    created_at: str | None = None  # audit-ok[field_default]: GraphEdgeAddedPayload.created_at optional on wire


class EffectIntendedPayload(BaseModel):
    model_config = _WIRE
    args_digest: str
    attempt_id: str
    effect: str
    key: str
    study_id: str
    task_key: str
    tool: str
    fingerprint: str | None = None  # audit-ok[field_default]: EffectIntendedPayload.fingerprint optional on wire


class EffectDonePayload(BaseModel):
    model_config = _WIRE
    attempt_id: str
    key: str
    result_ref: str | None
    status: str
    tool: str
    detail: str | None = None  # audit-ok[field_default]: EffectDonePayload.detail optional on wire
    error_kind: str | None = None  # audit-ok[field_default]: EffectDonePayload.error_kind optional on wire


class SideEffectDeniedPayload(BaseModel):
    model_config = _WIRE
    attempt_id: str
    effect: str
    key: str
    reason: str
    task_key: str
    tool: str


class SourceFetchedPayload(BaseModel):
    model_config = _WIRE
    attempt_id: str
    call_id: str
    digest: str
    tool: str
    url: str


class AttemptTaintedPayload(BaseModel):
    model_config = _WIRE
    attempt_id: str
    call_id: str
    is_fetch: bool
    reason: str
    tool: str


class FetchIntegrityRaisedPayload(BaseModel):
    model_config = _WIRE
    call_id: str
    internal_error: bool
    kind: str
    tool: str


class QuestionDisposedPayload(BaseModel):
    model_config = _WIRE
    design_artifact_id: str | None
    disposition: str
    question_id: str
    reason: str
    refs: list[str]


class QuestionDisposeRejectedPayload(BaseModel):
    model_config = _WIRE
    detail: str
    disposition: str
    question_id: str
    reason: str
    report_line: str
    row_key: str | None = None  # audit-ok[field_default]: QuestionDisposeRejectedPayload.row_key optional on wire


class CheckProposedPayload(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    attempt_id: str
    check: dict[str, JsonValue]
    justification: str
    measures: list[JsonValue] | str | None
    predicted_direction: str | None
    predicts_null: bool | None
    proposal_digest: str
    proposal_id: str
    proposer_attempt_id: str
    rejected_templates: list[str | dict[str, JsonValue]]
    role: str
    rubric: str | None
    sesoi: float | None
    study_id: str
    target: str
    target_kind: str
    task_id: str
    threshold: float | None
    uses_sesoi: bool


class CheckApprovedPayload(BaseModel):
    model_config = _WIRE
    approved_by: str
    bound_target: str
    bound_task_id: str
    by: str
    delegated: bool
    hitl_request_id: str | None
    human_principal: str | None
    proposal_digest: str
    proposal_id: str
    reviewer_attempt_id: str | None
    source: str


class CheckRejectedPayload(BaseModel):
    model_config = _WIRE
    by: str
    delegated: bool
    hitl_request_id: str | None
    human_principal: str | None
    proposal_digest: str
    proposal_id: str
    reason: str
    rejected_by: str
    reviewer_attempt_id: str | None
    source: str


PAYLOAD_MODELS: dict[str, type[BaseModel]] = {
    "StudyCreated": StudyCreatedPayload,
    "StudyRefused": StudyRefusedPayload,
    "StudyCancelled": StudyCancelledPayload,
    "StudyFinalized": StudyFinalizedPayload,
    "TaskCreated": TaskCreatedPayload,
    "TaskInherited": TaskInheritedPayload,
    "AttemptStarted": AttemptStartedPayload,
    "AttemptClosed": AttemptClosedPayload,
    "RouteReturned": RouteReturnedPayload,
    "ResultRecorded": ResultRecordedPayload,
    "TaskDone": TaskDonePayload,
    "PlanAccepted": PlanAcceptedPayload,
    "PlanExpanded": PlanExpandedPayload,
    "ExpansionRefused": ExpansionRefusedPayload,
    "PlanRefused": PlanRefusedPayload,
    "LoopCapped": LoopCappedPayload,
    "LoopCompleted": LoopCompletedPayload,
    "LoopRefused": LoopRefusedPayload,
    "HitlOpened": HitlOpenedPayload,
    "HitlDecided": HitlDecidedPayload,
    "HitlTimedOut": HitlTimedOutPayload,
    "HitlDeadlineExtended": HitlDeadlineExtendedPayload,
    "BudgetDebited": BudgetDebitedPayload,
    "BudgetExceeded": BudgetExceededPayload,
    "BudgetSharesAdjusted": BudgetSharesAdjustedPayload,
    "BudgetSharesRefused": BudgetSharesRefusedPayload,
    "ArtifactRegistered": ArtifactRegisteredPayload,
    "LateWriteRejected": LateWriteRejectedPayload,
    "DowngradeRejected": DowngradeRejectedPayload,
    "CatalogErrorRaised": CatalogErrorRaisedPayload,
    "SandboxInfraRaised": SandboxInfraRaisedPayload,
    "SameDigestRetryRejected": SameDigestRetryRejectedPayload,
    "LeaseAcquired": LeaseAcquiredPayload,
    "ResultPending": ResultPendingPayload,
    "ReviewScheduled": ReviewScheduledPayload,
    "ReviewRecorded": ReviewRecordedPayload,
    "JudgeCasCorrupt": JudgeCasCorruptPayload,
    "ForkCreated": ForkCreatedPayload,
    "VerdictSnapshotsRecorded": VerdictSnapshotsRecordedPayload,
    "GraphNodeUpserted": GraphNodeUpsertedPayload,
    "GraphEdgeAdded": GraphEdgeAddedPayload,
    "EffectIntended": EffectIntendedPayload,
    "EffectDone": EffectDonePayload,
    "SideEffectDenied": SideEffectDeniedPayload,
    "SourceFetched": SourceFetchedPayload,
    "AttemptTainted": AttemptTaintedPayload,
    "FetchIntegrityRaised": FetchIntegrityRaisedPayload,
    "QuestionDisposed": QuestionDisposedPayload,
    "QuestionDisposeRejected": QuestionDisposeRejectedPayload,
    "CheckProposed": CheckProposedPayload,
    "CheckApproved": CheckApprovedPayload,
    "CheckRejected": CheckRejectedPayload,
}

# Back-compat name used by schema_gate / tests.
PAYLOAD_SPECS = PAYLOAD_MODELS


def validate_event_payload(event_type: str, raw: dict[str, Any]) -> dict[str, Any]:
    """Fail-closed typed check; returns ``raw`` unchanged (no defaults)."""
    model = PAYLOAD_MODELS.get(event_type)
    if model is None:
        raise JournalPayloadError(
            f"{event_type}: no payload schema registered",
            expected_store_format=None,
            found_store_format=None,
            loc=("payload",),
            error_type="untyped_payload",
        )
    if type(raw) is not dict:
        raise JournalPayloadError(
            f"{event_type}: payload must be object",
            expected_store_format=None,
            found_store_format=None,
            loc=("payload",),
            error_type="dict_type",
        )
    return _check(event_type, model, raw)
