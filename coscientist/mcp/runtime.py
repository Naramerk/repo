"""Injected clock / async primitives for the MCP client (no global time/sleep)."""

from __future__ import annotations

from typing import Awaitable, Protocol, TypeVar

T = TypeVar("T")


class Clock(Protocol):
    def monotonic(self) -> float:
        """Monotonic seconds for deadlines."""


class AsyncSleep(Protocol):
    async def __call__(self, delay_s: float) -> None: ...


class AsyncWaitFor(Protocol):
    async def __call__(self, awaitable: Awaitable[T], *, timeout_s: float) -> T: ...
