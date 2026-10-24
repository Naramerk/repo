"""Versioned prompt templates (DESIGN §7 agent layer; §11 ``agents/prompts``)."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

_VAR_RE = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")


class PromptError(Exception):
    """Base prompt registry failure."""


class PromptConfigError(PromptError):
    """Invalid config or unusable template."""


class PromptNotFoundError(PromptError):
    """Exact (prompt_id, version) missing."""


class PromptRenderError(PromptError):
    """Missing or undeclared render variables."""


def _wrap_validation(exc: ValidationError) -> PromptConfigError:
    return PromptConfigError(str(exc))


class _PromptModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    def __init__(self, **data: object) -> None:
        try:
            super().__init__(**data)
        except ValidationError as e:
            raise _wrap_validation(e) from e

    @classmethod
    def model_validate(cls, obj: Any, **kwargs: Any) -> Self:
        try:
            return super().model_validate(obj, **kwargs)
        except ValidationError as e:
            raise _wrap_validation(e) from e

    @classmethod
    def model_construct(cls, _fields_set: set[str] | None = None, **values: Any) -> Self:
        if _fields_set is not None:
            raise PromptConfigError("model_construct does not support _fields_set")
        return cls.model_validate(values)

    def model_copy(self, *, update: Mapping[str, Any] | None = None, deep: bool = False) -> Self:
        if deep:
            raise PromptConfigError("model_copy(deep=True) is refused")
        data = self.model_dump()
        if update:
            data.update(dict(update))
        return type(self).model_validate(data)


class PromptRef(_PromptModel):
    prompt_id: str = Field(min_length=1)
    version: str = Field(min_length=1)


class PromptTemplate(_PromptModel):
    prompt_id: str = Field(min_length=1)
    version: str = Field(min_length=1)
    body: str
    required_variables: tuple[str, ...]

    @model_validator(mode="after")
    def _validate_template(self) -> Self:
        req = self.required_variables
        label = f"prompt {self.prompt_id!r} v{self.version}"
        if not all(v for v in req):
            raise PromptConfigError(
                f"{label}: required_variables must be a tuple of non-empty strings",
            )
        if len(req) != len(set(req)):
            raise PromptConfigError(
                f"{label}: required_variables must not contain duplicates",
            )
        if not self.body.strip():
            raise PromptConfigError(f"{label}: template body is empty")
        required_set = set(req)
        body_vars = set(_VAR_RE.findall(self.body))
        if missing := sorted(required_set - body_vars):
            raise PromptConfigError(f"{label}: body missing placeholders {missing}")
        if extra := sorted(body_vars - required_set):
            raise PromptConfigError(
                f"{label}: body has undeclared placeholders {extra}",
            )
        return self


class PromptRegistry:
    def __init__(
        self,
        templates: Mapping[tuple[str, str], PromptTemplate],
    ) -> None:
        if not templates:
            raise PromptConfigError("prompt registry has no templates")
        out: dict[tuple[str, str], PromptTemplate] = {}
        for key, tmpl in templates.items():
            expected = (tmpl.prompt_id, tmpl.version)
            if key != expected:
                raise PromptConfigError(
                    f"registry key {key!r} does not match template {expected!r}",
                )
            out[key] = tmpl
        self._templates = out

    @classmethod
    def from_untrusted(
        cls,
        templates: Mapping[tuple[str, str], object],
    ) -> PromptRegistry:
        typed: dict[tuple[str, str], PromptTemplate] = {}
        for key, tmpl in templates.items():
            if not isinstance(tmpl, PromptTemplate):
                got = type(tmpl).__name__
                raise PromptConfigError(
                    f"registry value for {key!r} must be PromptTemplate, got {got}",
                )
            typed[key] = tmpl
        return cls(typed)

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
        return frozenset(PromptRef(prompt_id=t.prompt_id, version=t.version) for t in self._templates.values())


def render_template(tmpl: PromptTemplate, variables: Mapping[str, str]) -> str:
    label = f"prompt {tmpl.prompt_id!r} v{tmpl.version}"
    required = set(tmpl.required_variables)
    provided = set(variables.keys())
    if missing := sorted(required - provided):
        raise PromptRenderError(f"{label}: missing variables {missing}")
    if undeclared := sorted(provided - required):
        raise PromptRenderError(f"{label}: undeclared variables {undeclared}")
    return _VAR_RE.sub(lambda m: variables[m.group(1)], tmpl.body)


def render_untrusted(tmpl: PromptTemplate, variables: Mapping[str, object]) -> str:
    label = f"prompt {tmpl.prompt_id!r} v{tmpl.version}"
    resolved: dict[str, str] = {}
    for name, value in variables.items():
        if not isinstance(value, str):
            got = type(value).__name__
            raise PromptRenderError(
                f"{label}: variable {name!r} must be str, got {got}",
            )
        resolved[name] = value
    return render_template(tmpl, resolved)
