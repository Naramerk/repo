"""HITL ``side_effect`` gate for write / external_effect tools (DESIGN_M1 §8)."""

from __future__ import annotations

from typing import Literal

from coscientist.core.execution.domain import HitlPolicy

SideEffectDecision = Literal["deny", "allow", "ask"]

_ALLOWED: dict[str, SideEffectDecision] = {
    "deny": "deny",
    "allow": "allow",
    "ask": "ask",
    "DENY": "deny",
    "ALLOW": "allow",
    "ASK": "ask",
}


def side_effect_decision(policy: HitlPolicy) -> SideEffectDecision:
    """Resolve policy default for the ``side_effect`` gate.

    Headless/auto cannot ask a human: ``ask`` collapses to ``deny`` (safe default).
    Interactive ``ask`` is returned so callers may open HITL; M1 invoke path treats
    anything other than ``allow`` as deny → ToolError(local).
    """
    gate = policy.gate("side_effect")
    if gate is None:
        return "deny"
    if not gate.enabled:
        return "deny"
    if "side_effect" not in policy.defaults:
        return "deny"
    raw = policy.defaults["side_effect"]
    if not isinstance(raw, str):
        return "deny"
    if raw not in _ALLOWED:
        return "deny"
    decision = _ALLOWED[raw]
    if decision == "ask" and policy.mode in ("headless", "auto"):
        return "deny"
    return decision


def side_effect_allowed(policy: HitlPolicy) -> bool:
    """True only when the side_effect gate explicitly allows the call."""
    return side_effect_decision(policy) == "allow"
