"""Global wideq rate limiter — a backstop against runaway polling.

Even though wideq polling is low-rate by design (one refresh_devices() call per
cycle), this serializes calls, spaces them, and caps calls/hour so that no
combination of options or bugs can reproduce the request storm that caused the
original 24h block.
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from collections.abc import Awaitable, Callable

_LOGGER = logging.getLogger(__name__)


class GlobalRateLimiter:
    def __init__(
        self,
        max_per_hour: int,
        min_spacing: float,
        *,
        clock: Callable[[], float] | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        self._max = max_per_hour
        self._spacing = min_spacing
        self._clock = clock
        self._sleep = sleep or asyncio.sleep
        self._lock = asyncio.Lock()
        self._last = 0.0
        self._calls: deque[float] = deque()

    async def acquire(self) -> None:
        async with self._lock:
            loop = asyncio.get_running_loop()
            now_fn = self._clock or loop.time
            now = now_fn()
            # Drop calls older than one hour.
            while self._calls and now - self._calls[0] >= 3600:
                self._calls.popleft()
            # Hourly cap backstop.
            if len(self._calls) >= self._max:
                wait = 3600 - (now - self._calls[0])
                _LOGGER.warning(
                    "wideq hourly cap (%d) hit; delaying %.0fs", self._max, wait
                )
                await self._sleep(max(wait, 0))
                now = now_fn()
                while self._calls and now - self._calls[0] >= 3600:
                    self._calls.popleft()
            # Minimum spacing between any two calls.
            gap = now - self._last
            if gap < self._spacing:
                await self._sleep(self._spacing - gap)
                now = now_fn()
            self._last = now
            self._calls.append(now)
