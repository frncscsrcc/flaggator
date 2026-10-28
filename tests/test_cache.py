"""MemoryCache and NullCache."""

import sys
import unittest
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from cache.memory import MemoryCache
from cache.null import NullCache


class ManualClock:
    """A monotonic clock the test moves by hand. Does not sleep."""

    def __init__(self) -> None:
        """Start at zero."""
        self.now = 0.0

    def __call__(self) -> float:
        """Return the time the test last set."""
        return self.now


class MemoryCacheTests(unittest.TestCase):
    def test_expired_entry_is_a_miss(self) -> None:
        """A get at the deadline removes the entry and returns None."""
        clock = ManualClock()
        cache = MemoryCache(clock=clock)
        cache.set("eval:shop:prod", b"bundle", ttl_seconds=5)
        self.assertEqual(cache.get("eval:shop:prod"), b"bundle")
        clock.now = 5
        self.assertIsNone(cache.get("eval:shop:prod"))
        clock.now = 0
        self.assertIsNone(cache.get("eval:shop:prod"))

    def test_delete_missing_key(self) -> None:
        """delete of a key that was never set does not raise."""
        cache = MemoryCache()
        cache.delete("missing")
        self.assertIsNone(cache.get("missing"))

    def test_full_map_drops_the_soonest_deadline(self) -> None:
        """A set past the cap removes expired entries, then the soonest one."""
        clock = ManualClock()
        cache = MemoryCache(max_entries=2, clock=clock)
        cache.set("a", b"a", ttl_seconds=10)
        cache.set("b", b"b", ttl_seconds=30)
        cache.set("c", b"c", ttl_seconds=20)
        self.assertIsNone(cache.get("a"))
        self.assertEqual(cache.get("b"), b"b")
        self.assertEqual(cache.get("c"), b"c")

    def test_non_positive_ttl_is_rejected(self) -> None:
        """set refuses a TTL that would already be expired."""
        cache = MemoryCache()
        with self.assertRaises(ValueError):
            cache.set("a", b"a", ttl_seconds=0)


class NullCacheTests(unittest.TestCase):
    def test_get_is_always_a_miss(self) -> None:
        """set does not make a later get succeed."""
        cache = NullCache()
        cache.set("eval:shop:prod", b"bundle", ttl_seconds=5)
        self.assertIsNone(cache.get("eval:shop:prod"))
        cache.delete("eval:shop:prod")
        self.assertIsNone(cache.get("eval:shop:prod"))
