"""Critic role typed output (DESIGN §7.1; CAPABILITIES #5).

``critic`` returns a schema-valid ``CriticVerdict`` via ``AgentPort.run``
(after G1 / adapter). This module is types + prompt ref only — **no Protocol**.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from coscientist.agents.prompts.registry import PromptRef
from coscientist.agents.strict import NONBLANK, AgentModel

CriticDecision = Literal["accept", "revise", "reject"]


class CriticIssue(AgentModel):
    """One critic finding; ``path`` is a plan/task locator (e.g. task_key)."""

    path: str = Field(pattern=NONBLANK)
    message: str = Field(pattern=NONBLANK)


class CriticVerdict(AgentModel):
    """Critic role output (DESIGN §7.1): accept | revise | reject + issues.

    ``revise`` / ``reject`` require a non-empty ``issues`` tuple (fail-closed).
    ``accept`` may carry empty ``issues``.
    """

    decision: CriticDecision
    issues: tuple[CriticIssue, ...]

    @model_validator(mode="after")
    def _issues_required_unless_accept(self) -> CriticVerdict:
        if self.decision != "accept" and len(self.issues) == 0:
            raise ValueError(
                f"CriticVerdict.decision={self.decision!r} requires non-empty issues"
            )
        return self


CRITIC_PROMPT_REF = PromptRef(prompt_id="role.critic", version="1")
