"""Detect open HITL gates in a journal (core must refuse them)."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from coscientist.core.errors import HitlUnsupported


def supports_hitl(runtime_or_cls: Any) -> bool:
    """True if this runtime (or class) implements HITL gate APIs."""
    return callable(getattr(runtime_or_cls, "open_hitl", None))


def open_hitl_request_ids(events: Iterable[Any]) -> list[str]:
    """Return request_ids that are still open after folding Hitl* events."""
    open_ids: dict[str, bool] = {}
    for ev in events:
        typ = getattr(ev, "type", None) if not isinstance(ev, dict) else ev.get("type")
        payload = (
            getattr(ev, "payload", None) if not isinstance(ev, dict) else ev.get("payload")
        )
        if not isinstance(payload, dict):
            continue
        rid = payload.get("request_id")
        if typ == "HitlOpened" and rid:
            open_ids[str(rid)] = True
        elif typ in ("HitlDecided", "HitlTimedOut") and rid:
            # WAIT / keep_open leaves the gate open for another human answer.
            if payload.get("keep_open") or payload.get("effect") == "WAIT":
                open_ids[str(rid)] = True
            else:
                open_ids[str(rid)] = False
    return [rid for rid, is_open in open_ids.items() if is_open]


def reject_open_hitl_if_unsupported(runtime_or_cls: Any, events: Iterable[Any]) -> None:
    """Raise HitlUnsupported when core meets a journal with open HITL gates."""
    if supports_hitl(runtime_or_cls):
        return
    open_ids = open_hitl_request_ids(events)
    if open_ids:
        raise HitlUnsupported(open_ids)
