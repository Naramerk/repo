"""Test doubles for SqliteEventStore (keep production store free of delay hooks)."""

from __future__ import annotations

import time
from threading import Event
from typing import Any

from coscientist.core.events import EventEnvelope
from coscientist.core.store import SqliteEventStore


class SlowWriteStore(SqliteEventStore):
    """Injects a sleep inside the write txn (off the event loop via to_thread)."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.write_delay_s: float = 0.0
        self.write_delay_entered: Event | None = None

    def _maybe_delay(self) -> None:
        if self.write_delay_s and self.write_delay_s > 0:
            if self.write_delay_entered is not None:
                self.write_delay_entered.set()
            time.sleep(self.write_delay_s)

    def _append_locked(self, event: EventEnvelope) -> EventEnvelope:
        self._maybe_delay()
        return super()._append_locked(event)

    def _append_many_sync(
        self,
        events: list[EventEnvelope],
        *,
        require_open_attempt: str | None = None,
        source_fetched_cap_id: str | None = None,
    ) -> list[EventEnvelope]:
        self._maybe_delay()
        return super()._append_many_sync(
            events,
            require_open_attempt=require_open_attempt,
            source_fetched_cap_id=source_fetched_cap_id,
        )
