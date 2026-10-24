"""Reviewer role typed output (DESIGN §5.1 / §7.1).

``reviewer`` returns a schema-valid ``ReviewVerdict`` via ``AgentPort.run``
(after G1 / adapter). This module is types + prompt ref only — **no Protocol**.

Evidence rows mirror domain ``EvidenceRef`` (artifact_id, digest, optional
json_pointer) without importing ``core`` — conversion at the adapter boundary.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from coscientist.agents.prompts.registry import PromptRef

_NONBLANK = r".*\S.*"

ReviewDecision = Literal["pass", "fail", "unassessed"]


class ReviewerModel(BaseModel):
    """Frozen, strict, no extras — reviewer output must match the schema exactly."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)


class ReviewEvidenceRef(ReviewerModel):
    """Evidence pointer in a ReviewVerdict (DESIGN §5.1 EvidenceRef shape)."""

    artifact_id: str = Field(pattern=_NONBLANK)
    digest: str = Field(pattern=_NONBLANK)
    json_pointer: str | None = None  # audit-ok[field_default]: EvidenceRef.json_pointer optional


class ReviewVerdict(ReviewerModel):
    """Reviewer role output (DESIGN §5.1 / §7.1).

    ``decision=pass`` requires a non-empty ``evidence`` tuple (code validates).
    ``fail`` / ``unassessed`` may carry empty evidence; ``reasons`` always required
    (may be empty only for ``pass`` when evidence alone is enough — refuse blank
    reason strings; empty tuple allowed for pass).
    """

    decision: ReviewDecision
    reasons: tuple[str, ...]
    evidence: tuple[ReviewEvidenceRef, ...]
    reviewer: str = Field(pattern=_NONBLANK)
    model: str = Field(pattern=_NONBLANK)

    @model_validator(mode="after")
    def _pass_needs_evidence_and_reasons_shape(self) -> ReviewVerdict:
        for i, reason in enumerate(self.reasons):
            if not isinstance(reason, str) or not reason.strip():
                raise ValueError(
                    f"ReviewVerdict.reasons[{i}] must be a non-blank string"
                )
        if self.decision == "pass" and len(self.evidence) == 0:
            raise ValueError("ReviewVerdict.decision='pass' requires non-empty evidence")
        return self


REVIEWER_PROMPT_REF = PromptRef(prompt_id="role.reviewer", version="1")
