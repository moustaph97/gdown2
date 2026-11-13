"""Asynchronous per-host token bucket rate limiter."""

from __future__ import annotations

import asyncio
import time


class TokenBucketRateLimiter:
    """Limits outbound HTTP request rate per host using a standard token bucket."""

    def __init__(self, rate_per_sec: float = 5.0, burst: float | None = None) -> None:
        self._rate = rate_per_sec
        self._burst = burst if burst is not None else max(1.0, rate_per_sec)
        self._tokens: dict[str, float] = {}
        self._updated_at: dict[str, float] = {}
        self._lock = asyncio.Lock()

    @property
    def enabled(self) -> bool:
        return self._rate > 0

    async def acquire(self, host: str = "default", tokens: float = 1.0) -> None:
        """Wait until ``tokens`` are available in the bucket for ``host``."""
        if self._rate <= 0:
            return

        while True:
            async with self._lock:
                now = time.monotonic()
                current = self._tokens.get(host, self._burst)
                last = self._updated_at.get(host, now)
                elapsed = max(0.0, now - last)
                current = min(self._burst, current + elapsed * self._rate)
                self._updated_at[host] = now

                if current >= tokens:
                    self._tokens[host] = current - tokens
                    return

                deficit = tokens - current
                wait_time = deficit / self._rate
                self._tokens[host] = current

            await asyncio.sleep(wait_time)

