"""Fail-closed EventType payload schemas (writer wire shapes). No silent defaults."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, StrictInt, ValidationError, model_validator

from coscientist.core.events.errors import JournalPayloadError
from coscientist.core.execution.domain import (
    Budget,
    CheckResult,
    Clarification,
    CodeCheck,
    Downgrade,
    ExpansionRequest,
    HitlPolicy,
    InputBinding,
    LoopPredicate,
    LoopSpec,
    QuestionSpec,
    QuestionSupersede,
    RejectedTemplateNote,
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


def _annotation_allows_none(annotation: object) -> bool:
    if annotation is type(None):
        return True
    origin = getattr(annotation, "__origin__", None)
    args = getattr(annotation, "__args__", ())
    if origin is None and args:
        # typing.Union / X | Y stored as UnionType
        return any(_annotation_allows_none(a) for a in args)
    if origin is getattr(__import__("typing"), "Union", None):
        return any(_annotation_allows_none(a) for a in args)
    # Python 3.10+ types.UnionType
    import types
    if isinstance(annotation, types.UnionType):
        return any(_annotation_allows_none(a) for a in annotation.__args__)
    return False


class WireModel(BaseModel):
    """Strict fail-closed wire model; nullable keys may be absent before validate."""

    model_config = _WIRE

    @model_validator(mode="before")
    @classmethod
    def _fill_nullable_keys(cls, data: object) -> object:
        if not isinstance(data, dict):
            return data
        out = dict(data)
        for name, field in cls.model_fields.items():
            if _annotation_allows_none(field.annotation):
                out.setdefault(name, None)
        return out



Scalar = str | int | float | bool | None | list[str] | list[int] | dict[str, str | int | float | bool | None]


class LimitationRow(WireModel):
    text: str
    ref: str | None


class SupersededQuestion(WireModel):
    new_id: str
    old_id: str


class QuestionDispositionRow(WireModel):
    disposition: str
    reason: str | None
    formulation: str | None
    reason_detail: list[str] | None
    final_type: str | None
    verdict: str | None
    terminal: bool | None
    excluded_claims: list[str] | list[dict[str, Scalar]] | None
    report_line: str | None
    design_artifact_id: str | None
    rollup: str | None
    rollup_children: list[str] | None
    by_literature: bool | StrictInt | None
    by_theory: bool | StrictInt | None
    provenance: str | None
    ref: str | None
    target_kind: str | None
    k: StrictInt | None
    supported_needed: StrictInt | None
    refutations_needed: StrictInt | None
    refutation_priority: bool | None
    note: str | None


class MinimalReportTaskRow(WireModel):
    task_key: str
    kind: str
    outcome: str
    reason: str | None
    provenance: str | None


class MinimalReportQuestionRow(WireModel):
    question_id: str
    formulation: str
    disposition: str
    reason: str


class HypothesisReportRow(WireModel):
    """StudyFinalized.hypothesis_verdicts / minimal_report.hypotheses row."""

    hypothesis_id: str | None
    question_id: str | None
    verdict: str | None
    rule: str | None
    counted: list[dict[str, Scalar]] | None
    excluded: list[dict[str, Scalar]] | None
    conflict: dict[str, list[str]] | None
    severity_note: str | None
    auxiliary_assumptions: list[str] | None
    legacy_claims: StrictInt | None
    not_tested: bool | None
    claims: list[str] | None
    # PR8 snapshot fields merged onto the report row for bind/digest consumers.
    ref: str | None
    target_kind: str | None
    by_literature: bool | StrictInt | None
    by_theory: bool | StrictInt | None
    provenance: str | None
    refutations_needed: StrictInt | None
    refutation_priority: bool | None
    k: StrictInt | None
    supported_needed: StrictInt | None
    note: str | None
    excluded_claims: list[str] | list[dict[str, Scalar]] | None


class CustomCheckReportRow(WireModel):
    proposal_id: str | None
    proposal_digest: str | None
    target: str | None
    target_kind: str | None
    role: str | None
    measures: str | None
    status: str | None
    delegated: bool | None
    approved_by: str | None
    approved_source: str | None
    rejected_by: str | None
    reject_reason: str | None
    justification: str | None
    rejected_templates: list[RejectedTemplateNote] | list[str] | None
    bound_task_id: str | None
    bound_target: str | None
    catalog: bool | None
    note: str | None


class ObservationRow(WireModel):
    text: str


class MinimalReportSections(WireModel):
    human: list[str] | None
    stub: list[str] | None
    real: list[str] | None


class MinimalReport(WireModel):
    study_id: str
    terminal_reason: str
    trigger: str
    contributing_reasons: list[str]
    summary: str
    question_count: StrictInt | None
    not_attempted_count: StrictInt | None
    fallback_reason: str | None
    refusal: str | None
    verdict: str
    tasks: list[MinimalReportTaskRow]
    questions: list[MinimalReportQuestionRow]
    hypotheses: list[HypothesisReportRow] | None
    hypotheses_unavailable: str | None
    sections: MinimalReportSections | dict[str, list[str]] | None
    custom_checks: list[CustomCheckReportRow] | None
    observations: list[ObservationRow | str] | None
    truncated: bool | None


class ClaimDrop(WireModel):
    key: str
    reason: str


class VerdictSnapshotRow(WireModel):
    ref: str | None
    target_kind: str | None
    verdict: str | None
    by_literature: bool | StrictInt | None
    by_theory: bool | StrictInt | None
    provenance: str | None
    refutations_needed: StrictInt | None
    refutation_priority: bool | None
    k: StrictInt | None
    supported_needed: StrictInt | None


class LoopWire(WireModel):
    """PlanAccepted.loops row; ``body`` may be corrupt str in restart fixtures."""

    loop_id: str
    body: list[TaskSpec] | str
    until: LoopPredicate
    max_iter: StrictInt
    scope_hypotheses: list[str] | None
    scope_questions: list[str] | None
    depends_on: list[str] | None


# --- per-event wire models --------------------------------------------------------

class StudyCreatedPayload(WireModel):
    """Writer Study dump after upcast (ISO datetimes as str)."""

    study_id: str
    catalog_version: StrictInt
    profile: str | None
    request: str | None
    hitl_policy: HitlPolicy | None
    deadline: str | None
    created_at: str | None
    parent_study_id: str | None
    budget: Budget | None
    finalize_reserve_s: StrictInt | None
    confirmations_needed: StrictInt | None
    refutations_needed: StrictInt | None
    refutation_priority: bool | None
    supported_needed: StrictInt | None
    aggregate: Literal["count"] | None
    evalue_alpha: float | None
    max_expansions: StrictInt | None
    max_human_expansions: StrictInt | None
    replan_reserve: float | None
    max_tasks: StrictInt | None
    max_depth: StrictInt | None
    plan_timeout_s: StrictInt | None
    max_foreach_children: StrictInt | None
    max_loop_iter: StrictInt | None
    max_task_budget_share: float | None
    as_of: str | None
    user_input_ids: list[str] | None
    max_proposals: StrictInt | None


class StudyRefusedPayload(WireModel):
    kind: str
    reason: str
    classifier_error: bool
    catalog_version: StrictInt | None


class StudyCancelledPayload(WireModel):
    study_id: str
    by: str
    at: str | None


class StudyFinalizedPayload(WireModel):
    trigger: str
    terminal_reason: str
    contributing_reasons: list[str]
    verdict: str
    question_dispositions: dict[str, QuestionDispositionRow]
    report_artifact_id: str | None
    conclusion_ids: list[str]
    minimal_report: MinimalReport | None
    fallback_reason: str | None
    refusal: str | None
    hypothesis_verdicts: dict[str, HypothesisReportRow] | None
    hypotheses_unavailable: str | None
    hypothesis_report: list[HypothesisReportRow] | None
    custom_checks: list[CustomCheckReportRow] | None
    observations: list[ObservationRow | str] | None


class TaskCreatedPayload(WireModel):
    """Task dump on the wire (datetimes as ISO strings)."""

    task_id: str
    kind: str
    spec: TaskSpec
    state: str
    outcome: str | None
    reason: str | None
    deadline: str
    attempts: list[str]
    inherited_from: str | None
    expand_after_task: str | None
    pending_expansion: ExpansionRequest | None
    replan_allocated_s: float | None


class TaskInheritedPayload(TaskCreatedPayload):
    pass


class AttemptStartedPayload(WireModel):
    """Writer wire shape (DESIGN §6.3). Legacy rows normalized before validate."""

    @model_validator(mode="before")
    @classmethod
    def _normalize_legacy(cls, data: object) -> object:
        if not isinstance(data, dict):
            return data
        out = dict(data)
        out.pop("task_key", None)
        if "n" not in out:
            out["n"] = 1
        if "provenance" not in out:
            out["provenance"] = "real"
        if "study_id" not in out:
            task_id = str(out.get("task_id") or "")
            out["study_id"] = task_id.split("/", 1)[0] if "/" in task_id else task_id
        out.setdefault("dispatch_id", f"dispatch:{out.get('attempt_id')}")
        out.setdefault("route_returned", False)
        out.setdefault("closed", False)
        out.setdefault("inputs", [])
        out.setdefault("tainted_context", False)
        out.setdefault("limitations", [])
        out.setdefault("attempt_role", "agent")
        return out

    attempt_id: str
    n: StrictInt
    dispatch_id: str
    route_returned: bool
    closed: bool
    close_reason: str | None
    provenance: str
    agent_output_digest: str | None
    task_id: str
    study_id: str
    inputs: list[InputBinding]
    inputs_digest: str | None
    tainted_context: bool
    limitations: list[LimitationRow]
    attempt_role: str
    deadline: str | None
    allocated_budget_rub: str | None
    budget_limit_rub: str | None
    share_renorm: bool | None


class AttemptClosedPayload(WireModel):
    attempt_id: str
    close_reason: str
    closed: bool | None
    task_id: str | None
    retryable: bool | None
    retry_at: str | None
    charged: bool | None


class RouteReturnedPayload(WireModel):
    attempt_id: str
    agent_output_digest: str
    route: str | None


class ResultRecordedPayload(WireModel):
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
    inputs: list[InputBinding]
    payload_digest: str
    charged: bool
    result_class: str
    output_kinds: dict[str, str] | None
    output_map: dict[str, str] | None
    downgrade: Downgrade | None
    expansion_request: ExpansionRequest | None
    detail: str | None
    claims_dropped: list[ClaimDrop] | None
    budget_overrun: bool | None
    budget_limit_rub: str | None
    agent_output_digest: str | None


class TaskDonePayload(WireModel):
    task_id: str
    outcome: str
    reason: str | None
    detail: str | None
    budget_overrun: bool | None


class PlanAcceptedPayload(WireModel):
    plan_id: str
    study_id: str
    revision: StrictInt
    hypothesis_id: str | None
    tasks: list[TaskSpec]
    questions: list[QuestionSpec]
    expand_after: list[str]
    loops: list[LoopWire | LoopSpec]
    subquestions: list[QuestionSpec] | None
    rollup: str | None


class PlanExpandedPayload(WireModel):
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
    superseded_questions: list[QuestionSupersede]
    origin: str
    reason: str
    delegated_by: str | None
    replan_allocated_s: float
    prev_plan_id: str | None
    hitl_request_id: str | None
    plan_key: str | None
    foreach: bool | None
    foreach_template: str | None
    foreach_items: list[str] | None
    foreach_kind: str | None
    foreach_capped: bool | None
    foreach_cap: StrictInt | None
    foreach_total: StrictInt | None
    foreach_dropped: list[str] | None
    foreach_rank_notes: list[str] | None
    foreach_refute: bool | None
    loop: bool | None
    loop_id: str | None
    loop_iter: StrictInt | None
    loop_max_iter: StrictInt | None
    loop_template_keys: list[str] | None
    loop_until: LoopPredicate | None


class ExpansionRefusedPayload(WireModel):
    after_task: str | None
    reason: str
    detail: str
    plan_key: str | None
    loop: bool | None
    loop_id: str | None
    loop_reason: str | None
    report_line: str | None


class PlanRefusedPayload(WireModel):
    reason: str
    detail: str
    report_line: str


class LoopCappedPayload(WireModel):
    loop_id: str
    iter: StrictInt
    max_iter: StrictInt
    survivors: StrictInt | None
    scope_hypotheses: list[str]
    scope_questions: list[str]
    until: LoopPredicate
    reason: str
    detail: str | None
    refuse_detail: str | None


class LoopCompletedPayload(WireModel):
    loop_id: str
    iter: StrictInt
    max_iter: StrictInt
    survivors: StrictInt
    scope_hypotheses: list[str]
    scope_questions: list[str]
    until: LoopPredicate
    reason: str


class LoopRefusedPayload(WireModel):
    loop_id: str
    iter: StrictInt
    max_iter: StrictInt
    reason: str
    detail: str
    scope_hypotheses: list[str]
    scope_questions: list[str]
    until: LoopPredicate


class HitlOpenedPayload(WireModel):
    request_id: str
    kind: str
    open: bool
    task_id: str
    cost_rub: float
    payload_ref: str | None
    deadline: str | None
    study_id: str | None
    expand_after_task: str | None


class HitlDecidedPayload(WireModel):
    request_id: str
    decision: str
    by: str
    reason: str
    spent_rub: float
    approved_by: str | None
    decided_by: str | None
    delegated_by: str | None
    proposal_id: str | None
    keep_open: bool | None
    effect: str | None
    human_message: str | None
    cost_estimate_rub: float | None
    cost_rub: float | None


class HitlTimedOutPayload(WireModel):
    request_id: str
    reason: str
    spent_rub: float
    proposal_id: str | None
    decided_by: str | None
    delegated_by: str | None
    human_message: str | None
    effect: str | None


class HitlDeadlineExtendedPayload(WireModel):
    request_id: str
    deadline: str


class BudgetSnapshot(WireModel):
    blocked: bool
    limit_rub: float
    spent_rub: float


class BudgetDebitedPayload(WireModel):
    request_id: str
    spent_rub: str
    task_id: str | None
    actual_rub: str | float | None
    budget: BudgetSnapshot | None
    cost_estimate_rub: float | None
    decided_by: str | None
    delegated_by: str | None
    reason: str | None


class BudgetExceededPayload(WireModel):
    scope: str


class BudgetSharesAdjustedPayload(WireModel):
    request_id: str
    plan_id: str
    budget_shares: dict[str, float]
    deadline_shares: dict[str, float]
    tasks: list[TaskSpec]
    approved_by: str


class BudgetSharesRefusedPayload(WireModel):
    request_id: str
    approved_by: str
    detail: str
    reason: str


class ArtifactRegisteredPayload(WireModel):
    """Full Artifact dump or kind-stamp subset."""

    artifact_id: str
    name: str
    sha256: str
    role: str
    kind: str | None
    media_type: str | None
    size: StrictInt | None
    uri: str | None
    produced_by: ToolCallRef | None
    schema_ref: str | None
    producer: str | None
    net_isolation: str | None
    fs_isolation: str | None
    tainted: bool | None
    source_uri: str | None
    stamped: bool | None


class LateWriteRejectedPayload(WireModel):
    attempt_id: str
    reason: str


class DowngradeRejectedPayload(WireModel):
    attempt_id: str
    reason: str


class CatalogErrorRaisedPayload(WireModel):
    error: str
    reason: str


class SandboxInfraRaisedPayload(WireModel):
    error: str
    reason: str


class SameDigestRetryRejectedPayload(WireModel):
    error: str
    reason: str


class LeaseAcquiredPayload(WireModel):
    owner: str
    epoch: StrictInt
    owner_nonce: str | None
    ttl_s: float | StrictInt | None


class ResultPendingPayload(WireModel):
    attempt_id: str
    task_id: str
    agent_output_digest: str
    check_results: list[CheckResult]
    reason: str


class ReviewScheduledPayload(WireModel):
    kind: str
    task_id: str
    attempt_id: str | None
    check_id: str | None
    item_id: str | None
    output_digest: str | None
    review_key: str | None
    rubric_digest: str | None
    reviewer_task_id: str | None
    reviewer_attempt_id: str | None
    status: str | None
    proposal_id: str | None
    deadline: str | None
    hitl_request_id: str | None
    proposer_attempt_id: str | None


class ReviewRecordedPayload(WireModel):
    attempt_id: str
    check_id: str
    failure_kind: str | None
    item_id: str
    output_digest: str
    retry: StrictInt | bool | dict[str, Scalar]
    review_key: str
    status: str
    task_id: str


class JudgeCasCorruptPayload(WireModel):
    detail: str
    report_line: str


class ForkCreatedPayload(WireModel):
    parent_study_id: str
    study_id: str
    clarifications: list[Clarification] | None
    digest: str | None


class VerdictSnapshotsRecordedPayload(WireModel):
    attempt_id: str
    scope: str
    snapshots: dict[str, VerdictSnapshotRow]
    study_id: str


class GraphNodeUpsertedPayload(WireModel):
    node_id: str
    kind: str
    study_id: str
    attrs: dict[str, Scalar]
    ref_id: str | None
    parent_id: str | None
    created_at: str | None


class GraphEdgeAddedPayload(WireModel):
    edge_id: str
    kind: str
    study_id: str
    src_id: str
    dst_id: str
    attrs: dict[str, Scalar]
    created_at: str | None


class EffectIntendedPayload(WireModel):
    args_digest: str
    attempt_id: str
    effect: str
    key: str
    study_id: str
    task_key: str
    tool: str
    fingerprint: str | None


class EffectDonePayload(WireModel):
    attempt_id: str
    key: str
    result_ref: str | None
    status: str
    tool: str
    detail: str | None
    error_kind: str | None


class SideEffectDeniedPayload(WireModel):
    attempt_id: str
    effect: str
    key: str
    reason: str
    task_key: str
    tool: str


class SourceFetchedPayload(WireModel):
    """Writer journals call_id/tool/digest/url; some rows also carry attempt_id."""

    call_id: str
    digest: str
    tool: str
    url: str
    attempt_id: str | None


class AttemptTaintedPayload(WireModel):
    attempt_id: str
    call_id: str
    is_fetch: bool
    reason: str
    tool: str


class FetchIntegrityRaisedPayload(WireModel):
    call_id: str
    internal_error: bool
    kind: str
    tool: str


class QuestionDisposedPayload(WireModel):
    design_artifact_id: str | None
    disposition: str
    question_id: str
    reason: str
    refs: list[str]


class QuestionDisposeRejectedPayload(WireModel):
    detail: str
    disposition: str
    question_id: str
    reason: str
    report_line: str
    row_key: str | None


class CheckProposedPayload(WireModel):
    attempt_id: str
    check: CodeCheck | None
    justification: str
    measures: str
    predicted_direction: str | None
    predicts_null: bool | None
    proposal_digest: str
    proposal_id: str
    proposer_attempt_id: str
    rejected_templates: list[RejectedTemplateNote]
    role: str
    rubric: str | None
    sesoi: float | None
    study_id: str
    target: str
    target_kind: str
    task_id: str
    threshold: float | None
    uses_sesoi: bool


class CheckApprovedPayload(WireModel):
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


class CheckRejectedPayload(WireModel):
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
