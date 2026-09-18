"""
Shared in-memory rate limiters.

Two independent limiter shapes live here because Fyrion needs both:

* :class:`TokenBucket` answers "is this actor going faster than an allowance?"
  for a continuous stream (AutoMod message spam). One token is spent per event
  and the bucket refills continuously; an empty bucket means the actor is over
  the allowance. Strikes track repeat offences inside a rolling window so a
  single burst never escalates on its own.
* :class:`SlidingWindow` answers "have there been at least N events in the last
  W seconds?" by keeping the event timestamps and pruning the ones that have
  aged out. AntiNuke uses it to detect a burst of destructive audit-log actions
  by one actor, where the exact count within the window is what matters.

Both are pure in-memory structures with no I/O, keyed by the caller. The caller
owns eviction of idle keys (see the ``is_idle`` predicates) so the backing
dictionaries never grow without bound.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

__all__ = ["TokenBucket", "SlidingWindow"]


@dataclass
class TokenBucket:
    """Classic token bucket: ``capacity`` events, refilled continuously.

    One token is consumed per event. An empty bucket means events are arriving
    faster than the configured allowance. Strikes track repeat offences inside a
    rolling window, so a single burst never escalates on its own.
    """

    tokens: float
    updated_at: float
    strikes: int = 0
    strike_window_ends_at: float = 0.0

    def consume(self, capacity: float, refill_per_second: float, now: float) -> bool:
        """Returns True when the event is within the allowance."""
        elapsed = max(0.0, now - self.updated_at)
        self.tokens = min(capacity, self.tokens + elapsed * refill_per_second)
        self.updated_at = now

        if self.tokens < 1.0:
            return False

        self.tokens -= 1.0
        return True

    def register_strike(self, now: float, window_seconds: float) -> int:
        """Records a violation and returns the strike count in the window."""
        if now > self.strike_window_ends_at:
            self.strikes = 0
        self.strikes += 1
        self.strike_window_ends_at = now + window_seconds
        return self.strikes

    def is_idle(self, now: float, idle_ttl_seconds: float) -> bool:
        """True when the bucket has been quiet long enough to evict."""
        return (
            now - self.updated_at > idle_ttl_seconds
            and now > self.strike_window_ends_at
        )


@dataclass
class SlidingWindow:
    """Counts events inside a trailing time window.

    Each :meth:`hit` records the event time and returns how many events fall in
    the last ``window_seconds``, so a caller can compare the count against a
    threshold. Timestamps older than the window are discarded on every call, so
    the deque never holds more than one window's worth of events.
    """

    events: deque[float] = field(default_factory=deque)

    def _prune(self, now: float, window_seconds: float) -> None:
        cutoff = now - window_seconds
        events = self.events
        while events and events[0] <= cutoff:
            events.popleft()

    def hit(self, now: float, window_seconds: float) -> int:
        """Records an event and returns the count within the window."""
        self._prune(now, window_seconds)
        self.events.append(now)
        return len(self.events)

    def count(self, now: float, window_seconds: float) -> int:
        """Returns the count within the window without recording an event."""
        self._prune(now, window_seconds)
        return len(self.events)

    def reset(self) -> None:
        """Drops every recorded event (used after a punishment fires)."""
        self.events.clear()

    def is_idle(self, now: float, window_seconds: float) -> bool:
        """True when no event falls inside the window any more."""
        self._prune(now, window_seconds)
        return not self.events
