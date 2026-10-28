"""A cache that stores nothing.

SQL tests use this so a hit cannot hide a bad query. A deploy that wants
every read to hit the database selects it in configuration.
"""


class NullCache:
    """get is always a miss. set and delete change nothing."""

    def get(self, key: str) -> bytes | None:
        """Return None. key is ignored. Does not store the lookup."""
        return None

    def set(self, key: str, value: bytes, ttl_seconds: int) -> None:
        """Accept the write and discard it.

        Raises ValueError when ttl_seconds is not positive, so a bad TTL is
        still reported when the cache is off. Does not store value.
        """
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be a positive integer.")

    def delete(self, key: str) -> None:
        """Do nothing. A missing key is the only state this cache has."""
        return None
