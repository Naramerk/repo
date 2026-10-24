"""Injected clock / async primitives for the LLM client (no global time/sleep)."""

from __future__ import annotations

from datetime import datetime
from typing import Awaitable, Protocol, TypeVar

T = TypeVar("T")


class Clock(Protocol):
    def monotonic(self) -> float:
        """Monotonic seconds for deadlines and latency."""

    def wall_utc(self) -> datetime:
        """Timezone-aware UTC wall time for Retry-After HTTP-date."""


class AsyncSleep(Protocol):
    async def __call__(self, delay_s: float) -> None: ...


class AsyncWaitFor(Protocol):
    async def __call__(self, awaitable: Awaitable[T], *, timeout_s: float) -> T: ...
