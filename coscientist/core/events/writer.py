"""Single asyncio write queue per study (DESIGN §3.5a)."""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from coscientist.core.events.events import StoredEvent, UnstampedEvent
from coscientist.core.events.store import SqliteEventStore

WriteFn = Callable[[], Awaitable[Any]]

@dataclass
class _Item:
    coro_factory: WriteFn
    future: asyncio.Future[Any]

class StudyWriter:
    """Serializes all study mutations through one queue (maxsize=1000)."""

    def __init__(self, store: SqliteEventStore, *, maxsize: int = 1000) -> None:
        self.store = store
        self.queue: asyncio.Queue[_Item | None] = asyncio.Queue(maxsize=maxsize)
        self._task: asyncio.Task[None] | None = None
        self._depth_high_water = 0
        self._latencies_ms: deque[float] = deque(maxlen=10_000)
        # SourceFetched fold cache (PR14) — study-scoped; typed Any to avoid
        # circular import with source_fetch.SourceFetchIndex.
        self.source_fetch_idx: Any | None = None
        self.source_fetch_seq: int = 0

    @property
    def depth(self) -> int:
        return self.queue.qsize()

    @property
    def depth_high_water(self) -> int:
        return self._depth_high_water

    def latency_samples_ms(self) -> list[float]:
        return list(self._latencies_ms)

    @property
    def running(self) -> bool:
        return self._task is not None

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(
                self._run(), name=f"writer:{self.store.study_id}"
            )

    async def stop(self) -> None:
        if self._task is None:
            return
        await self.queue.put(None)
        await self._task
        self._task = None

    async def _run(self) -> None:
        while True:
            item = await self.queue.get()
            if item is None:
                return
            loop = asyncio.get_running_loop()
            t0 = loop.time()
            try:
                result = await item.coro_factory()
                if not item.future.done():
                    item.future.set_result(result)
            except Exception as e:
                if not item.future.done():
                    item.future.set_exception(e)
            finally:
                self._latencies_ms.append((loop.time() - t0) * 1000)
                self.queue.task_done()

    async def submit(self, factory: WriteFn) -> Any:
        if self._task is None:
            raise RuntimeError("StudyWriter not started")
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[Any] = loop.create_future()
        await self.queue.put(_Item(coro_factory=factory, future=fut))
        self._depth_high_water = max(self._depth_high_water, self.queue.qsize())
        return await fut

    async def append(
        self,
        event: UnstampedEvent,
        *,
        _source_fetched_cap_id: str | None = None,
    ) -> StoredEvent:
        out = await self.append_many(
            [event], _source_fetched_cap_id=_source_fetched_cap_id
        )
        if out:
            return out[0]
        # Defensive: append_many always returns stored envelopes (incl. idempotent).
        raise RuntimeError(f"append produced no result for idem_key={event.idem_key}")

    async def append_many(
        self,
        events: list[UnstampedEvent],
        *,
        require_open_attempt: str | None = None,
        _source_fetched_cap_id: str | None = None,
    ) -> list[StoredEvent]:
        for ev in events:
            if getattr(ev, "type", None) == "SourceFetched":
                # Capability id required; store takes the effects write permit.
                from coscientist.core.events.errors import ArtifactRejected

                if not _source_fetched_cap_id:
                    raise ArtifactRejected("fetch_capability_required")

        _cap_id = _source_fetched_cap_id

        async def _do() -> list[StoredEvent]:
            return await self.store.append_many(
                events,
                require_open_attempt=require_open_attempt,
                _source_fetched_cap_id=_cap_id,
            )

        return await self.submit(_do)
