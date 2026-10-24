"""Reviewer role typed output (DESIGN §5.1 / §7.1).

``reviewer`` returns a schema-valid ``ReviewVerdict`` via ``AgentPort.run``
(after G1 / adapter). This module is types + prompt ref only — **no Protocol**.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, model_validator

from coscientist.agents.prompts.registry import PromptRef
from coscientist.agents.strict import NONBLANK, AgentModel

ReviewDecision = Literal["pass", "fail", "unassessed"]
_NonBlankStr = Annotated[str, Field(pattern=NONBLANK)]


class ReviewEvidenceRef(AgentModel):
    """Evidence pointer in a ReviewVerdict (DESIGN §5.1 EvidenceRef).

    ``json_pointer`` is a required RFC 6901 string; ``""`` means the whole
    artifact (DESIGN §5.1). Adapter → domain ``EvidenceRef``: pass
    ``artifact_id`` / ``digest`` / ``json_pointer`` through as-is; do not invent
    a pointer. Domain still allows ``json_pointer: str | None`` historically —
    map only explicit reviewer ``""`` to whole-artifact, never coerce a missing
    agent-layer key (that is a ValidationError here).
    """

    artifact_id: str = Field(pattern=NONBLANK)
    digest: str = Field(pattern=NONBLANK)
    json_pointer: str


class ReviewVerdict(AgentModel):
    """Reviewer role output (DESIGN §5.1 / §7.1).

    - ``pass``: ``evidence`` must be non-empty; ``reasons`` may be empty.
    - ``fail`` / ``unassessed``: ``evidence`` may be empty; ``reasons`` may be
      empty, but every reason string must be non-blank when present.
    """

    decision: ReviewDecision
    reasons: tuple[_NonBlankStr, ...]
    evidence: tuple[ReviewEvidenceRef, ...]
    reviewer: str = Field(pattern=NONBLANK)
    model: str = Field(pattern=NONBLANK)

    @model_validator(mode="after")
    def _pass_needs_evidence(self) -> ReviewVerdict:
        if self.decision == "pass" and len(self.evidence) == 0:
            raise ValueError("ReviewVerdict.decision='pass' requires non-empty evidence")
        return self


REVIEWER_PROMPT_REF = PromptRef(prompt_id="role.reviewer", version="1")
