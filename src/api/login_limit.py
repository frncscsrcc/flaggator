"""In-process limit for local login attempts.

One process, one map. A reverse proxy in front of several processes does
not share this map. The key is the client address, never the email.
"""

import threading
import time
from collections.abc import Callable


class LoginLimiter:
    """How many login attempts one address may make in a window.

    limit is the number of attempts. window_seconds is how long an attempt
    stays counted. clock returns a monotonic time in seconds.
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
        """Return True when this address may attempt login now.

        A hit is recorded only when the attempt is allowed. An empty
        address shares one bucket. Does not read the database.
        """
        key = client_address if client_address != "" else "unknown"
        now = self._clock()
        with self._lock:
            recent = [
                stamp
                for stamp in self._hits.get(key, [])
                if now - stamp < self._window_seconds
            ]
            if len(recent) >= self._limit:
                self._hits[key] = recent
                return False
            recent.append(now)
            self._hits[key] = recent
            return True
