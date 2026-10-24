"""WS /studies/{id}/stream helper (DESIGN §9). Polls EventStore in seq order."""

from __future__ import annotations

import math
from collections.abc import Awaitable, Callable
from typing import Protocol

from coscientist.api.errors import StreamConfigRefuse
from coscientist.api.views import list_study_events
from coscientist.core.events.ports import EventStore


class _WebSocketLike(Protocol):
    async def send_json(self, data: object) -> None: ...


def require_poll_interval_s(value: float) -> float:
    """Positive finite ``float`` poll interval (``type is float``; bool/int refused)."""
    if type(value) is not float:
        kind = type(value).__name__
        msg = f"poll_interval_s_must_be_float_got_{kind}:{value!r}"
        raise StreamConfigRefuse(msg)
    if not math.isfinite(value) or value <= 0.0:
        msg = f"poll_interval_s_must_be_positive_finite_got_{value!r}"
        raise StreamConfigRefuse(msg)
    return value


async def stream_study_events(
    websocket: _WebSocketLike,
    store: EventStore,
    *,
    study_id: str,
    after: int,
    poll_interval_s: float,
    sleep: Callable[[float], Awaitable[None]],
    is_disconnected: Callable[[], bool],
) -> None:
    """Emit EventWire JSON in seq order until ``is_disconnected`` is true.

    Caller must ``accept`` the WebSocket and pass already-validated ``after`` /
    ``poll_interval_s``.
    """
    after_seq = after
    while True:
        if is_disconnected():
            return
        for wire in await list_study_events(store, study_id=study_id, after=after_seq):
            await websocket.send_json(wire.model_dump(mode="json"))
            after_seq = wire.seq
        await sleep(poll_interval_s)
