"""
Tests for MatchedPairStream backpressure, debounce, and emit throttle.
TDD: these tests are written BEFORE the implementation details are confirmed.

Task 10 tests:
- test_bounded_queue_drops_oldest_on_overflow
- test_per_canonical_debounce
- test_emit_throttle_caps_at_ten_per_sec_per_side
"""
import asyncio
import time

import pytest

from arbiter.scanner.matched_pair_stream import MatchedPair, MatchedPairStream, _QUEUE_MAXSIZE
from arbiter.utils.price_store import PricePoint


def _make_quote(platform: str, canonical_id: str, ts: float | None = None) -> PricePoint:
    return PricePoint(
        platform=platform,
        canonical_id=canonical_id,
        yes_price=0.50,
        no_price=0.50,
        yes_volume=100.0,
        no_volume=100.0,
        timestamp=ts if ts is not None else time.time(),
        raw_market_id=f"{platform}-{canonical_id}",
        yes_market_id=f"{platform}-{canonical_id}-yes",
        no_market_id=f"{platform}-{canonical_id}-no",
    )


def test_bounded_queue_drops_oldest_on_overflow():
    """Push 5001 items without consuming; backpressure_drops counter == 1."""

    async def runner():
        # Use MatchedPairStream with default bounded queue
        stream = MatchedPairStream()
        assert stream._queue.maxsize == _QUEUE_MAXSIZE, (
            f"Queue maxsize should be {_QUEUE_MAXSIZE}"
        )

        # To push exactly maxsize+1 items we need maxsize+1 distinct canonicals
        # each with both sides pre-seeded, so debounce/throttle don't interfere.
        # We first seed all kalshi sides, then push poly sides one by one.
        n = _QUEUE_MAXSIZE + 1  # 5001

        # Seed kalshi side for all canonicals
        for i in range(n):
            await stream.on_quote(_make_quote("kalshi", f"BP-{i}"))

        # Now push poly sides — each triggers a match emit
        # Throttle starts with full bucket (10 tokens), but each canonical is a
        # distinct throttle key so all pass throttle; debounce window is 50ms and
        # we're not hammering one canonical.
        for i in range(n):
            await stream.on_quote(_make_quote("polymarket", f"BP-{i}"))

        assert stream.backpressure_drops >= 1, (
            f"Expected at least 1 backpressure drop, got {stream.backpressure_drops}"
        )
        assert stream._queue.qsize() <= _QUEUE_MAXSIZE, (
            "Queue should never exceed maxsize"
        )

    asyncio.run(runner())


def test_per_canonical_debounce():
    """30 updates in 100ms for one canonical → ≤ 2 matches emitted (debounce coalesces rest)."""

    async def runner():
        stream = MatchedPairStream()

        canonical = "DEBOUNCE-TEST"
        # Seed both sides so subsequent updates trigger matching
        await stream.on_quote(_make_quote("kalshi", canonical))
        await stream.on_quote(_make_quote("polymarket", canonical))

        # Drain the initial pair
        _ = stream._queue.get_nowait()

        # Now hammer the same canonical 30 times within ~100ms
        start_q = stream._queue.qsize()
        start_debounce = stream.debounce_coalesced

        for _ in range(30):
            await stream.on_quote(_make_quote("kalshi", canonical))

        coalesced = stream.debounce_coalesced - start_debounce
        emitted = stream._queue.qsize() - start_q

        assert emitted <= 2, (
            f"Expected ≤2 emits from 30 rapid updates, got {emitted}; "
            f"coalesced={coalesced}"
        )
        assert coalesced >= 1, (
            f"Expected at least 1 debounce coalescion from 30 rapid updates, "
            f"got {coalesced}"
        )

    asyncio.run(runner())


def test_emit_throttle_caps_at_ten_per_sec_per_side():
    """20 opportunities detected in 1s for same canonical/side → ≤10 emitted, ≥10 dropped.

    Strategy: inject all 20 updates in rapid-fire synchronous bursts of 1 per
    debounce window (55ms sleep between groups), but exhaust tokens in the first
    10 calls.  After 10 emits within ~50ms the token bucket is dry; the next 10
    within the same ~1s window are throttle-dropped.

    Implementation notes:
    - We disable debounce for this test by using a subclass that overrides
      _should_debounce → always False, so ALL 20 reach the throttle gate.
    - Token bucket: 10 max / 10 per sec.  20 rapid calls (< 1ms apart) means
      no meaningful refill between calls, so first 10 pass, next 10 are dropped.
    """

    class _NoDebounceStream(MatchedPairStream):
        def _should_debounce(self, canonical_id: str) -> bool:
            return False

    async def runner():
        stream = _NoDebounceStream()

        canonical = "THROTTLE-TEST"
        # Seed kalshi side
        await stream.on_quote(_make_quote("kalshi", canonical))

        start_throttle_drops = stream.throttle_drops

        # Fire 20 poly updates rapidly (no sleep — token bucket gets no refill)
        for _ in range(20):
            await stream.on_quote(_make_quote("polymarket", canonical))

        drops = stream.throttle_drops - start_throttle_drops
        emitted = stream._queue.qsize()

        assert emitted <= 10, (
            f"Expected ≤10 emits from 20 poly updates (throttle at 10/sec), got {emitted}"
        )
        assert drops >= 10, (
            f"Expected ≥10 throttle drops, got {drops}"
        )

    asyncio.run(runner())
