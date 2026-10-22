"""Minimal study report surfaces (experimental / pre-M5)."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any


def _event_type(e: Any) -> str | None:
    if isinstance(e, dict):
        t = e.get("type")
        return str(t) if t is not None else None
    t = getattr(e, "type", None)
    return str(t) if t is not None else None


def _event_payload(e: Any) -> dict[str, Any]:
    if isinstance(e, dict):
        p = e.get("payload")
        return p if isinstance(p, dict) else {}
    p = getattr(e, "payload", None)
    return p if isinstance(p, dict) else {}


def render_hitl_spend(events: Iterable[Any]) -> dict[str, Any]:
    """Aggregate critic/human HITL decisions and spent_rub for report/web."""
    decisions: list[dict[str, Any]] = []
    total = 0.0
    for e in events:
        typ = _event_type(e)
        payload = _event_payload(e)
        if typ == "BudgetDebited":
            spent = float(payload.get("spent_rub") or 0.0)
            total += spent
            decisions.append(
                {
                    "type": typ,
                    "request_id": payload.get("request_id"),
                    "decided_by": payload.get("decided_by"),
                    "delegated_by": payload.get("delegated_by"),
                    "reason": payload.get("reason"),
                    "spent_rub": spent,
                }
            )
            continue
        if typ not in ("HitlDecided", "HitlTimedOut"):
            continue
        spent = float(payload.get("spent_rub") or 0.0)
        decisions.append(
            {
                "type": typ,
                "request_id": payload.get("request_id"),
                "decided_by": payload.get("decided_by") or payload.get("approved_by"),
                "delegated_by": payload.get("delegated_by"),
                "reason": payload.get("reason"),
                "spent_rub": spent,
                "human_message": payload.get("human_message"),
            }
        )
    if total == 0.0:
        total = sum(d["spent_rub"] for d in decisions if d["type"] == "BudgetDebited")
        if total == 0.0:
            total = sum(d["spent_rub"] for d in decisions)
    return {
        "decisions": decisions,
        "total_spent_rub": total,
        "by_critic": [
            d
            for d in decisions
            if d.get("decided_by") == "critic" or d["type"] == "BudgetDebited"
        ],
    }


def render(events: Iterable[Any]) -> dict[str, Any]:
    """M0 stub of the M5 report: HITL spend is always visible."""
    spend = render_hitl_spend(events)
    return {
        "hitl_spend": spend,
        "summary": (
            f"critic spent {spend['total_spent_rub']} RUB "
            f"across {len([d for d in spend['by_critic'] if d['spent_rub']])} delegated gate(s)"
            if any(d["spent_rub"] for d in spend["by_critic"])
            else "no critic HITL spend"
        ),
    }
