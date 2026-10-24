"""WS /studies/{id}/stream helper (DESIGN §9). Polls EventStore in seq order."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from coscientist.api.errors import StreamConfigRefuse
from coscientist.api.views import list_study_events, require_after_seq
from coscientist.core.events.ports import EventStore


class _WebSocketLike(Protocol):
    async def accept(self) -> None: ...

    async def send_json(self, data: Any) -> None: ...


def require_poll_interval_s(value: object) -> float:
    """Positive finite poll interval (bool refused; no silent default)."""
    if isinstance(value, bool) or type(value) not in (int, float):
        raise StreamConfigRefuse(
            f"poll_interval_s_must_be_number_got_{type(value).__name__}:{value!r}"
        )
    assert isinstance(value, (int, float))
    interval = float(value)
    if interval <= 0.0 or interval != interval:
        raise StreamConfigRefuse(f"poll_interval_s_must_be_positive_got_{value!r}")
    return interval


async def stream_study_events(
    websocket: _WebSocketLike,
    store: EventStore,
    *,
    study_id: object,
    after: object,
    poll_interval_s: object,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    is_disconnected: Callable[[], bool] | None = None,
) -> None:
    """Accept WS, emit EventWire JSON in seq order, poll until disconnect."""
    after_seq = require_after_seq(after)
    interval = require_poll_interval_s(poll_interval_s)
    await websocket.accept()
    while True:
        if is_disconnected is not None and is_disconnected():
            return
        for wire in await list_study_events(
            store, study_id=study_id, after=after_seq
        ):
            await websocket.send_json(wire.model_dump(mode="json"))
            after_seq = wire.seq
        await sleep(interval)
