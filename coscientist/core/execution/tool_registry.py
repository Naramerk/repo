"""Closed tool registry: authoritative effect class (DESIGN_M1 §8 / PR18 review).

``invoke_tool`` is the only allowed call path for side-effecting tools; the
agent wiring lands later. Effect is taken from this registry — never from a
caller-supplied ``ToolSpec.effect``. Unknown / unregistered names resolve to
``external_effect`` (fail-closed).

Only ``coscientist.core.execution.tool_loader`` may call ``register_tool`` (loader gate).
After ``freeze_registry()``, ``_REGISTRY`` is a ``MappingProxyType`` and further
registration is forbidden.
"""

from __future__ import annotations

from contextlib import contextmanager
from types import MappingProxyType
from typing import Iterator, Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field

ToolEffect = Literal["read", "write", "external_effect"]

DEFAULT_UNREGISTERED_EFFECT: ToolEffect = "external_effect"

_REGISTRY_MUTABLE: dict[str, RegisteredToolSpec] = {}
_REGISTRY: Mapping[str, RegisteredToolSpec] = _REGISTRY_MUTABLE
_FROZEN: bool = False
_LOADER_GATE: bool = False


class RegisteredToolSpec(BaseModel):
    """Core-owned catalog row (subset of tools.ToolSpec used for gating)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    effect: ToolEffect
    # Explicit fetch tools may put_fetched + SourceFetched via invoke_tool.
    # Plain ``effect=read`` is not enough (spoof/read ≠ literature fetch).
    is_fetch: bool = False
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


class RegistryUnauthorizedError(RuntimeError):
    """Raised when ``register_tool`` is called outside the loader gate."""


@contextmanager
def _loader_registration_gate() -> Iterator[None]:
    """Open the gate so only ``tool_loader`` can register built-ins."""
    global _LOADER_GATE
    _LOADER_GATE = True
    try:
        yield
    finally:
        _LOADER_GATE = False


def freeze_registry() -> None:
    """Seal the registry — further ``register_tool`` calls raise; expose read-only map."""
    global _FROZEN, _REGISTRY
    _FROZEN = True
    _REGISTRY = MappingProxyType(_REGISTRY_MUTABLE)


def is_registry_frozen() -> bool:
    return _FROZEN


def _put(spec: RegisteredToolSpec) -> RegisteredToolSpec:
    if _FROZEN:
        raise RegistryFrozenError(
            f"tool registry is frozen; cannot register {spec.name!r}"
        )
    if spec.name in _REGISTRY_MUTABLE:
        raise RegistryDuplicateError(
            f"tool already registered: {spec.name!r} (replacement forbidden)"
        )
    _REGISTRY_MUTABLE[spec.name] = spec
    return spec


def register_tool(spec: RegisteredToolSpec) -> RegisteredToolSpec:
    """Register a tool. Production: only via ``tool_loader`` while the gate is open."""
    if _FROZEN:
        raise RegistryFrozenError(
            f"tool registry is frozen; cannot register {spec.name!r}"
        )
    if not _LOADER_GATE:
        raise RegistryUnauthorizedError(
            "register_tool is only allowed from coscientist.core.execution.tool_loader"
        )
    return _put(spec)


def _register_tool_for_tests(spec: RegisteredToolSpec) -> RegisteredToolSpec:
    """Test-only registration (AST-banned outside ``tests/``)."""
    return _put(spec)


def get_registered(name: str) -> RegisteredToolSpec | None:
    return _REGISTRY.get(name)


def resolve_effect(name: str) -> ToolEffect:
    """Authoritative effect for ``name``. Unregistered → ``external_effect``."""
    spec = _REGISTRY.get(name)
    if spec is None:
        return DEFAULT_UNREGISTERED_EFFECT
    return spec.effect


def resolve_is_fetch(name: str) -> bool:
    """True only for registered tools with ``is_fetch=True``."""
    spec = _REGISTRY.get(name)
    return bool(spec is not None and spec.is_fetch)


def resolve_spec(name: str) -> RegisteredToolSpec:
    """Return registered spec, or a synthetic external_effect stub for unknowns."""
    spec = _REGISTRY.get(name)
    if spec is not None:
        return spec
    return RegisteredToolSpec(name=name, effect=DEFAULT_UNREGISTERED_EFFECT)


def _reset_registry_for_tests() -> None:
    """Test-only: unfreeze and clear. AST-banned outside ``tests/``."""
    global _FROZEN, _REGISTRY, _REGISTRY_MUTABLE, _LOADER_GATE
    _FROZEN = False
    _LOADER_GATE = False
    _REGISTRY_MUTABLE = {}
    _REGISTRY = _REGISTRY_MUTABLE
