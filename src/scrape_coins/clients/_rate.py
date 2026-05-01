"""Tiny async leaky-bucket rate limiter shared across clients."""

from __future__ import annotations

import asyncio
import time


class AsyncRateLimiter:
    """Allow at most `rate` operations per second across concurrent callers."""

    def __init__(self, rate: float) -> None:
        if rate <= 0:
            raise ValueError("rate must be > 0")
        self._min_interval = 1.0 / rate
        self._lock = asyncio.Lock()
        self._next_at = 0.0

    async def wait(self) -> None:
        async with self._lock:
            now = time.monotonic()
            wait_for = self._next_at - now
            if wait_for > 0:
                await asyncio.sleep(wait_for)
                now = time.monotonic()
            self._next_at = max(now, self._next_at) + self._min_interval
