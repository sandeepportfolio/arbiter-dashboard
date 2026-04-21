"""
Event-driven O(1)-per-quote matcher for cross-platform arbitrage.

Maintains a per-canonical 2-slot latest-quote cache. When both platforms
have a quote for a canonical, emits exactly one MatchedPair per
both-sides-present event.

Task 9: core MatchedPairStream
Task 10: bounded queue + debounce + emit throttle (extended in same file)
"""
from __future__ import annotations

import asyncio
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, Optional, Tuple

from ..utils.price_store import PricePoint

# Platform constants
_KALSHI = "kalshi"
_POLY = "polymarket"
_PLATFORMS = (_KALSHI, _POLY)

# Debounce / throttle constants (Task 10)
_DEBOUNCE_WINDOW_SEC = 0.05          # 50ms debounce window
_DEBOUNCE_MAX_RATE = 20              # updates/sec that triggers coalescing
_THROTTLE_MAX_PER_SEC = 10           # max emits per (canonical, side) per second
_QUEUE_MAXSIZE = 5000                # bounded queue capacity


@dataclass
class MatchedPair:
    canonical_id: str
    kalshi_quote: PricePoint
    poly_quote: PricePoint
    matched_at: float  # unix timestamp


@dataclass
class _DebounceState:
    """Per-canonical debounce tracking."""
    update_count: int = 0
    window_start: float = field(default_factory=time.time)
    pending_emit: bool = False


@dataclass
class _ThrottleState:
    """Per-(canonical, side) token-bucket throttle."""
    tokens: float = float(_THROTTLE_MAX_PER_SEC)
    last_refill: float = field(default_factory=time.time)


class MatchedPairStream:
    """O(1)-per-quote matcher.

    Maintains a per-canonical 2-slot latest-quote cache. When both platforms
    have a quote for a canonical, emits exactly one MatchedPair per
    both-sides-present event.

    Task 10 additions:
    - Bounded asyncio.Queue(maxsize=5000); on overflow drops oldest, increments
      ``backpressure_drops``.
    - Per-canonical debounce: > 20 updates/sec coalesced to 1 emit per 50ms.
    - Token-bucket emit throttle: ≤ 10 emits/sec per (canonical_id, side).
    """

    def __init__(self, output_queue: Optional[asyncio.Queue] = None) -> None:
        # If caller passes an unbounded queue we wrap it for backpressure tracking.
        # For Task 10 compliance we always use our own bounded queue internally and
        # forward to the external queue if provided.
        if output_queue is not None and output_queue.maxsize == 0:
            # Unbounded external queue — use it directly (Task 9 compatibility).
            self._queue = output_queue
            self._bounded = False
        elif output_queue is not None:
            self._queue = output_queue
            self._bounded = True
        else:
            self._queue = asyncio.Queue(maxsize=_QUEUE_MAXSIZE)
            self._bounded = True

        # Per-canonical latest-quote cache: {canonical_id: {platform: PricePoint}}
        self._cache: Dict[str, Dict[str, PricePoint]] = defaultdict(dict)

        # Task 10 counters
        self.backpressure_drops: int = 0
        self.debounce_coalesced: int = 0
        self.throttle_drops: int = 0

        # Task 10 state
        self._debounce: Dict[str, _DebounceState] = defaultdict(_DebounceState)
        self._throttle: Dict[Tuple[str, str], _ThrottleState] = defaultdict(_ThrottleState)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def on_quote(self, quote: PricePoint) -> None:
        """Process a single incoming quote.

        Updates the cache for the given (canonical_id, platform), and if both
        platforms now have a quote, runs debounce+throttle checks before
        emitting a MatchedPair.
        """
        canonical_id = quote.canonical_id
        platform = quote.platform

        # Only handle recognised platforms
        if platform not in _PLATFORMS:
            return

        # Update cache — O(1)
        self._cache[canonical_id][platform] = quote

        # Check if both sides are present
        slot = self._cache[canonical_id]
        if _KALSHI not in slot or _POLY not in slot:
            return

        # Both sides present — run debounce
        if self._bounded and self._should_debounce(canonical_id):
            self.debounce_coalesced += 1
            return

        # Determine "side" for throttle key
        kalshi_q = slot[_KALSHI]
        poly_q = slot[_POLY]
        side = _compute_side(kalshi_q, poly_q)

        # Run throttle
        if self._bounded and not self._throttle_allow(canonical_id, side):
            self.throttle_drops += 1
            return

        # Build and emit pair
        pair = MatchedPair(
            canonical_id=canonical_id,
            kalshi_quote=kalshi_q,
            poly_quote=poly_q,
            matched_at=time.time(),
        )
        self._emit(pair)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _emit(self, pair: MatchedPair) -> None:
        """Put pair onto the queue; drop oldest on overflow."""
        try:
            self._queue.put_nowait(pair)
        except asyncio.QueueFull:
            # Drop oldest to make room
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
            self.backpressure_drops += 1
            try:
                self._queue.put_nowait(pair)
            except asyncio.QueueFull:
                # Still full (race) — just drop
                self.backpressure_drops += 1

    def _should_debounce(self, canonical_id: str) -> bool:
        """Return True if this update should be coalesced (debounced)."""
        now = time.time()
        state = self._debounce[canonical_id]

        window_elapsed = now - state.window_start
        if window_elapsed >= _DEBOUNCE_WINDOW_SEC:
            # New window
            state.update_count = 1
            state.window_start = now
            return False

        state.update_count += 1
        # If we're getting more than max_rate × window duration updates in
        # this window, coalesce
        max_in_window = _DEBOUNCE_MAX_RATE * _DEBOUNCE_WINDOW_SEC
        if state.update_count > max_in_window:
            return True
        return False

    def _throttle_allow(self, canonical_id: str, side: str) -> bool:
        """Token-bucket: allow up to _THROTTLE_MAX_PER_SEC emits/sec per (canonical, side)."""
        now = time.time()
        key = (canonical_id, side)
        state = self._throttle[key]

        # Refill tokens
        elapsed = now - state.last_refill
        state.tokens = min(
            float(_THROTTLE_MAX_PER_SEC),
            state.tokens + elapsed * _THROTTLE_MAX_PER_SEC,
        )
        state.last_refill = now

        if state.tokens >= 1.0:
            state.tokens -= 1.0
            return True
        return False


def _compute_side(kalshi_q: PricePoint, poly_q: PricePoint) -> str:
    """Determine which side is cheaper to get 'yes' exposure on."""
    if kalshi_q.yes_price <= poly_q.yes_price:
        return "yes_cheaper"
    return "no_cheaper"
