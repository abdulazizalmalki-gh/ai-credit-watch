"""Refresh abuse protection.

``/api/balances?refresh=true`` deliberately bypasses the cache, so it is the only
path that can make the app call provider APIs on demand — and therefore the only
one worth ratelimiting. Rules:

* per client: at most one *upstream-triggering* request per ``min_interval`` seconds
* globally: at most ``max_per_minute`` upstream-triggering requests per minute
  (protects provider quotas even when many clients share the dashboard)

A cached response needs no token at all, so normal use (page load, auto refresh
every minute, several open tabs) never trips this. Set
``REFRESH_MIN_INTERVAL_SECONDS=0`` to switch the limiter off.
"""

from __future__ import annotations

import time
from collections import deque
from typing import Callable

#: Clients are remembered for this long after their last accepted request.
_CLIENT_RETENTION_SECONDS = 600.0
#: Hard cap on tracked clients, so a spoofed-IP flood cannot grow memory forever.
_MAX_TRACKED_CLIENTS = 4096

LOOPBACK = {"127.0.0.1", "::1", "localhost"}


def client_key(client_host: str | None, forwarded_for: str | None = None) -> str:
    """Identify a caller.

    ``X-Forwarded-For`` is only honoured when the direct peer is loopback (that is
    exactly the case uvicorn's ``--proxy-headers`` trusts by default), so a public
    client cannot fake its identity by sending the header itself.
    """
    peer = (client_host or "unknown").strip()
    if peer in LOOPBACK and forwarded_for:
        first = forwarded_for.split(",")[0].strip()
        if first:
            return first
    return peer


class RefreshLimiter:
    """Sliding-window limiter returning retry hints instead of raising."""

    def __init__(
        self,
        min_interval: float = 10.0,
        max_per_minute: int = 20,
        clock: Callable[[], float] = time.monotonic,
        window: float = 60.0,
    ) -> None:
        self.min_interval = max(0.0, float(min_interval))
        self.max_per_minute = max(0, int(max_per_minute))
        self.window = window
        self._clock = clock
        self._last: dict[str, float] = {}
        self._events: deque[float] = deque()

    @property
    def enabled(self) -> bool:
        return self.min_interval > 0 or self.max_per_minute > 0

    def describe(self) -> dict[str, float | int | bool]:
        return {
            "enabled": self.enabled,
            "refresh_min_interval_seconds": self.min_interval,
            "refresh_max_per_minute": self.max_per_minute,
        }

    def _trim(self, now: float) -> None:
        while self._events and now - self._events[0] >= self.window:
            self._events.popleft()
        if len(self._last) > _MAX_TRACKED_CLIENTS:
            cutoff = now - _CLIENT_RETENTION_SECONDS
            self._last = {key: seen for key, seen in self._last.items() if seen >= cutoff}
            if len(self._last) > _MAX_TRACKED_CLIENTS:  # still too many: drop the oldest half
                ordered = sorted(self._last.items(), key=lambda item: item[1])
                self._last = dict(ordered[len(ordered) // 2 :])

    def acquire(self, key: str) -> tuple[bool, float]:
        """Reserve one upstream call. Returns ``(allowed, retry_after_seconds)``.

        No awaits inside, so the check-and-record pair is atomic within the event
        loop (single-process uvicorn).
        """
        if not self.enabled:
            return True, 0.0

        now = self._clock()
        self._trim(now)

        if self.max_per_minute and len(self._events) >= self.max_per_minute:
            oldest = self._events[0]
            return False, max(self.window - (now - oldest), 0.0) or 1.0

        if self.min_interval:
            last = self._last.get(key)
            if last is not None and now - last < self.min_interval:
                return False, self.min_interval - (now - last)

        self._last[key] = now
        if self.max_per_minute:
            self._events.append(now)
        return True, 0.0
