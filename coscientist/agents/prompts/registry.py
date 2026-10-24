"""Versioned prompt templates (DESIGN §7 agent layer; §11 ``agents/prompts``)."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

_VAR_RE = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")


class PromptError(Exception):
    """Base prompt registry failure."""


class PromptConfigError(PromptError):
    """Invalid config or unusable template."""


class PromptNotFoundError(PromptError):
    """Exact (prompt_id, version) missing."""


class PromptRenderError(PromptError):
    """Missing or undeclared render variables."""


class PromptRef(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    prompt_id: str = Field(min_length=1)
    version: str = Field(min_length=1)


def _assert_body_placeholders(
    body: str,
    required: tuple[str, ...],
    *,
    prompt_id: str,
    version: str,
) -> None:
    label = f"prompt {prompt_id!r} v{version}"
    if not body.strip():
        raise PromptConfigError(f"{label}: template body is empty")
    required_set = set(required)
    body_vars = set(_VAR_RE.findall(body))
    if missing := sorted(required_set - body_vars):
        raise PromptConfigError(f"{label}: body missing placeholders {missing}")
    if extra := sorted(body_vars - required_set):
        raise PromptConfigError(
            f"{label}: body has undeclared placeholders {extra}",
        )


def _required_variables_ok(req: object) -> bool:
    if not isinstance(req, tuple):
        return False
    items: tuple[Any, ...] = req
    if not items:
        return True
    return all(isinstance(v, str) and v for v in items)


@dataclass(frozen=True, slots=True)
class PromptTemplate:
    prompt_id: str
    version: str
    body: str
    required_variables: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.prompt_id or not self.version:
            raise PromptConfigError("prompt_id and version must be non-empty")
        req = self.required_variables
        label = f"prompt {self.prompt_id!r} v{self.version}"
        if not _required_variables_ok(req):
            raise PromptConfigError(
                f"{label}: required_variables must be a tuple of non-empty strings",
            )
        if len(req) != len(set(req)):
            raise PromptConfigError(
                f"{label}: required_variables must not contain duplicates",
            )
        _assert_body_placeholders(
            self.body,
            req,
            prompt_id=self.prompt_id,
            version=self.version,
        )


class PromptRegistry:
    def __init__(
        self,
        templates: Mapping[tuple[str, str], PromptTemplate],
    ) -> None:
        if not templates:
            raise PromptConfigError("prompt registry has no templates")
        out: dict[tuple[str, str], PromptTemplate] = {}
        for key, tmpl in templates.items():
            if not isinstance(tmpl, PromptTemplate):
                got = type(tmpl).__name__
                raise PromptConfigError(
                    f"registry value for {key!r} must be PromptTemplate, got {got}",
                )
            expected = (tmpl.prompt_id, tmpl.version)
            if key != expected:
                raise PromptConfigError(
                    f"registry key {key!r} does not match template {expected!r}",
                )
            out[key] = tmpl
        self._templates = out

    def get(self, ref: PromptRef) -> PromptTemplate:
        key = (ref.prompt_id, ref.version)
        if key not in self._templates:
            raise PromptNotFoundError(
                f"prompt {ref.prompt_id!r} version {ref.version!r} not in registry",
            )
        return self._templates[key]

    def render(self, ref: PromptRef, variables: Mapping[str, str]) -> str:
        return render_template(self.get(ref), variables)

    def refs(self) -> frozenset[PromptRef]:
        values = self._templates.values()
        return frozenset(PromptRef(prompt_id=t.prompt_id, version=t.version) for t in values)


def render_template(
    tmpl: PromptTemplate,
    variables: Mapping[str, str],
) -> str:
    label = f"prompt {tmpl.prompt_id!r} v{tmpl.version}"
    required = set(tmpl.required_variables)
    provided = set(variables.keys())
    if missing := sorted(required - provided):
        raise PromptRenderError(f"{label}: missing variables {missing}")
    if undeclared := sorted(provided - required):
        raise PromptRenderError(f"{label}: undeclared variables {undeclared}")
    for name, value in variables.items():
        if not isinstance(value, str):
            got = type(value).__name__
            raise PromptRenderError(
                f"{label}: variable {name!r} must be str, got {got}",
            )
    return _VAR_RE.sub(lambda m: variables[m.group(1)], tmpl.body)
