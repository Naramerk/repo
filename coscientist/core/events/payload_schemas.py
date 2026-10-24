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
    ref: str | None = None  # audit-ok[field_default]: LimitationRow.ref opt


class SupersededQuestion(BaseModel):
    model_config = _WIRE
    new_id: str
    old_id: str


# --- per-event wire models --------------------------------------------------------

class StudyCreatedPayload(BaseModel):
    """Wire shape after upcast; sparse journals may omit Study dump keys."""

    model_config = _WIRE
    study_id: str
    catalog_version: StrictInt
    profile: str | None = None  # audit-ok[field_default]: StudyCreated.profile opt
    request: str | None = None  # audit-ok[field_default]: StudyCreated.request opt
    hitl_policy: HitlPolicy | None = None  # audit-ok[field_default]: StudyCreated.hitl_policy opt
    deadline: str | None = None  # audit-ok[field_default]: StudyCreated.deadline opt
    created_at: str | None = None  # audit-ok[field_default]: StudyCreated.created_at opt
    parent_study_id: str | None = None  # audit-ok[field_default]: StudyCreated.parent_study_id opt
    budget: Budget | None = None  # audit-ok[field_default]: StudyCreated.budget opt
    finalize_reserve_s: StrictInt | None = None  # audit-ok[field_default]: StudyCreated.finalize_reserve_s opt
    confirmations_needed: StrictInt | None = None  # audit-ok[field_default]: StudyCreated.confirmations_needed opt
    refutations_needed: StrictInt | None = None  # audit-ok[field_default]: StudyCreated.refutations_needed opt
    refutation_priority: bool | None = None  # audit-ok[field_default]: StudyCreated.refutation_priority opt
    supported_needed: StrictInt | None = None  # audit-ok[field_default]: StudyCreated.supported_needed opt
    aggregate: Literal["count"] | None = None  # audit-ok[field_default]: StudyCreated.aggregate opt
    evalue_alpha: float | None = None  # audit-ok[field_default]: StudyCreated.evalue_alpha opt
    max_expansions: StrictInt | None = None  # audit-ok[field_default]: StudyCreated.max_expansions opt
    max_human_expansions: StrictInt | None = None  # audit-ok[field_default]: StudyCreated.max_human_expansions opt
    replan_reserve: float | None = None  # audit-ok[field_default]: StudyCreated.replan_reserve opt
    max_tasks: StrictInt | None = None  # audit-ok[field_default]: StudyCreated.max_tasks opt
    max_depth: StrictInt | None = None  # audit-ok[field_default]: StudyCreated.max_depth opt
    plan_timeout_s: StrictInt | None = None  # audit-ok[field_default]: StudyCreated.plan_timeout_s opt
    max_foreach_children: StrictInt | None = None  # audit-ok[field_default]: StudyCreated.max_foreach_children opt
    max_loop_iter: StrictInt | None = None  # audit-ok[field_default]: StudyCreated.max_loop_iter opt
    max_task_budget_share: float | None = None  # audit-ok[field_default]: StudyCreated.max_task_budget_share opt
    as_of: str | None = None  # audit-ok[field_default]: StudyCreated.as_of opt
    user_input_ids: list[str] | None = None  # audit-ok[field_default]: StudyCreated.user_input_ids opt
    max_proposals: StrictInt | None = None  # audit-ok[field_default]: StudyCreated.max_proposals opt


class StudyRefusedPayload(BaseModel):
    model_config = _WIRE
    kind: str
    reason: str
    classifier_error: bool
    catalog_version: StrictInt | None = None  # audit-ok[field_default]: StudyRefused.catalog_version opt


class StudyCancelledPayload(BaseModel):
    model_config = _WIRE
    study_id: str
    by: str
    at: str | None = None  # audit-ok[field_default]: StudyCancelled.at opt


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
    refusal: str | None = None  # audit-ok[field_default]: StudyFinalized.refusal opt
    hypothesis_verdicts: dict[str, JsonValue] | None = None  # audit-ok[field_default]: StudyFinalized.hv opt
    hypotheses_unavailable: str | None = None  # audit-ok[field_default]: StudyFinalized.hypotheses_unavailable opt
    hypothesis_report: list[JsonValue] | None = None  # audit-ok[field_default]: StudyFinalized.hypothesis_report opt
    custom_checks: list[JsonValue] | None = None  # audit-ok[field_default]: StudyFinalized.custom_checks opt
    observations: list[JsonValue] | None = None  # audit-ok[field_default]: StudyFinalized.observations opt


class TaskCreatedPayload(BaseModel):
    """Task dump on the wire (datetimes as ISO strings)."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    task_id: str
    kind: str
    spec: TaskSpec
    state: str
    outcome: str | None = None  # audit-ok[field_default]: TaskCreated.outcome opt
    reason: str | None = None  # audit-ok[field_default]: TaskCreated.reason opt
    deadline: str
    attempts: list[str]
    inherited_from: str | None = None  # audit-ok[field_default]: TaskCreated.inherited_from opt
    expand_after_task: str | None = None  # audit-ok[field_default]: TaskCreated.expand_after_task opt
    pending_expansion: dict[str, JsonValue] | None = None  # audit-ok[field_default]: TaskCreated.pending_expansion opt
    replan_allocated_s: float | None = None  # audit-ok[field_default]: TaskCreated.replan_allocated_s opt


class TaskInheritedPayload(TaskCreatedPayload):
    pass


class AttemptStartedPayload(BaseModel):
    """Writer emits n/provenance/study_id; sparse v1 journals may omit them / keep task_key."""

    model_config = _WIRE
    attempt_id: str
    task_id: str
    n: StrictInt | None = None  # audit-ok[field_default]: AttemptStarted.n sparse opt
    provenance: str | None = None  # audit-ok[field_default]: AttemptStarted.provenance sparse opt
    study_id: str | None = None  # audit-ok[field_default]: AttemptStarted.study_id sparse opt
    task_key: str | None = None  # audit-ok[field_default]: AttemptStarted.task_key legacy opt
    dispatch_id: str | None = None  # audit-ok[field_default]: AttemptStarted.dispatch_id opt
    route_returned: bool | None = None  # audit-ok[field_default]: AttemptStarted.route_returned opt
    closed: bool | None = None  # audit-ok[field_default]: AttemptStarted.closed opt
    close_reason: str | None = None  # audit-ok[field_default]: AttemptStarted.close_reason opt
    agent_output_digest: str | None = None  # audit-ok[field_default]: AttemptStarted.agent_output_digest opt
    deadline: str | None = None  # audit-ok[field_default]: AttemptStarted.deadline opt
    inputs: list[InputBinding] | None = None  # audit-ok[field_default]: AttemptStarted.inputs opt
    inputs_digest: str | None = None  # audit-ok[field_default]: AttemptStarted.inputs_digest opt
    tainted_context: bool | None = None  # audit-ok[field_default]: AttemptStarted.tainted_context opt
    limitations: list[LimitationRow] | None = None  # audit-ok[field_default]: AttemptStarted.limitations opt
    attempt_role: str | None = None  # audit-ok[field_default]: AttemptStarted.attempt_role opt
    allocated_budget_rub: str | None = None  # audit-ok[field_default]: AttemptStarted.allocated_budget_rub opt
    budget_limit_rub: str | None = None  # audit-ok[field_default]: AttemptStarted.budget_limit_rub opt
    share_renorm: bool | None = None  # audit-ok[field_default]: AttemptStarted.share_renorm opt


class AttemptClosedPayload(BaseModel):
    model_config = _WIRE
    attempt_id: str
    close_reason: str
    closed: bool | None = None  # audit-ok[field_default]: AttemptClosed.closed opt
    task_id: str | None = None  # audit-ok[field_default]: AttemptClosed.task_id opt
    retryable: bool | None = None  # audit-ok[field_default]: AttemptClosed.retryable opt
    retry_at: str | None = None  # audit-ok[field_default]: AttemptClosed.retry_at opt
    charged: bool | None = None  # audit-ok[field_default]: AttemptClosed.charged opt


class RouteReturnedPayload(BaseModel):
    model_config = _WIRE
    attempt_id: str
    agent_output_digest: str
    route: str | None = None  # audit-ok[field_default]: RouteReturned.route opt


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
    output_kinds: dict[str, str] | None = None  # audit-ok[field_default]: ResultRecorded.output_kinds opt
    output_map: dict[str, str] | None = None  # audit-ok[field_default]: ResultRecorded.output_map opt
    downgrade: dict[str, JsonValue] | None = None  # audit-ok[field_default]: ResultRecorded.downgrade opt
    expansion_request: dict[str, JsonValue] | None = None  # audit-ok[field_default]: ResultRecorded.er opt
    detail: str | None = None  # audit-ok[field_default]: ResultRecorded.detail opt
    claims_dropped: list[JsonValue] | dict[str, JsonValue] | None = None  # audit-ok[field_default]: RR.cd
    budget_overrun: bool | None = None  # audit-ok[field_default]: ResultRecorded.budget_overrun opt
    budget_limit_rub: str | None = None  # audit-ok[field_default]: ResultRecorded.budget_limit_rub opt
    agent_output_digest: str | None = None  # audit-ok[field_default]: ResultRecorded.agent_output_digest opt


class TaskDonePayload(BaseModel):
    model_config = _WIRE
    task_id: str
    outcome: str
    reason: str | None
    detail: str | None = None  # audit-ok[field_default]: TaskDone.detail opt
    budget_overrun: bool | None = None  # audit-ok[field_default]: TaskDone.budget_overrun opt


class LoopWire(BaseModel):
    """PlanAccepted.loops row; ``body`` may be corrupt str in restart fixtures."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    loop_id: str
    body: list[TaskSpec] | str
    until: UntilSpec | dict[str, JsonValue]
    max_iter: StrictInt
    scope_hypotheses: list[str] | None = None  # audit-ok[field_default]: LoopWire.scope_hypotheses opt
    scope_questions: list[str] | None = None  # audit-ok[field_default]: LoopWire.scope_questions opt
    depends_on: list[str] | None = None  # audit-ok[field_default]: LoopWire.depends_on opt


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
    subquestions: list[QuestionSpec] | None = None  # audit-ok[field_default]: PlanAccepted.subquestions opt
    rollup: str | None = None  # audit-ok[field_default]: PlanAccepted.rollup opt


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
    foreach: bool | None = None  # audit-ok[field_default]: PlanExpanded.foreach opt
    foreach_template: str | None = None  # audit-ok[field_default]: PlanExpanded.foreach_template opt
    foreach_items: list[JsonValue] | None = None  # audit-ok[field_default]: PlanExpanded.foreach_items opt
    foreach_kind: str | None = None  # audit-ok[field_default]: PlanExpanded.foreach_kind opt
    foreach_capped: bool | None = None  # audit-ok[field_default]: PlanExpanded.foreach_capped opt
    foreach_cap: StrictInt | None = None  # audit-ok[field_default]: PlanExpanded.foreach_cap opt
    foreach_total: StrictInt | None = None  # audit-ok[field_default]: PlanExpanded.foreach_total opt
    foreach_dropped: list[JsonValue] | None = None  # audit-ok[field_default]: PlanExpanded.foreach_dropped opt
    foreach_rank_notes: list[JsonValue] | None = None  # audit-ok[field_default]: PlanExpanded.foreach_rank_notes opt
    foreach_refute: bool | None = None  # audit-ok[field_default]: PlanExpanded.foreach_refute opt
    loop: bool | None = None  # audit-ok[field_default]: PlanExpanded.loop opt
    loop_id: str | None = None  # audit-ok[field_default]: PlanExpanded.loop_id opt
    loop_iter: StrictInt | None = None  # audit-ok[field_default]: PlanExpanded.loop_iter opt
    loop_max_iter: StrictInt | None = None  # audit-ok[field_default]: PlanExpanded.loop_max_iter opt
    loop_template_keys: list[str] | None = None  # audit-ok[field_default]: PlanExpanded.loop_template_keys opt
    loop_until: UntilSpec | dict[str, JsonValue] | None = None  # audit-ok[field_default]: PlanExpanded.loop_until opt


class ExpansionRefusedPayload(BaseModel):
    model_config = _WIRE
    after_task: str | None
    reason: str
    detail: str
    plan_key: str | None
    loop: bool | None = None  # audit-ok[field_default]: ExpansionRefused.loop opt
    loop_id: str | None = None  # audit-ok[field_default]: ExpansionRefused.loop_id opt
    loop_reason: str | None = None  # audit-ok[field_default]: ExpansionRefused.loop_reason opt
    report_line: str | None = None  # audit-ok[field_default]: ExpansionRefused.report_line opt


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
    detail: str | None = None  # audit-ok[field_default]: LoopCapped.detail opt
    refuse_detail: str | None = None  # audit-ok[field_default]: LoopCapped.refuse_detail opt


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
    payload_ref: str | None = None  # audit-ok[field_default]: HitlOpened.payload_ref opt
    deadline: str | None = None  # audit-ok[field_default]: HitlOpened.deadline opt
    study_id: str | None = None  # audit-ok[field_default]: HitlOpened.study_id opt
    expand_after_task: str | None = None  # audit-ok[field_default]: HitlOpened.expand_after_task opt


class HitlDecidedPayload(BaseModel):
    model_config = _WIRE
    request_id: str
    decision: str
    by: str
    reason: str
    spent_rub: float
    approved_by: str | None = None  # audit-ok[field_default]: HitlDecided.approved_by opt
    decided_by: str | None = None  # audit-ok[field_default]: HitlDecided.decided_by opt
    delegated_by: str | None = None  # audit-ok[field_default]: HitlDecided.delegated_by opt
    proposal_id: str | None = None  # audit-ok[field_default]: HitlDecided.proposal_id opt
    keep_open: bool | None = None  # audit-ok[field_default]: HitlDecided.keep_open opt
    effect: str | None = None  # audit-ok[field_default]: HitlDecided.effect opt
    human_message: str | None = None  # audit-ok[field_default]: HitlDecided.human_message opt
    cost_estimate_rub: float | None = None  # audit-ok[field_default]: HitlDecided.cost_estimate_rub opt
    cost_rub: float | None = None  # audit-ok[field_default]: HitlDecided.cost_rub opt


class HitlTimedOutPayload(BaseModel):
    model_config = _WIRE
    request_id: str
    reason: str
    spent_rub: float
    proposal_id: str | None = None  # audit-ok[field_default]: HitlTimedOut.proposal_id opt
    decided_by: str | None = None  # audit-ok[field_default]: HitlTimedOut.decided_by opt
    delegated_by: str | None = None  # audit-ok[field_default]: HitlTimedOut.delegated_by opt
    human_message: str | None = None  # audit-ok[field_default]: HitlTimedOut.human_message opt
    effect: str | None = None  # audit-ok[field_default]: HitlTimedOut.effect opt


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
    task_id: str | None = None  # audit-ok[field_default]: BudgetDebited.task_id opt
    actual_rub: str | float | None = None  # audit-ok[field_default]: BudgetDebited.actual_rub opt
    budget: BudgetSnapshot | None = None  # audit-ok[field_default]: BudgetDebited.budget opt
    cost_estimate_rub: float | None = None  # audit-ok[field_default]: BudgetDebited.cost_estimate_rub opt
    decided_by: str | None = None  # audit-ok[field_default]: BudgetDebited.decided_by opt
    delegated_by: str | None = None  # audit-ok[field_default]: BudgetDebited.delegated_by opt
    reason: str | None = None  # audit-ok[field_default]: BudgetDebited.reason opt


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
    media_type: str | None = None  # audit-ok[field_default]: ArtifactRegistered.media_type opt
    size: StrictInt | None = None  # audit-ok[field_default]: ArtifactRegistered.size opt
    uri: str | None = None  # audit-ok[field_default]: ArtifactRegistered.uri opt
    produced_by: ToolCallRef | None = None  # audit-ok[field_default]: ArtifactRegistered.produced_by opt
    schema_ref: str | None = None  # audit-ok[field_default]: ArtifactRegistered.schema_ref opt
    producer: str | None = None  # audit-ok[field_default]: ArtifactRegistered.producer opt
    net_isolation: str | None = None  # audit-ok[field_default]: ArtifactRegistered.net_isolation opt
    fs_isolation: str | None = None  # audit-ok[field_default]: ArtifactRegistered.fs_isolation opt
    tainted: bool | None = None  # audit-ok[field_default]: ArtifactRegistered.tainted opt
    source_uri: str | None = None  # audit-ok[field_default]: ArtifactRegistered.source_uri opt
    stamped: bool | None = None  # audit-ok[field_default]: ArtifactRegistered.stamped opt


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
    owner_nonce: str | None = None  # audit-ok[field_default]: LeaseAcquired.owner_nonce opt
    ttl_s: float | StrictInt | None = None  # audit-ok[field_default]: LeaseAcquired.ttl_s opt


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
    attempt_id: str | None = None  # audit-ok[field_default]: ReviewScheduled.attempt_id opt
    check_id: str | None = None  # audit-ok[field_default]: ReviewScheduled.check_id opt
    item_id: str | None = None  # audit-ok[field_default]: ReviewScheduled.item_id opt
    output_digest: str | None = None  # audit-ok[field_default]: ReviewScheduled.output_digest opt
    review_key: str | None = None  # audit-ok[field_default]: ReviewScheduled.review_key opt
    rubric_digest: str | None = None  # audit-ok[field_default]: ReviewScheduled.rubric_digest opt
    reviewer_task_id: str | None = None  # audit-ok[field_default]: ReviewScheduled.reviewer_task_id opt
    reviewer_attempt_id: str | None = None  # audit-ok[field_default]: ReviewScheduled.reviewer_attempt_id opt
    status: str | None = None  # audit-ok[field_default]: ReviewScheduled.status opt
    proposal_id: str | None = None  # audit-ok[field_default]: ReviewScheduled.proposal_id opt
    deadline: str | None = None  # audit-ok[field_default]: ReviewScheduled.deadline opt
    hitl_request_id: str | None = None  # audit-ok[field_default]: ReviewScheduled.hitl_request_id opt
    proposer_attempt_id: str | None = None  # audit-ok[field_default]: ReviewScheduled.proposer_attempt_id opt


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
    clarifications: list[JsonValue] | None = None  # audit-ok[field_default]: ForkCreated.clarifications opt
    digest: str | None = None  # audit-ok[field_default]: ForkCreated.digest opt


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
    ref_id: str | None = None  # audit-ok[field_default]: GraphNodeUpserted.ref_id opt
    parent_id: str | None = None  # audit-ok[field_default]: GraphNodeUpserted.parent_id opt
    created_at: str | None = None  # audit-ok[field_default]: GraphNodeUpserted.created_at opt


class GraphEdgeAddedPayload(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    edge_id: str
    kind: str
    study_id: str
    src_id: str
    dst_id: str
    attrs: dict[str, JsonValue]
    created_at: str | None = None  # audit-ok[field_default]: GraphEdgeAdded.created_at opt


class EffectIntendedPayload(BaseModel):
    model_config = _WIRE
    args_digest: str
    attempt_id: str
    effect: str
    key: str
    study_id: str
    task_key: str
    tool: str
    fingerprint: str | None = None  # audit-ok[field_default]: EffectIntended.fingerprint opt


class EffectDonePayload(BaseModel):
    model_config = _WIRE
    attempt_id: str
    key: str
    result_ref: str | None
    status: str
    tool: str
    detail: str | None = None  # audit-ok[field_default]: EffectDone.detail opt
    error_kind: str | None = None  # audit-ok[field_default]: EffectDone.error_kind opt


class SideEffectDeniedPayload(BaseModel):
    model_config = _WIRE
    attempt_id: str
    effect: str
    key: str
    reason: str
    task_key: str
    tool: str


class SourceFetchedPayload(BaseModel):
    """Writer journals call_id/tool/digest/url only (attempt_id derived from call_id)."""

    model_config = _WIRE
    call_id: str
    digest: str
    tool: str
    url: str
    attempt_id: str | None = None  # audit-ok[field_default]: SourceFetched.attempt_id not on writer wire


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
    row_key: str | None = None  # audit-ok[field_default]: QuestionDisposeRejected.row_key opt


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
