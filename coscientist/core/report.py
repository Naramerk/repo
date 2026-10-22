"""Minimal study report surfaces for M0 (HITL spend); full report is M5."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any


def render_hitl_spend(events: Iterable[Any]) -> dict[str, Any]:
    """Aggregate critic/human HITL decisions and spent_rub for report/web."""
    decisions: list[dict[str, Any]] = []
    total = 0.0
    for e in events:
        typ = getattr(e, "type", None) or (e.get("type") if isinstance(e, dict) else None)
        if typ not in ("HitlDecided", "HitlTimedOut"):
            continue
        payload = getattr(e, "payload", None) or (
            e.get("payload") if isinstance(e, dict) else {}
        )
        spent = float(payload.get("spent_rub") or 0.0)
        total += spent
        decisions.append(
            {
                "type": typ,
                "request_id": payload.get("request_id"),
                "decided_by": payload.get("decided_by") or payload.get("approved_by"),
                "delegated_by": payload.get("delegated_by"),
                "reason": payload.get("reason"),
                "spent_rub": spent,
            }
        )
    return {
        "decisions": decisions,
        "total_spent_rub": total,
        "by_critic": [d for d in decisions if d["decided_by"] == "critic"],
    }


def render(events: Iterable[Any]) -> dict[str, Any]:
    """M0 stub of the M5 report: HITL spend is always visible."""
    spend = render_hitl_spend(events)
    return {
        "hitl_spend": spend,
        "summary": (
            f"critic spent {spend['total_spent_rub']} RUB "
            f"across {len(spend['by_critic'])} delegated gate(s)"
            if spend["by_critic"]
            else "no critic HITL spend"
        ),
    }
