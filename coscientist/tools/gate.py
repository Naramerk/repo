"""HITL ``side_effect`` gate for write / external_effect tools (DESIGN_M1 §8)."""

from __future__ import annotations

from typing import Literal

from coscientist.core.execution.domain import HitlPolicy

SideEffectDecision = Literal["deny", "allow", "ask"]
_ALLOWED: frozenset[str] = frozenset({"deny", "allow", "ask"})


def side_effect_decision(policy: HitlPolicy) -> SideEffectDecision:
    """Resolve policy default for the ``side_effect`` gate.

    Headless/auto cannot ask a human: ``ask`` collapses to ``deny`` (safe default).
    Interactive ``ask`` is returned so callers may open HITL; M1 invoke path treats
    anything other than ``allow`` as deny → ToolError(local).

    ``HitlPolicy.defaults["side_effect"]`` must be an exact lowercase member of
    ``deny|allow|ask`` (no case-fold inventing).
    """
    gate = policy.gate("side_effect")
    if gate is None or not gate.enabled:
        return "deny"
    raw = policy.defaults["side_effect"]
    if not isinstance(raw, str):
        raise TypeError(f"side_effect default must be str, got {type(raw).__name__}")
    if raw not in _ALLOWED:
        raise ValueError(f"side_effect default invalid: {raw!r}")
    if raw == "ask" and policy.mode in ("headless", "auto"):
        return "deny"
    if raw == "deny":
        return "deny"
    if raw == "allow":
        return "allow"
    return "ask"


def side_effect_allowed(policy: HitlPolicy) -> bool:
    """True only when the side_effect gate explicitly allows the call."""
    return side_effect_decision(policy) == "allow"
