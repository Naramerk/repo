"""Single asyncio write queue per study (DESIGN §3.5a)."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from coscientist.core.errors import Idempotent, LeaseLost
from coscientist.core.events import EventEnvelope
from coscientist.core.store import SqliteEventStore

WriteFn = Callable[[], Awaitable[Any]]


@dataclass
class _Item:
    coro_factory: WriteFn
    future: asyncio.Future[Any]
    events: list[EventEnvelope] = field(default_factory=list)


class StudyWriter:
    """Serializes all study mutations through one queue (maxsize=1000)."""

    def __init__(self, store: SqliteEventStore, *, maxsize: int = 1000) -> None:
        self.store = store
        self.queue: asyncio.Queue[_Item | None] = asyncio.Queue(maxsize=maxsize)
        self._task: asyncio.Task[None] | None = None
        self._depth_high_water = 0
        self.write_latencies_ms: list[float] = []

    @property
    def depth(self) -> int:
        return self.queue.qsize()

    @property
    def depth_high_water(self) -> int:
        return self._depth_high_water

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name=f"writer:{self.store.study_id}")

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
            except Idempotent as e:
                if not item.future.done():
                    item.future.set_result(e)
            except Exception as e:
                if not item.future.done():
                    item.future.set_exception(e)
            finally:
                dt = (loop.time() - t0) * 1000
                self.write_latencies_ms.append(dt)
                self.queue.task_done()

    async def submit(self, factory: WriteFn) -> Any:
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[Any] = loop.create_future()
        item = _Item(coro_factory=factory, future=fut)
        await self.queue.put(item)
        self._depth_high_water = max(self._depth_high_water, self.queue.qsize())
        return await fut

    async def append(self, event: EventEnvelope) -> EventEnvelope | Idempotent:
        async def _do() -> EventEnvelope:
            try:
                return await self.store.append(event)
            except Idempotent as e:
                return e  # type: ignore[return-value]

        result = await self.submit(_do)
        return result

    async def append_many(self, events: list[EventEnvelope]) -> list[EventEnvelope]:
        async def _do() -> list[EventEnvelope]:
            out: list[EventEnvelope] = []
            pending = list(events)
            # batch in one store transaction
            try:
                return await self.store.append_many(pending)
            except Idempotent:
                # fall back one-by-one for mixed idempotent batches
                for ev in pending:
                    try:
                        out.append(await self.store.append(ev))
                    except Idempotent:
                        continue
                return out

        return await self.submit(_do)

    async def guarded(self, factory: WriteFn) -> Any:
        """Run a write; LeaseLost propagates and stops further writers from succeeding."""
        try:
            return await self.submit(factory)
        except LeaseLost:
            raise
