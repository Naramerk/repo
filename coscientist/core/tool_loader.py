"""Register built-in tools then seal the closed registry (DESIGN_M1 §8).

``open_runtime`` calls ``ensure_core_tools_ready``. Registration is only allowed
while the loader gate is open; ``freeze_registry`` runs at the end of this
function so tools registered before the first open cannot survive into the
frozen map (they never could — ``register_tool`` rejects non-loader callers).
"""

from __future__ import annotations

from coscientist.core.tool_registry import (
    RegisteredToolSpec,
    _loader_registration_gate,
    freeze_registry,
    get_registered,
    is_registry_frozen,
    register_tool,
)

# Production built-ins. Empty until product tools ship; freeze still runs.
CORE_BUILTIN_TOOLS: tuple[RegisteredToolSpec, ...] = ()


def ensure_core_tools_ready() -> None:
    """Register any missing built-ins under the loader gate, then freeze."""
    if is_registry_frozen():
        return
    with _loader_registration_gate():
        for spec in CORE_BUILTIN_TOOLS:
            if get_registered(spec.name) is None:
                register_tool(spec)
        freeze_registry()
