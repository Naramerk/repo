"""Wall-clock adapter satisfying ``coscientist.llm.runtime.Clock``."""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from typing import Awaitable, TypeVar

T = TypeVar("T")


class WallLlmClock:
    def monotonic(self) -> float:
        return time.monotonic()

    def wall_utc(self) -> datetime:
        return datetime.now(UTC)


async def wall_sleep(delay_s: float) -> None:
    await asyncio.sleep(delay_s)


async def wall_wait_for(awaitable: Awaitable[T], *, timeout_s: float) -> T:
    return await asyncio.wait_for(awaitable, timeout=timeout_s)
