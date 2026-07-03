from __future__ import annotations

import time
from collections import OrderedDict
from collections.abc import Hashable


class TTLDedupe:
    """Tiny TTL-keyed dedupe set. Not thread-safe — only used from the event loop."""

    def __init__(self, ttl_seconds: float, max_entries: int = 1024) -> None:
        self._ttl = ttl_seconds
        self._max = max_entries
        self._entries: OrderedDict[Hashable, float] = OrderedDict()

    def _now(self) -> float:
        return time.monotonic()

    def _evict(self) -> None:
        cutoff = self._now() - self._ttl

        while self._entries:
            _key, t = next(iter(self._entries.items()))

            if t < cutoff:
                self._entries.popitem(last=False)
            else:
                break

        while len(self._entries) > self._max:
            self._entries.popitem(last=False)

    def seen(self, key: Hashable) -> bool:
        """Return True if `key` was already recorded within the TTL window.
        Records the key (refreshing the timestamp) either way."""
        self._evict()
        already = key in self._entries

        # Move to end / refresh timestamp.
        self._entries[key] = self._now()
        self._entries.move_to_end(key)

        return already
