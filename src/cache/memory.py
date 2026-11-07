"""An in-process cache.

One map, lost when the process exits. A lock lets the threaded HTTP adapter
share it. Values are bytes. This module does not know what a flag is.
"""

import threading
import time
from collections.abc import Callable

DEFAULT_MAX_ENTRIES = 10000
DEFAULT_MAX_BYTES = 128 * 1024 * 1024


class MemoryCache:
    """A locked map of bytes with a monotonic deadline on each entry."""

    def __init__(
        self,
        max_entries: int = DEFAULT_MAX_ENTRIES,
        clock: Callable[[], float] | None = None,
        max_bytes: int = DEFAULT_MAX_BYTES,
    ) -> None:
        """Create an empty map.

        max_entries bounds how many keys are kept. max_bytes bounds the
        sum of their values. clock returns a monotonic time in seconds;
        tests pass a fake one. The default clock is time.monotonic. Does
        not read the database.
        """
        if max_entries < 1:
            raise ValueError("max_entries must be at least 1.")
        if max_bytes < 1:
            raise ValueError("max_bytes must be at least 1.")
        self._max_entries = max_entries
        self._max_bytes = max_bytes
        if clock is None:
            self._clock = time.monotonic
        else:
            self._clock = clock
        self._lock = threading.Lock()
        self._entries: dict[str, tuple[bytes, float]] = {}
        self._bytes = 0

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
                self._forget(key)
                return None
            return value

    def set(self, key: str, value: bytes, ttl_seconds: int) -> None:
        """Store value until ttl_seconds have passed on the monotonic clock.

        Raises ValueError when ttl_seconds is not positive. Expired entries
        go first. Then the entry with the soonest deadline goes, until this
        value fits the entry cap and the byte budget. A value larger than
        the budget is not stored, and that set does not evict other keys.
        Replacing a key frees its previous size.
        """
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be a positive integer.")
        deadline = self._clock() + ttl_seconds
        size = len(value)
        with self._lock:
            if size > self._max_bytes:
                return
            self._drop_expired()
            while not self._fits(key, size):
                if not self._drop_soonest(skip=key):
                    self._forget(key)
                    return
            self._forget(key)
            self._entries[key] = (value, deadline)
            self._bytes += size

    def delete(self, key: str) -> None:
        """Remove key. A missing key stays missing. Does not raise."""
        with self._lock:
            self._forget(key)

    def _drop_expired(self) -> None:
        """Delete entries whose deadline has passed. The caller holds the lock."""
        now = self._clock()
        expired = [key for key, (_value, deadline) in self._entries.items() if deadline <= now]
        for key in expired:
            self._forget(key)

    def _fits(self, key: str, size: int) -> bool:
        """Return True when storing size at key stays inside both caps.

        The caller holds the lock. The key's current value is not counted,
        because a set replaces it.
        """
        current = self._entries.get(key)
        current_size = 0 if current is None else len(current[0])
        count = len(self._entries) if current is not None else len(self._entries) + 1
        total = self._bytes - current_size + size
        return count <= self._max_entries and total <= self._max_bytes

    def _drop_soonest(self, skip: str) -> bool:
        """Delete the earliest deadline other than skip.

        The caller holds the lock. Returns False when no other entry exists.
        """
        soonest_key = ""
        soonest_deadline: float | None = None
        for key, (_value, deadline) in self._entries.items():
            if key == skip:
                continue
            if soonest_deadline is None or deadline < soonest_deadline:
                soonest_key = key
                soonest_deadline = deadline
        if soonest_deadline is None:
            return False
        self._forget(soonest_key)
        return True

    def _forget(self, key: str) -> None:
        """Remove key and its bytes. The caller holds the lock.

        A missing key stays missing.
        """
        found = self._entries.pop(key, None)
        if found is not None:
            self._bytes -= len(found[0])
