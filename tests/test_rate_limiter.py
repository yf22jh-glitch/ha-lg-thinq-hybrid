"""Tests for the global WideQ request backstop."""

from __future__ import annotations

import unittest

from custom_components.my_lg.rate_limiter import GlobalRateLimiter


class FakeClock:
    def __init__(self, now: float = 100.0) -> None:
        self.now = now
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, delay: float) -> None:
        self.sleeps.append(delay)
        self.now += delay


class RateLimiterTests(unittest.IsolatedAsyncioTestCase):
    async def test_minimum_spacing_is_enforced(self) -> None:
        clock = FakeClock()
        limiter = GlobalRateLimiter(
            200, 3, clock=clock, sleep=clock.sleep
        )
        await limiter.acquire()
        await limiter.acquire()
        self.assertEqual(clock.sleeps, [3])
        self.assertEqual(list(limiter._calls), [100, 103])

    async def test_hourly_cap_never_exceeds_limit(self) -> None:
        clock = FakeClock()
        limiter = GlobalRateLimiter(2, 3, clock=clock, sleep=clock.sleep)
        await limiter.acquire()
        await limiter.acquire()
        await limiter.acquire()
        self.assertEqual(clock.sleeps, [3, 3597])
        self.assertLessEqual(len(limiter._calls), 2)
        self.assertEqual(list(limiter._calls), [103, 3700])


if __name__ == "__main__":
    unittest.main()
