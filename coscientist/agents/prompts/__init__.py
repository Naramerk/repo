"""Versioned prompt templates — no inline prompt strings at call sites."""

from __future__ import annotations

from typing import Any

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

__all__ = [
    "PromptConfigError",
    "PromptError",
    "PromptNotFoundError",
    "PromptRef",
    "PromptRegistry",
    "PromptRenderError",
    "PromptTemplate",
    "builtin_role_prompt_refs",
    "builtin_role_prompt_registry",
    "builtin_role_prompt_templates",
    "render_template",
    "render_untrusted",
]


def __getattr__(name: str) -> Any:
    """Lazy builtins — avoid circular import with role modules that use PromptRef."""
    if name in {
        "builtin_role_prompt_refs",
        "builtin_role_prompt_registry",
        "builtin_role_prompt_templates",
    }:
        from coscientist.agents.prompts import builtin as _builtin

        return getattr(_builtin, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
