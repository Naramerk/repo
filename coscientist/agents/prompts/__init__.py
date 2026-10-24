"""Versioned prompt templates — no inline prompt strings at call sites.

Built-in role prompts live in ``coscientist.agents.prompts.builtin`` (import
that module directly — not re-exported here, to avoid circular imports with
role type modules that need ``PromptRef``).
"""

from __future__ import annotations

from coscientist.agents.prompts.registry import (
    PromptConfigError,
    PromptError,
    PromptNotFoundError,
    PromptRef,
    PromptRegistry,
    PromptRenderError,
    PromptTemplate,
    render_template,
    render_untrusted,
)
from coscientist.agents.prompts.roles import (
    ROLE_EXECUTOR_REF,
    ROLE_PLANNER_REF,
    role_prompt_registry,
)

__all__ = [
    "PromptConfigError",
    "PromptError",
    "PromptNotFoundError",
    "PromptRef",
    "PromptRegistry",
    "PromptRenderError",
    "PromptTemplate",
    "ROLE_EXECUTOR_REF",
    "ROLE_PLANNER_REF",
    "render_template",
    "render_untrusted",
    "role_prompt_registry",
]
