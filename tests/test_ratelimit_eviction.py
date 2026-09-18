"""
Regression test for the dashboard rate limiter's key eviction.

When the key table filled and no bucket was stale, _evict used to call
self._hits.clear(), wiping every caller's counters at once — so a flood of
unique keys reset the limiter under the exact load it exists to blunt. Eviction
must instead drop the least-recently-used keys and keep recent ones.

Skipped unless the web extra is installed (fyrion.web.app imports FastAPI).
"""
import pytest

pytest.importorskip("fastapi")

from fyrion.web.app import SlidingWindowLimiter  # noqa: E402


def test_eviction_is_lru_not_a_full_clear():
    limiter = SlidingWindowLimiter(limit=100, window=3600, max_keys=10)

    for i in range(10):
        assert limiter.allow(f"k{i}") is True  # fills the table, oldest first

    # One more key triggers eviction. It must make room without wiping the table.
    assert limiter.allow("k10") is True

    keys = set(limiter._hits)
    assert 0 < len(keys) <= 10          # not cleared to empty
    assert "k0" not in keys             # the least-recently-used key was dropped
    assert "k9" in keys and "k10" in keys  # recent keys survived
