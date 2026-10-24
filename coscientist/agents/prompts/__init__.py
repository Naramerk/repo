"""Versioned prompt templates — no inline prompt strings at call sites."""

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
)

__all__ = [
    "PromptConfigError",
    "PromptError",
    "PromptNotFoundError",
    "PromptRef",
    "PromptRegistry",
    "PromptRenderError",
    "PromptTemplate",
    "render_template",
]
