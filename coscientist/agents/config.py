"""Agent-layer configuration. Every operational field is required (no guessed defaults)."""

from __future__ import annotations

import math
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class RoleConfig(BaseModel):
    """Per-role model and prompt reference. Values come from the profile only."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    model: str = Field(min_length=1)
    system_prompt_ref: str = Field(min_length=1)
    max_turns: int = Field(ge=1)
    allowed_tool_names: tuple[str, ...]

    @field_validator("model", "system_prompt_ref")
    @classmethod
    def _stripped_nonempty(cls, v: str) -> str:
        if v != v.strip() or v == "":
            raise ValueError("must be a non-empty stripped string")
        return v

    @field_validator("allowed_tool_names")
    @classmethod
    def _tool_names(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        for name in v:
            if not isinstance(name, str) or name != name.strip() or name == "":
                raise ValueError(
                    "allowed_tool_names entries must be non-empty stripped strings"
                )
        if len(v) != len(set(v)):
            raise ValueError("allowed_tool_names must not contain duplicates")
        return v


class AgentBudgetLimits(BaseModel):
    """Attempt/role ceilings. All numeric ceilings required; no domain.Budget defaults."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    max_llm_calls: int = Field(ge=0)
    max_tokens: int = Field(ge=0)
    max_tool_calls: int = Field(ge=0)
    max_cost_rub: Decimal | None

    @field_validator("max_cost_rub")
    @classmethod
    def _cost_non_negative(cls, v: Decimal | None) -> Decimal | None:
        if v is None:
            return None
        if v < 0:
            raise ValueError("max_cost_rub must be >= 0 when set")
        return v


class AgentConfig(BaseModel):
    """Wiring for JournaledAgentPort. Roles and prompts are config-only."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    roles: dict[str, RoleConfig]
    prompts: dict[str, str]
    # Per-token RUB prices from the profile (not per-1k — avoids a divisor literal).
    cost_per_prompt_token_rub: Decimal | None
    cost_per_completion_token_rub: Decimal | None

    @field_validator("roles")
    @classmethod
    def _roles_nonempty(cls, v: dict[str, RoleConfig]) -> dict[str, RoleConfig]:
        if not v:
            raise ValueError("roles must be non-empty")
        for key in v:
            if key != key.strip() or key == "":
                raise ValueError("role keys must be non-empty stripped strings")
        return v

    @field_validator("prompts")
    @classmethod
    def _prompts(cls, v: dict[str, str]) -> dict[str, str]:
        for key, text in v.items():
            if key != key.strip() or key == "":
                raise ValueError("prompt keys must be non-empty stripped strings")
            if not isinstance(text, str) or text == "":
                raise ValueError(f"prompt {key!r} must be a non-empty string")
        return v

    @model_validator(mode="after")
    def _cross(self) -> AgentConfig:
        for role_name, role in self.roles.items():
            if role.system_prompt_ref not in self.prompts:
                raise ValueError(
                    f"role {role_name!r} system_prompt_ref "
                    f"{role.system_prompt_ref!r} is not in prompts"
                )
        both_none = (
            self.cost_per_prompt_token_rub is None
            and self.cost_per_completion_token_rub is None
        )
        both_set = (
            self.cost_per_prompt_token_rub is not None
            and self.cost_per_completion_token_rub is not None
        )
        if not both_none and not both_set:
            raise ValueError(
                "cost_per_prompt_token_rub and cost_per_completion_token_rub "
                "must both be set or both be null"
            )
        if both_set:
            assert self.cost_per_prompt_token_rub is not None
            assert self.cost_per_completion_token_rub is not None
            for label, value in (
                ("cost_per_prompt_token_rub", self.cost_per_prompt_token_rub),
                ("cost_per_completion_token_rub", self.cost_per_completion_token_rub),
            ):
                if value < 0:
                    raise ValueError(f"{label} must be >= 0")
                if not math.isfinite(float(value)):
                    raise ValueError(f"{label} must be finite")
        return self

    def role(self, name: str) -> RoleConfig:
        try:
            return self.roles[name]
        except KeyError as exc:
            raise ValueError(f"unknown role: {name!r}") from exc

    def prompt_text(self, ref: str) -> str:
        try:
            return self.prompts[ref]
        except KeyError as exc:
            raise ValueError(f"unknown prompt ref: {ref!r}") from exc

    def estimate_cost_rub(self, *, prompt_tokens: int, completion_tokens: int) -> Decimal | None:
        if (
            self.cost_per_prompt_token_rub is None
            or self.cost_per_completion_token_rub is None
        ):
            return None
        prompt = Decimal(prompt_tokens) * self.cost_per_prompt_token_rub
        completion = Decimal(completion_tokens) * self.cost_per_completion_token_rub
        return prompt + completion


def role_config_from_mapping(data: dict[str, Any]) -> RoleConfig:
    """Validate a mapping into RoleConfig (profile loaders)."""
    return RoleConfig.model_validate(data)


def agent_config_from_mapping(data: dict[str, Any]) -> AgentConfig:
    """Validate a mapping into AgentConfig (profile loaders)."""
    return AgentConfig.model_validate(data)
