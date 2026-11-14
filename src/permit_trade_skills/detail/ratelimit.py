"""Per-host async token bucket shared by every fetch in a process.

One token per top-level fetch (a portal navigation including its redirect hops,
or one SODA request). Burst 1: requests to a host are spaced 60/rate seconds.
"""

from __future__ import annotations

import asyncio
import time
from typing import Callable, Dict, Mapping, Optional


class HostRateLimiter:
    def __init__(self, default_per_minute: float = 20.0,
                 per_host: Optional[Mapping[str, float]] = None,
                 clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], "asyncio.Future"] = asyncio.sleep):
        self.default = float(default_per_minute)
        self.per_host: Dict[str, float] = {k.lower(): float(v) for k, v in (per_host or {}).items()}
        self._next: Dict[str, float] = {}
        self._locks: Dict[str, asyncio.Lock] = {}
        self._clock = clock
        self._sleep = sleep

    def set_rate(self, host: str, per_minute: float) -> None:
        self.per_host[host.lower()] = float(per_minute)

    def rate(self, host: str) -> float:
        return self.per_host.get(host.lower(), self.default)

    async def acquire(self, host: str) -> float:
        """Wait for the host's next slot; returns seconds waited."""
        host = host.lower()
        rate = self.rate(host)
        if rate <= 0:
            return 0.0
        lock = self._locks.setdefault(host, asyncio.Lock())
        async with lock:
            now = self._clock()
            wait = max(0.0, self._next.get(host, now) - now)
            if wait:
                await self._sleep(wait)
            self._next[host] = max(now, self._next.get(host, now)) + 60.0 / rate
            return wait
