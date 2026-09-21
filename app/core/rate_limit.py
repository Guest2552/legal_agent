"""Sliding-window rate limiter protecting the (paid) Gemini endpoints from abuse."""

from __future__ import annotations

import math
import time
from collections import deque
from collections.abc import Callable


class RateLimiter:
    """Allow at most ``limit`` hits per ``window`` seconds for each key."""

    def __init__(self, limit: int, window: float = 60.0, clock: Callable[[], float] = time.monotonic) -> None:
        self._limit = limit
        self._window = window
        self._clock = clock
        self._hits: dict[str, deque[float]] = {}

    def hit(self, key: str) -> int | None:
        """Record a hit. Returns ``None`` if allowed, else seconds until the next slot."""
        now = self._clock()
        hits = self._hits.setdefault(key, deque())
        while hits and hits[0] <= now - self._window:
            hits.popleft()
        if len(hits) >= self._limit:
            return max(1, math.ceil(self._window - (now - hits[0])))
        hits.append(now)
        if len(self._hits) > 10_000:
            self._compact(now)
        return None

    def _compact(self, now: float) -> None:
        stale = [key for key, hits in self._hits.items() if not hits or hits[-1] <= now - self._window]
        for key in stale:
            del self._hits[key]
