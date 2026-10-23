"""Register built-in tools then seal the closed registry (DESIGN_M1 §8).

``open_runtime`` calls ``ensure_core_tools_ready`` so production always freezes
after built-ins are registered. Further ``register_tool`` calls raise
``RegistryFrozenError``. Tests may ``_reset_registry_for_tests`` between cases.
"""

from __future__ import annotations

from coscientist.core.tool_registry import (
    RegisteredToolSpec,
    freeze_registry,
    get_registered,
    is_registry_frozen,
    register_tool,
)

# Production built-ins. Empty until product tools ship; freeze still runs.
CORE_BUILTIN_TOOLS: tuple[RegisteredToolSpec, ...] = ()


def ensure_core_tools_ready() -> None:
    """Register any missing built-ins, then freeze the registry (idempotent)."""
    if is_registry_frozen():
        return
    for spec in CORE_BUILTIN_TOOLS:
        if get_registered(spec.name) is None:
            register_tool(spec)
    freeze_registry()
