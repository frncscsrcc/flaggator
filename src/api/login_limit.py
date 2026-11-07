"""In-process limit for local login attempts and public evaluation.

One process, one map. A reverse proxy in front of several processes does
not share this map. The key is whatever the caller passes: a client
address for the address limits, an email for failed logins.
"""

import threading
import time
from collections.abc import Callable

_MAX_KEYS = 4096


class LoginLimiter:
    """How many hits one key may make in a window.

    limit is the number of hits. window_seconds is how long a hit stays
    counted. clock returns a monotonic time in seconds. The map drops
    empty keys and, past _MAX_KEYS, the keys whose latest hit is oldest.
    A new key is kept: refusing it would let a crowd of addresses lock
    everyone else out.
    """

    def __init__(
        self,
        limit: int = 10,
        window_seconds: float = 60,
        clock: Callable[[], float] | None = None,
    ) -> None:
        """Start with an empty map. Does not read the database."""
        self._limit = limit
        self._window_seconds = window_seconds
        if clock is None:
            self._clock = time.monotonic
        else:
            self._clock = clock
        self._lock = threading.Lock()
        self._hits: dict[str, list[float]] = {}

    def allow(self, client_address: str) -> bool:
        """Return True when this key may make another hit now.

        A hit is recorded only when the attempt is allowed. An empty
        address shares one bucket. Does not read the database.
        """
        key = client_address if client_address != "" else "unknown"
        now = self._clock()
        with self._lock:
            recent = self._fresh(key, now)
            if len(recent) >= self._limit:
                self._store(key, recent)
                return False
            recent.append(now)
            self._store(key, recent)
            self._evict(now)
            return True

    def over_limit(self, key: str) -> bool:
        """Return True when key already has limit hits in the window.

        Does not record a hit. An empty key shares the bucket unknown.
        Does not read the database.
        """
        stored = key if key != "" else "unknown"
        now = self._clock()
        with self._lock:
            recent = self._fresh(stored, now)
            self._store(stored, recent)
            return len(recent) >= self._limit

    def record(self, key: str) -> None:
        """Count one hit for key now.

        Used when a login failed, so a success is not a hit. Does not
        read the database.
        """
        stored = key if key != "" else "unknown"
        now = self._clock()
        with self._lock:
            recent = self._fresh(stored, now)
            recent.append(now)
            self._store(stored, recent)
            self._evict(now)

    def _fresh(self, key: str, now: float) -> list[float]:
        """Return hits for key that are still inside the window.

        The caller holds the lock. Does not write the map.
        """
        return [
            stamp
            for stamp in self._hits.get(key, [])
            if now - stamp < self._window_seconds
        ]

    def _store(self, key: str, recent: list[float]) -> None:
        """Save recent, or drop key when recent is empty.

        The caller holds the lock.
        """
        if recent:
            self._hits[key] = recent
        else:
            self._hits.pop(key, None)

    def _evict(self, now: float) -> None:
        """Keep the map at _MAX_KEYS keys.

        Expired keys go first. The keys that remain over the cap are the
        ones with the oldest latest hit. The caller holds the lock.
        """
        if len(self._hits) <= _MAX_KEYS:
            return
        for key in list(self._hits):
            self._store(key, self._fresh(key, now))
        if len(self._hits) <= _MAX_KEYS:
            return
        ranked = sorted(self._hits, key=lambda item: self._hits[item][-1])
        index = 0
        while len(self._hits) > _MAX_KEYS and index < len(ranked):
            self._hits.pop(ranked[index], None)
            index = index + 1
