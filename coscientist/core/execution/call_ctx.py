"""``CallCtx`` — invocation context for tool calls (DESIGN §6.1).

Lives in ``core.execution`` (not ``tools.contract``) so ``agents`` may import it
while the import-linter contract keeps ``tools.contract`` (Tool / star) forbidden
for agents. ``tools.contract`` imports this type for the ``Tool`` Protocol.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from coscientist.core.execution.domain import HitlPolicy


@dataclass
class CallCtx:
    """Invocation context for ``Tool.call`` / ``ToolRouter.call`` / ``invoke_tool``."""

    study_id: str
    task_key: str
    attempt_id: str
    hitl_policy: HitlPolicy
    # Filled by invoke_tool from input artifacts (caller value is ignored).
    # field(default=…) — not AnnAssign Constant — so audit field_default stays clean.
    tainted_context: bool = field(default=False)
    input_artifact_ids: list[str] = field(default_factory=list)
    input_bindings: list[Any] = field(default_factory=list)
    call_seq: int = field(default=1)
    extra: dict[str, Any] = field(default_factory=dict)
    # Set only by invoke_tool for RegisteredToolSpec.is_fetch tools.
    fetch_capability: Any | None = field(default=None)
