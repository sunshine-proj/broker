"""Per-provider rate limiter.

Enforces every configured limit over a rolling window. Usage is held in
memory: it is shared by everything in this process (scheduled jobs, manual
triggers, retries) but starts from zero after a restart.
"""

import asyncio
import time
from collections import deque
from collections.abc import Awaitable, Callable, Sequence

from app.providers.errors import RateBudgetExceededError
from app.schemas.provider_config import RateLimit


class _Window:
    def __init__(self, limit: RateLimit) -> None:
        self.limit = limit.limit
        self.period = float(limit.period.seconds)
        self.used = 0
        self._entries: deque[tuple[float, int]] = deque()

    def expire(self, now: float) -> None:
        while self._entries and self._entries[0][0] + self.period <= now:
            self.used -= self._entries.popleft()[1]

    def wait_for(self, cost: int, now: float) -> float:
        """Seconds until `cost` more credits fit; 0 if they fit now."""
        excess = self.used + cost - self.limit
        if excess <= 0:
            return 0.0
        freed = 0
        for timestamp, entry_cost in self._entries:
            freed += entry_cost
            if freed >= excess:
                return timestamp + self.period - now
        return self.period

    def record(self, cost: int, now: float) -> None:
        self._entries.append((now, cost))
        self.used += cost


class RateLimiter:
    def __init__(
        self,
        provider: str,
        limits: Sequence[RateLimit],
        *,
        max_wait_seconds: float,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._provider = provider
        self._windows = [_Window(limit) for limit in limits]
        self._max_wait = max_wait_seconds
        self._clock = clock
        self._sleep = sleep
        self._lock = asyncio.Lock()

    async def acquire(self, cost: int = 1) -> None:
        """Reserve `cost` request credits, waiting for capacity if the wait is short.

        Raises RateBudgetExceededError when capacity would take longer than
        `max_wait_seconds` to free up, which is the normal outcome for an
        exhausted daily or monthly quota.
        """
        for window in self._windows:
            if cost > window.limit:
                raise RateBudgetExceededError(
                    self._provider,
                    f"a request costing {cost} credits can never fit a limit of {window.limit}",
                    retry_after=window.period,
                )

        # The lock is held while waiting, so callers are served in order.
        async with self._lock:
            waited = 0.0
            while True:
                now = self._clock()
                wait = 0.0
                for window in self._windows:
                    window.expire(now)
                    wait = max(wait, window.wait_for(cost, now))
                if wait <= 0:
                    for window in self._windows:
                        window.record(cost, now)
                    return
                if waited + wait > self._max_wait:
                    raise RateBudgetExceededError(
                        self._provider,
                        f"rate budget exhausted; capacity frees up in {wait:.0f} seconds",
                        retry_after=wait,
                    )
                await self._sleep(wait)
                waited += wait
