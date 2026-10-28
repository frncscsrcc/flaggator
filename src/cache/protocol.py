"""The cache operations both implementations provide.

get returns bytes or None. set stores bytes for a positive TTL. delete
removes a key and does nothing when the key is absent. The cache does not
know what a flag is.
"""

from typing import Protocol


class Cache(Protocol):
    """A byte store with a time to live on each write."""

    def get(self, key: str) -> bytes | None:
        """Return the stored bytes, or None when the key is missing or expired."""

    def set(self, key: str, value: bytes, ttl_seconds: int) -> None:
        """Store value under key until ttl_seconds have passed.

        ttl_seconds is a positive integer. A later get returns None after
        that interval.
        """

    def delete(self, key: str) -> None:
        """Remove key. A missing key is not an error."""
