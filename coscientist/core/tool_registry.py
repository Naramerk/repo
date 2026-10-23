"""Closed tool registry: authoritative effect class (DESIGN_M1 §8 / PR18 review).

``invoke_tool`` is the only allowed call path for side-effecting tools; the
agent wiring lands later. Effect is taken from this registry — never from a
caller-supplied ``ToolSpec.effect``. Unknown / unregistered names resolve to
``external_effect`` (fail-closed).

After ``freeze_registry()``, registration is forbidden (no silent replace).
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

ToolEffect = Literal["read", "write", "external_effect"]

DEFAULT_UNREGISTERED_EFFECT: ToolEffect = "external_effect"

_REGISTRY: dict[str, RegisteredToolSpec] = {}
_FROZEN: bool = False


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


class RegistryFrozenError(RuntimeError):
    """Raised when mutating a frozen tool registry."""


class RegistryDuplicateError(ValueError):
    """Raised when registering a name that already exists (no silent replace)."""


def freeze_registry() -> None:
    """Seal the registry — further ``register_tool`` calls raise."""
    global _FROZEN
    _FROZEN = True


def is_registry_frozen() -> bool:
    return _FROZEN


def register_tool(spec: RegisteredToolSpec) -> RegisteredToolSpec:
    """Register a tool. Fails if frozen or if ``name`` is already registered."""
    if _FROZEN:
        raise RegistryFrozenError(
            f"tool registry is frozen; cannot register {spec.name!r}"
        )
    if spec.name in _REGISTRY:
        raise RegistryDuplicateError(
            f"tool already registered: {spec.name!r} (replacement forbidden)"
        )
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


def _reset_registry_for_tests() -> None:
    """Test-only: unfreeze and clear. Not part of the public production API."""
    global _FROZEN
    _FROZEN = False
    _REGISTRY.clear()
