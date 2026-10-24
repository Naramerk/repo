"""Typed MinimalReport (R16a / CAPABILITIES #16). ``extra=forbid`` on report rows."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, StrictBool, StrictInt, StrictStr

from coscientist.core.claims.reasons import Disposition
from coscientist.core.execution.domain import Provenance, TaskOutcome

_F = ConfigDict(frozen=True, extra="forbid", strict=True)


class MinimalReportRefuse(ValueError):
    """Refuse when a minimal_report payload cannot be constructed."""


class MinimalReportTaskRow(BaseModel):
    model_config = _F
    task_key: StrictStr
    kind: StrictStr
    outcome: TaskOutcome
    reason: StrictStr | None
    # None = DONE with no closed attempt (explicit not-started; never invent real).
    provenance: Provenance | None


class MinimalReportQuestionRow(BaseModel):
    model_config = _F
    question_id: StrictStr
    formulation: StrictStr
    disposition: Disposition
    reason: StrictStr
    note: StrictStr | None


class MinimalReportSections(BaseModel):
    model_config = _F
    real: list[StrictStr]
    stub: list[StrictStr]
    human: list[StrictStr]


class MinimalReport(BaseModel):
    """StudyFinalized.minimal_report / ``render_markdown_report`` input.

    R16a types task/question/sections rows (handoff). Hypotheses / custom_checks /
    observations stay structured dict/str payloads from finalize builders.
    """

    model_config = _F
    study_id: StrictStr
    terminal_reason: StrictStr
    trigger: StrictStr
    contributing_reasons: list[StrictStr]
    summary: StrictStr
    question_count: StrictInt
    not_attempted_count: StrictInt
    fallback_reason: StrictStr | None
    refusal: StrictStr | None
    verdict: StrictStr
    tasks: list[MinimalReportTaskRow]
    questions: list[MinimalReportQuestionRow]
    hypotheses: list[dict[str, Any]]
    hypotheses_unavailable: StrictStr | None
    sections: MinimalReportSections
    custom_checks: list[dict[str, Any]]
    observations: list[StrictStr | dict[str, Any]]
    truncated: StrictBool
