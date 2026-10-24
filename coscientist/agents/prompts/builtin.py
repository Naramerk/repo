"""Built-in role prompt templates (CAPABILITIES #27; DESIGN §7.1).

Exact ``(prompt_id, version)`` entries for critic and reviewer. Call sites must
use ``PromptRegistry`` + ``PromptRef`` — no inline prompt strings.

Template bodies live here (not in role type modules) so ``critic`` / ``reviewer``
can import ``PromptRef`` without a circular import through this package.
"""

from __future__ import annotations

from coscientist.agents.critic import CRITIC_PROMPT_REF
from coscientist.agents.prompts.registry import PromptRef, PromptRegistry, PromptTemplate
from coscientist.agents.reviewer import REVIEWER_PROMPT_REF

CRITIC_PROMPT_TEMPLATE = PromptTemplate(
    prompt_id="role.critic",
    version="1",
    body=(
        "You are the plan critic for CoScientist.\n"
        "Study request:\n{{request}}\n\n"
        "Plan JSON:\n{{plan_json}}\n\n"
        "Return only a CriticVerdict JSON object with fields "
        "decision (accept|revise|reject) and issues "
        "(list of {path, message}). "
        "For revise or reject, issues must be non-empty.\n"
    ),
    required_variables=("request", "plan_json"),
)

REVIEWER_PROMPT_TEMPLATE = PromptTemplate(
    prompt_id="role.reviewer",
    version="1",
    body=(
        "You are the quality reviewer for CoScientist.\n"
        "Review question:\n{{question}}\n\n"
        "Evidence refs JSON:\n{{evidence_json}}\n\n"
        "Return only a ReviewVerdict JSON object with fields "
        "decision (pass|fail|unassessed), reasons, evidence "
        "(list of {artifact_id, digest, json_pointer|null}), "
        "reviewer, and model. "
        "For pass, evidence must be non-empty.\n"
    ),
    required_variables=("question", "evidence_json"),
)

_BUILTIN: tuple[PromptTemplate, ...] = (
    CRITIC_PROMPT_TEMPLATE,
    REVIEWER_PROMPT_TEMPLATE,
)


def builtin_role_prompt_templates() -> tuple[PromptTemplate, ...]:
    """Frozen snapshot of shipped role prompts (critic, reviewer)."""
    return _BUILTIN


def builtin_role_prompt_refs() -> frozenset[PromptRef]:
    return frozenset({CRITIC_PROMPT_REF, REVIEWER_PROMPT_REF})


def builtin_role_prompt_registry() -> PromptRegistry:
    """In-memory registry containing only the built-in role prompts."""
    return PromptRegistry(
        {(t.prompt_id, t.version): t for t in builtin_role_prompt_templates()}
    )
