"""Fail-closed EventType payload schemas (writer wire shapes). No silent defaults."""

from __future__ import annotations

from typing import Annotated, ClassVar, Literal, assert_never

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    ValidationError,
    model_validator,
)

from coscientist.core.claims.graph import (
    GraphSchemaError,
    KindSpec,
    registry_from_schema,
    required_attr_names,
    resolve_kind_fields,
    validate_attr_value,
)
from coscientist.core.events.errors import JournalPayloadError
from coscientist.core.events.job_events import (
    JobCancelFailedPayload,
    JobCancelledPayload,
    JobGuestProcessGroupGonePayload,
    JobStatusChangedPayload,
    JobSubmittedPayload,
    JobTimedOutPayload,
)
from coscientist.core.events.mcp_events import (
    McpRequestRecordedPayload,
    McpResponseRecordedPayload,
)
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
    Need,
    NeedUnrecorded,
    QuestionSpec,
    QuestionSupersede,
    RejectedTemplateNote,
    TaskSpec,
    ToolCallRef,
)

_WIRE = ConfigDict(frozen=True, extra="forbid", strict=True)


def _validate_registry_attrs(
    *,
    kind: object,
    attrs: object,
    layer: Literal["node", "edge"],
) -> dict[str, object]:
    """Validate attrs via KindRegistry — same gate as ``graph_writer``.

    Kind must exist. Unknown keys refuse. Required KindSpec fields must be
    present (no invent). Each present field runs ``validate_attr_value``.
    """
    if not isinstance(kind, str) or not kind:
        raise ValueError(f"{layer} kind must be a non-empty str")
    if type(attrs) is not dict:
        raise ValueError("attrs must be a mapping")
    typed: dict[str, object] = {}
    for key, value in attrs.items():
        if type(key) is not str:
            raise ValueError("attrs keys must be str")
        typed[key] = value
    reg = registry_from_schema()
    if layer == "node":
        if kind not in reg.node_kinds():
            raise ValueError(f"unknown node kind: {kind}")
        spec: KindSpec = reg.require_node_kind(kind)
    elif layer == "edge":
        if kind not in reg.edge_kinds():
            raise ValueError(f"unknown edge kind: {kind}")
        spec = reg.require_edge_kind(kind)
    else:
        assert_never(layer)
    try:
        fields = resolve_kind_fields(spec, typed)
    except GraphSchemaError as e:
        raise ValueError(str(e)) from e
    known = {f.name: f for f in fields}
    try:
        required = required_attr_names(kind, typed, spec)
    except GraphSchemaError as e:
        raise ValueError(str(e)) from e
    missing = sorted(required - set(typed))
    if missing:
        raise ValueError(f"{kind} missing required attrs: {missing}")
    if not known:
        if typed:
            raise ValueError(f"{kind} has no schema fields but attrs were provided")
        return typed
    unknown = sorted(set(typed) - set(known))
    if unknown:
        raise ValueError(f"{kind} unknown attrs: {unknown}")
    for fname, value in typed.items():
        field = known[fname]
        null_ok = field.default is not None and field.default.value is None
        try:
            validate_attr_value(
                field.type,
                value,
                null_ok=null_ok,
                ctx=fname,
            )
        except GraphSchemaError as e:
            raise ValueError(f"{fname}: expected type {field.type.text}, got {type(value).__name__}") from e
    return typed


def _refuse(event_type: str, exc: ValidationError) -> None:
    first = exc.errors()[0]
    raise JournalPayloadError(
        f"{event_type}: payload schema refuse ({first['type']})",
        expected_store_format=None,
        found_store_format=None,
        loc=tuple(first["loc"]),
        error_type=str(first["type"]),
    ) from exc


def _check(event_type: str, model: type[BaseModel], raw: dict[str, object]) -> dict[str, object]:
    """Validate then return ``raw`` unchanged (no defaults written back)."""
    try:
        model.model_validate(raw)
    except ValidationError as e:
        _refuse(event_type, e)
    return raw


def _apply_writer_omitted(data: object, omitted: frozenset[str]) -> object:
    """For validate only: writer-omitted keys become None. ``raw`` stays unchanged."""
    if not omitted or not isinstance(data, dict):
        return data
    missing = omitted.difference(data)
    if not missing:
        return data
    out = dict(data)
    for key in missing:
        out[key] = None
    return out


# --- nested wire helpers (strict) -------------------------------------------------


class WireModel(BaseModel):
    """Strict fail-closed wire model.

    ``_writer_omitted`` lists keys the writer may leave absent (explicit contract).
    Missing keys outside that set still refuse. Returned ``raw`` is never filled.
    """

    model_config = _WIRE
    _writer_omitted: ClassVar[frozenset[str]] = frozenset()

    @model_validator(mode="before")
    @classmethod
    def _before_writer_omitted(cls, data: object) -> object:
        return _apply_writer_omitted(data, cls._writer_omitted)


class AttemptUnrecorded(WireModel):
    """Legacy AttemptStarted omitted this field — not a writer value."""

    kind: Literal["unrecorded"]
    source_schema_version: StrictInt


class LimitationRow(WireModel):
    _writer_omitted = frozenset({"ref"})

    text: str
    ref: str | None


class SupersededQuestion(WireModel):
    new_id: str
    old_id: str


class ExcludedClaimRow(WireModel):
    """excluded_claims_report / hypothesis excluded row."""

    _writer_omitted = frozenset({"claim_id", "attempt_id", "node_id"})

    reason: str
    claim_id: str | None
    attempt_id: str | None
    node_id: str | None


class CountedClaimRow(WireModel):
    """hypothesis_report_row counted[] entry."""

    _writer_omitted = frozenset({"intent", "severe", "independence_key"})

    claim_id: str
    outcome: str
    intent: str | None
    severe: bool | None
    independence_key: str | None


class QuestionDispositionRow(WireModel):
    _writer_omitted = frozenset(
        {
            "by_literature",
            "by_theory",
            "design_artifact_id",
            "excluded_claims",
            "final_type",
            "formulation",
            "k",
            "note",
            "provenance",
            "reason",
            "reason_detail",
            "ref",
            "refutation_priority",
            "refutations_needed",
            "report_line",
            "rollup",
            "rollup_children",
            "supported_needed",
            "target_kind",
            "terminal",
            "verdict",
        }
    )

    disposition: str
    reason: str | None
    formulation: str | None
    reason_detail: list[str] | None
    final_type: str | None
    verdict: str | None
    terminal: bool | None
    excluded_claims: list[str] | list[ExcludedClaimRow] | None
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
    _writer_omitted = frozenset({"provenance", "reason"})

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

    _writer_omitted = frozenset(
        {
            "auxiliary_assumptions",
            "by_literature",
            "by_theory",
            "claims",
            "conflict",
            "counted",
            "excluded",
            "excluded_claims",
            "hypothesis_id",
            "k",
            "legacy_claims",
            "not_tested",
            "note",
            "provenance",
            "question_id",
            "ref",
            "refutation_priority",
            "refutations_needed",
            "rule",
            "severity_note",
            "supported_needed",
            "target_kind",
            "verdict",
        }
    )

    hypothesis_id: str | None
    question_id: str | None
    verdict: str | None
    rule: str | None
    counted: list[CountedClaimRow] | None
    excluded: list[ExcludedClaimRow] | None
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
    excluded_claims: list[str] | list[ExcludedClaimRow] | None


class CustomCheckReportRow(WireModel):
    _writer_omitted = frozenset(
        {
            "approved_by",
            "approved_source",
            "bound_target",
            "bound_task_id",
            "catalog",
            "delegated",
            "justification",
            "measures",
            "note",
            "proposal_digest",
            "proposal_id",
            "reject_reason",
            "rejected_by",
            "rejected_templates",
            "role",
            "status",
            "target",
            "target_kind",
        }
    )

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
    _writer_omitted = frozenset({"human", "real", "stub"})

    human: list[str] | None
    stub: list[str] | None
    real: list[str] | None


class MinimalReport(WireModel):
    _writer_omitted = frozenset(
        {
            "custom_checks",
            "fallback_reason",
            "hypotheses",
            "hypotheses_unavailable",
            "not_attempted_count",
            "observations",
            "question_count",
            "refusal",
            "sections",
            "truncated",
        }
    )

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
    sections: MinimalReportSections | None
    custom_checks: list[CustomCheckReportRow] | None
    observations: list[ObservationRow | str] | None
    truncated: bool | None


class ClaimDrop(WireModel):
    key: str
    reason: str


class VerdictSnapshotRow(WireModel):
    _writer_omitted = frozenset(
        {
            "by_literature",
            "by_theory",
            "k",
            "provenance",
            "ref",
            "refutation_priority",
            "refutations_needed",
            "supported_needed",
            "target_kind",
            "verdict",
        }
    )

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

    _writer_omitted = frozenset({"depends_on", "scope_hypotheses", "scope_questions"})

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

    # Legacy journals may omit these; writer dumps always include nulls.
    _writer_omitted = frozenset({"parent_study_id", "budget"})

    study_id: str
    catalog_version: StrictInt
    profile: str
    request: str
    hitl_policy: HitlPolicy
    deadline: str
    created_at: str
    parent_study_id: str | None
    budget: Budget | None
    finalize_reserve_s: StrictInt
    confirmations_needed: StrictInt
    refutations_needed: StrictInt
    refutation_priority: bool
    supported_needed: StrictInt
    aggregate: Literal["count"]
    evalue_alpha: float
    max_expansions: StrictInt
    max_attempts: StrictInt
    max_human_expansions: StrictInt
    replan_reserve: float
    max_tasks: StrictInt
    max_depth: StrictInt
    plan_timeout_s: StrictInt
    max_foreach_children: StrictInt
    max_loop_iter: StrictInt
    max_task_budget_share: float
    as_of: str | None
    user_input_ids: list[str]
    max_proposals: StrictInt
    # G4 / StudyCreated v14: required bool (no invent on omit).
    require_hypotheses: bool


class StudyRefusedPayload(WireModel):
    _writer_omitted = frozenset({"catalog_version"})

    kind: str
    reason: str
    classifier_error: bool
    catalog_version: StrictInt | None


class StudyCancelledPayload(WireModel):
    _writer_omitted = frozenset({"at"})

    study_id: str
    by: str
    at: str | None


class StudyFinalizedPayload(WireModel):
    _writer_omitted = frozenset(
        {
            "custom_checks",
            "fallback_reason",
            "hypotheses_unavailable",
            "hypothesis_report",
            "hypothesis_verdicts",
            "minimal_report",
            "observations",
            "refusal",
            "report_artifact_id",
        }
    )

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

    _writer_omitted = frozenset(
        {
            "expand_after_task",
            "inherited_from",
            "outcome",
            "pending_expansion",
            "reason",
            "replan_allocated_s",
        }
    )

    # expand_* only on PLAN-n replan TaskCreated; absent on ordinary task dumps.
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
    """Writer wire shape (DESIGN §6.3).

    Legacy gaps → AttemptUnrecorded via upcasters (never invent values here).
    """

    _writer_omitted = frozenset(
        {
            "close_reason",
            "agent_output_digest",
            "inputs_digest",
            "deadline",
            "allocated_budget_rub",
            "budget_limit_rub",
            "share_renorm",
        }
    )

    attempt_id: str
    n: StrictInt | AttemptUnrecorded
    dispatch_id: str | AttemptUnrecorded
    route_returned: bool | AttemptUnrecorded
    closed: bool | AttemptUnrecorded
    close_reason: str | None
    provenance: str | AttemptUnrecorded
    agent_output_digest: str | None
    task_id: str
    study_id: str | AttemptUnrecorded
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
    _writer_omitted = frozenset(
        {
            "charged",
            "closed",
            "retry_at",
            "retryable",
            "task_id",
        }
    )

    attempt_id: str
    close_reason: str
    closed: bool | None
    task_id: str | None
    retryable: bool | None
    retry_at: str | None
    charged: bool | None


class RouteReturnedPayload(WireModel):
    _writer_omitted = frozenset({"route"})

    attempt_id: str
    agent_output_digest: str
    route: str | None


class ResultRecordedPayload(WireModel):
    _writer_omitted = frozenset(
        {
            "agent_claim",
            "agent_output_digest",
            "budget_limit_rub",
            "budget_overrun",
            "claims_dropped",
            "detail",
            "downgrade",
            "expansion_request",
            "output_kinds",
            "output_map",
            "reason",
        }
    )

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
    _writer_omitted = frozenset({"budget_overrun", "detail", "reason"})

    task_id: str
    outcome: str
    reason: str | None
    detail: str | None
    budget_overrun: bool | None


class PlanAcceptedPayload(WireModel):
    _writer_omitted = frozenset({"hypothesis_id", "rollup", "subquestions"})

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
    # G4 PlanAccepted v12: Need | NeedUnrecorded only (no loose str).
    hypotheses: Annotated[Need | NeedUnrecorded, Field(discriminator="kind")]
    literature: Annotated[Need | NeedUnrecorded, Field(discriminator="kind")]


class PlanExpandedPayload(WireModel):
    _writer_omitted = frozenset(
        {
            "after_task",
            "delegated_by",
            "foreach",
            "foreach_cap",
            "foreach_capped",
            "foreach_dropped",
            "foreach_items",
            "foreach_kind",
            "foreach_rank_notes",
            "foreach_refute",
            "foreach_template",
            "foreach_total",
            "hitl_request_id",
            "hypothesis_id",
            "loop",
            "loop_id",
            "loop_iter",
            "loop_max_iter",
            "loop_template_keys",
            "loop_until",
            "plan_key",
            "prev_plan_id",
        }
    )

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
    # G4: Plan dump carries hypotheses/literature Need (same as PlanAccepted).
    hypotheses: Annotated[Need | NeedUnrecorded, Field(discriminator="kind")]
    literature: Annotated[Need | NeedUnrecorded, Field(discriminator="kind")]
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
    _writer_omitted = frozenset(
        {
            "after_task",
            "loop",
            "loop_id",
            "loop_reason",
            "plan_key",
            "report_line",
        }
    )

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
    _writer_omitted = frozenset({"detail", "refuse_detail", "survivors"})

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
    _writer_omitted = frozenset(
        {
            "deadline",
            "expand_after_task",
            "payload_ref",
            "study_id",
        }
    )

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
    _writer_omitted = frozenset(
        {
            "approved_by",
            "cost_estimate_rub",
            "cost_rub",
            "decided_by",
            "delegated_by",
            "effect",
            "human_message",
            "keep_open",
            "proposal_id",
        }
    )

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
    _writer_omitted = frozenset(
        {
            "decided_by",
            "delegated_by",
            "effect",
            "human_message",
            "proposal_id",
        }
    )

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
    _writer_omitted = frozenset(
        {
            "actual_rub",
            "budget",
            "cost_estimate_rub",
            "decided_by",
            "delegated_by",
            "reason",
            "task_id",
        }
    )

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

    _writer_omitted = frozenset(
        {
            "fs_isolation",
            "kind",
            "media_type",
            "net_isolation",
            "produced_by",
            "producer",
            "schema_ref",
            "size",
            "source_uri",
            "stamped",
            "tainted",
            "uri",
        }
    )

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
    _writer_omitted = frozenset({"owner_nonce", "ttl_s"})

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
    _writer_omitted = frozenset(
        {
            "attempt_id",
            "check_id",
            "deadline",
            "hitl_request_id",
            "item_id",
            "output_digest",
            "proposal_id",
            "proposer_attempt_id",
            "review_key",
            "reviewer_attempt_id",
            "reviewer_task_id",
            "rubric_digest",
            "status",
        }
    )

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
    _writer_omitted = frozenset({"failure_kind"})

    attempt_id: str
    check_id: str
    failure_kind: str | None
    item_id: str
    output_digest: str
    # Writer stamps int; corrupt journals may carry bool/object for judge refuse path.
    retry: StrictInt | bool | dict[str, bool]
    review_key: str
    status: str
    task_id: str


class JudgeCasCorruptPayload(WireModel):
    detail: str
    report_line: str


class ForkCreatedPayload(WireModel):
    """Durable fork journal wire (#160): partition + clarifications + nonce."""

    parent_id: str
    child_id: str
    idem_key: str
    affected: list[str]
    inherited: list[str]
    digests: dict[str, str]
    clarifications: list[Clarification]
    nonce: str


class VerdictSnapshotsRecordedPayload(WireModel):
    attempt_id: str
    scope: str
    snapshots: dict[str, VerdictSnapshotRow]
    study_id: str


class GraphNodeUpsertedPayload(WireModel):
    """GraphNode wire dump; attrs via KindRegistry AttrType / validate_attr_value."""

    _writer_omitted = frozenset({"created_at", "parent_id", "ref_id"})

    node_id: str
    kind: str
    study_id: str
    attrs: dict[str, object]
    ref_id: str | None
    parent_id: str | None
    created_at: str | None

    @model_validator(mode="after")
    def _attrs_registry(self) -> GraphNodeUpsertedPayload:
        _validate_registry_attrs(kind=self.kind, attrs=self.attrs, layer="node")
        return self


class GraphEdgeAddedPayload(WireModel):
    """GraphEdge wire dump; attrs via KindRegistry AttrType / validate_attr_value."""

    _writer_omitted = frozenset({"created_at"})

    edge_id: str
    kind: str
    study_id: str
    src_id: str
    dst_id: str
    attrs: dict[str, object]
    created_at: str | None

    @model_validator(mode="after")
    def _attrs_registry(self) -> GraphEdgeAddedPayload:
        _validate_registry_attrs(kind=self.kind, attrs=self.attrs, layer="edge")
        return self


class EffectIntendedPayload(WireModel):
    _writer_omitted = frozenset({"fingerprint"})

    args_digest: str
    attempt_id: str
    effect: str
    key: str
    study_id: str
    task_key: str
    tool: str
    fingerprint: str | None


class EffectDonePayload(WireModel):
    _writer_omitted = frozenset({"detail", "error_kind", "result_ref"})

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
    """Writer journals call_id/tool/digest/url; attempt_id optional on v1 wire."""

    _writer_omitted = frozenset({"attempt_id"})

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
    _writer_omitted = frozenset({"design_artifact_id"})

    design_artifact_id: str | None
    disposition: str
    question_id: str
    reason: str
    refs: list[str]


class QuestionDisposeRejectedPayload(WireModel):
    _writer_omitted = frozenset({"row_key"})

    detail: str
    disposition: str
    question_id: str
    reason: str
    report_line: str
    row_key: str | None


class CheckProposedPayload(WireModel):
    _writer_omitted = frozenset(
        {
            "check",
            "predicted_direction",
            "predicts_null",
            "rubric",
            "sesoi",
            "threshold",
        }
    )

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
    _writer_omitted = frozenset({"hitl_request_id", "human_principal", "reviewer_attempt_id"})

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
    _writer_omitted = frozenset({"hitl_request_id", "human_principal", "reviewer_attempt_id"})

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


MatchKind = Literal["exact", "none"]


class MatchInfo(WireModel):
    kind: MatchKind
    ranked: list[str]


class ToolResolvedPayload(WireModel):
    """G3 ToolResolved — same WireModel path as every other PAYLOAD_MODELS entry."""

    task_id: str = Field(min_length=1)
    status: Literal["tool_found", "tool_missing"]
    tool: str | None
    catalog_version: StrictInt = Field(ge=0)
    match: MatchInfo

    @model_validator(mode="after")
    def _status_match_tool(self) -> ToolResolvedPayload:
        found = self.status == "tool_found"
        if found != (self.tool is not None):
            raise ValueError(
                "tool_found requires tool" if found else "tool_missing requires tool=None"
            )
        want = "exact" if found else "none"
        if self.match.kind != want:
            raise ValueError(f"{self.status} requires match.kind={want}")
        if found and self.match.ranked != [self.tool]:
            raise ValueError("tool_found requires ranked=[tool]")
        return self


class ToolCalledPayload(WireModel):
    """TG-Call ToolCalled — DESIGN §10 observability on every invoke_tool call."""

    ref: ToolCallRef
    outcome: Literal["ok", "error"]
    error_kind: str | None
    duration_ms: StrictInt = Field(ge=0)

    @model_validator(mode="after")
    def _outcome_error_kind(self) -> ToolCalledPayload:
        if self.outcome == "ok":
            if self.error_kind is not None:
                raise ValueError("ok requires error_kind=None")
        elif self.error_kind is None or type(self.error_kind) is not str or not self.error_kind:
            raise ValueError("error requires non-empty error_kind")
        if not self.ref.call_id or not self.ref.tool or not self.ref.args_digest:
            raise ValueError("ref.call_id/tool/args_digest must be non-empty")
        if self.ref.ended_at is None:
            raise ValueError("ref.ended_at required for ToolCalled")
        return self


class HypothesisProposedPayload(WireModel):
    """RC9c HypothesisProposed — CAP #2 / DESIGN §2 hyp row (owner → G7 node)."""

    _writer_omitted = frozenset({"source_task", "rationale"})

    hypothesis_id: str = Field(min_length=1)
    statement: str = Field(min_length=1)
    prediction: str = Field(min_length=1)
    rank: StrictInt
    selected: bool
    source_task: str | None
    rationale: str | None

    @model_validator(mode="after")
    def _nonblank_and_optionals(self) -> HypothesisProposedPayload:
        for name in ("hypothesis_id", "statement", "prediction"):
            v = getattr(self, name)
            if type(v) is not str or not v or v.strip() != v:
                raise ValueError(
                    f"{name} must be non-empty without surrounding whitespace"
                )
        if type(self.selected) is not bool:
            raise ValueError("selected must be bool")
        if type(self.rank) is not int or isinstance(self.rank, bool):
            raise ValueError("rank must be int")
        for opt in ("source_task", "rationale"):
            ov = getattr(self, opt)
            if ov is None:
                continue
            if type(ov) is not str or not ov or ov.strip() != ov:
                raise ValueError(
                    f"{opt} must be non-empty without surrounding whitespace"
                )
        return self


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
    "JobSubmitted": JobSubmittedPayload,
    "JobStatusChanged": JobStatusChangedPayload,
    "JobTimedOut": JobTimedOutPayload,
    "JobCancelled": JobCancelledPayload,
    "JobCancelFailed": JobCancelFailedPayload,
    "JobGuestProcessGroupGone": JobGuestProcessGroupGonePayload,
    "ToolResolved": ToolResolvedPayload,
    "McpRequestRecorded": McpRequestRecordedPayload,
    "McpResponseRecorded": McpResponseRecordedPayload,
    "ToolCalled": ToolCalledPayload,
    "HypothesisProposed": HypothesisProposedPayload,
}


def validate_event_payload(event_type: str, raw: object) -> dict[str, object]:
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
