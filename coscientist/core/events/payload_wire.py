"""Strict nested wire helpers for event payload schemas. No silent defaults."""

from __future__ import annotations

from typing import ClassVar, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    StrictInt,
    model_validator,
)

from coscientist.core.execution.domain import (
    LoopPredicate,
    RejectedTemplateNote,
    TaskSpec,
)

_WIRE = ConfigDict(frozen=True, extra="forbid", strict=True)


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
