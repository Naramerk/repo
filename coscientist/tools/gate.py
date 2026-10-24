"""HITL ``side_effect`` gate for write / external_effect tools (DESIGN_M1 §8)."""

from __future__ import annotations

from typing import Literal

from coscientist.core.execution.domain import HitlPolicy

SideEffectDecision = Literal["deny", "allow", "ask"]


def side_effect_decision(policy: HitlPolicy) -> SideEffectDecision:
    """Resolve policy default for the ``side_effect`` gate.

    Headless/auto cannot ask a human: ``ask`` collapses to ``deny`` (safe default).
    Interactive ``ask`` is returned so callers may open HITL; M1 invoke path treats
    anything other than ``allow`` as deny → ToolError(local).
    """
    gate = policy.gate("side_effect")
    if gate is None or not gate.enabled:
        return "deny"
    raw = policy.defaults["side_effect"]
    if not isinstance(raw, str):
        raise TypeError(f"side_effect default must be str, got {type(raw).__name__}")
    raw = raw.lower()
    if raw not in ("deny", "allow", "ask"):
        raise ValueError(f"side_effect default invalid: {raw!r}")
    decision: SideEffectDecision = raw  # type: ignore[assignment]
    if decision == "ask" and policy.mode in ("headless", "auto"):
        return "deny"
    return decision


def side_effect_allowed(policy: HitlPolicy) -> bool:
    """True only when the side_effect gate explicitly allows the call."""
    return side_effect_decision(policy) == "allow"
