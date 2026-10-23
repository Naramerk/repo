"""Closed tool registry: authoritative effect class (DESIGN_M1 §8 / PR18 review).

``invoke_tool`` is the only allowed call path for side-effecting tools; the
agent wiring lands later. Effect is taken from this registry — never from a
caller-supplied ``ToolSpec.effect``. Unknown / unregistered names resolve to
``external_effect`` (fail-closed).
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

ToolEffect = Literal["read", "write", "external_effect"]

# Fail-closed default for anything not explicitly registered.
DEFAULT_UNREGISTERED_EFFECT: ToolEffect = "external_effect"


class RegisteredToolSpec(BaseModel):
    """Core-owned catalog row (subset of tools.ToolSpec used for gating)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    effect: ToolEffect
    idempotent: bool = False
    supports_idem_key: bool = False
    timeout_s: float = 120
    max_retries: int = 2
    provider: Literal["mcp", "local", "coder", "alembic"] = "local"
    input_schema: dict = Field(default_factory=dict)
    output_schema: dict | None = None


_REGISTRY: dict[str, RegisteredToolSpec] = {}


def clear_registry() -> None:
    """Test helper — empty the closed registry."""
    _REGISTRY.clear()


def register_tool(spec: RegisteredToolSpec) -> RegisteredToolSpec:
    """Register or replace a tool. Production catalogs call this at boot."""
    _REGISTRY[spec.name] = spec
    return spec


def get_registered(name: str) -> RegisteredToolSpec | None:
    return _REGISTRY.get(name)


def resolve_effect(name: str) -> ToolEffect:
    """Authoritative effect for ``name``. Unregistered → ``external_effect``."""
    spec = _REGISTRY.get(name)
    if spec is None:
        return DEFAULT_UNREGISTERED_EFFECT
    return spec.effect


def resolve_spec(name: str) -> RegisteredToolSpec:
    """Return registered spec, or a synthetic external_effect stub for unknowns."""
    spec = _REGISTRY.get(name)
    if spec is not None:
        return spec
    return RegisteredToolSpec(name=name, effect=DEFAULT_UNREGISTERED_EFFECT)
