"""An in-process cache.

One map, lost when the process exits. A lock lets the threaded HTTP adapter
share it. Values are bytes. This module does not know what a flag is.
"""

import threading
import time
from collections.abc import Callable

DEFAULT_MAX_ENTRIES = 10000


class MemoryCache:
    """A locked map of bytes with a monotonic deadline on each entry."""

    def __init__(
        self,
        max_entries: int = DEFAULT_MAX_ENTRIES,
        clock: Callable[[], float] | None = None,
    ) -> None:
        """Create an empty map.

        max_entries bounds the map. clock returns a monotonic time in
        seconds; tests pass a fake one. The default clock is time.monotonic.
        Does not read the database.
        """
        if max_entries < 1:
            raise ValueError("max_entries must be at least 1.")
        self._max_entries = max_entries
        if clock is None:
            self._clock = time.monotonic
        else:
            self._clock = clock
        self._lock = threading.Lock()
        self._entries: dict[str, tuple[bytes, float]] = {}

    def get(self, key: str) -> bytes | None:
        """Return the value, or None when key is missing or the deadline has passed.

        An expired entry is removed. Does not extend the deadline.
        """
        with self._lock:
            found = self._entries.get(key)
            if found is None:
                return None
            value, deadline = found
            if self._clock() >= deadline:
                del self._entries[key]
                return None
            return value

    def set(self, key: str, value: bytes, ttl_seconds: int) -> None:
        """Store value until ttl_seconds have passed on the monotonic clock.

        Raises ValueError when ttl_seconds is not positive. When the map is
        full, expired entries are removed first, then the entry with the
        soonest deadline, until this key fits. Replacing an existing key
        does not grow the map.
        """
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be a positive integer.")
        deadline = self._clock() + ttl_seconds
        with self._lock:
            if key not in self._entries and len(self._entries) >= self._max_entries:
                self._drop_expired()
            while key not in self._entries and len(self._entries) >= self._max_entries:
                self._drop_soonest()
            self._entries[key] = (value, deadline)

    def delete(self, key: str) -> None:
        """Remove key. A missing key stays missing. Does not raise."""
        with self._lock:
            self._entries.pop(key, None)

    def _drop_expired(self) -> None:
        """Delete entries whose deadline has passed. The caller holds the lock."""
        now = self._clock()
        expired = [key for key, (_value, deadline) in self._entries.items() if deadline <= now]
        for key in expired:
            del self._entries[key]

    def _drop_soonest(self) -> None:
        """Delete the entry with the earliest deadline. The caller holds the lock.

        Does nothing when the map is empty, so a zero-capacity bug cannot spin.
        """
        if not self._entries:
            return
        soonest_key = ""
        soonest_deadline: float | None = None
        for key, (_value, deadline) in self._entries.items():
            if soonest_deadline is None or deadline < soonest_deadline:
                soonest_key = key
                soonest_deadline = deadline
        del self._entries[soonest_key]
