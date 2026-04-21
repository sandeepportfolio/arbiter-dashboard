"""
Tests for MatchedPairStream — event-driven O(1) matcher.
TDD: these tests are written BEFORE the implementation.
"""
import asyncio
import time

import pytest

from arbiter.scanner.matched_pair_stream import MatchedPair, MatchedPairStream
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


def test_emits_once_when_both_sides_present():
    """Feed a kalshi quote then a poly quote for same canonical — queue must have 1 MatchedPair."""

    async def runner():
        queue: asyncio.Queue = asyncio.Queue()
        stream = MatchedPairStream(output_queue=queue)

        await stream.on_quote(_make_quote("kalshi", "CANON-1"))
        assert queue.qsize() == 0, "Should not emit with only kalshi side"

        await stream.on_quote(_make_quote("polymarket", "CANON-1"))
        assert queue.qsize() == 1, "Should emit exactly one pair when both sides present"

        pair: MatchedPair = queue.get_nowait()
        assert pair.canonical_id == "CANON-1"
        assert pair.kalshi_quote.platform == "kalshi"
        assert pair.poly_quote.platform == "polymarket"
        assert pair.matched_at > 0

    asyncio.run(runner())


def test_never_emits_with_only_one_side():
    """Feed 10 kalshi quotes for 10 canonicals, no poly quotes — queue must stay empty."""

    async def runner():
        queue: asyncio.Queue = asyncio.Queue()
        stream = MatchedPairStream(output_queue=queue)

        for i in range(10):
            await stream.on_quote(_make_quote("kalshi", f"CANON-{i}"))

        assert queue.qsize() == 0, "No pairs should be emitted with only kalshi side"

    asyncio.run(runner())


def test_1000_canonicals_O_1_per_update():
    """Drive 1000 distinct canonicals (kalshi+poly pair each) — must complete < 1s, queue has exactly 1000 pairs."""

    async def runner():
        queue: asyncio.Queue = asyncio.Queue()
        stream = MatchedPairStream(output_queue=queue)

        start = time.monotonic()
        for i in range(1000):
            await stream.on_quote(_make_quote("kalshi", f"CANON-{i}"))
            await stream.on_quote(_make_quote("polymarket", f"CANON-{i}"))
        elapsed = time.monotonic() - start

        assert elapsed < 1.0, f"1000 canonicals took {elapsed:.3f}s — should be well under 1s"
        assert queue.qsize() == 1000, f"Expected 1000 pairs, got {queue.qsize()}"

    asyncio.run(runner())


def test_quote_update_refreshes_match():
    """Feed kalshi1+poly1 (pair1); then kalshi2 for same canonical (pair2 with new kalshi quote)."""

    async def runner():
        queue: asyncio.Queue = asyncio.Queue()
        stream = MatchedPairStream(output_queue=queue)

        ts1 = time.time()
        await stream.on_quote(_make_quote("kalshi", "CANON-X", ts=ts1))
        await stream.on_quote(_make_quote("polymarket", "CANON-X", ts=ts1))

        assert queue.qsize() == 1
        pair1: MatchedPair = queue.get_nowait()
        assert pair1.kalshi_quote.timestamp == ts1

        # Update kalshi with a newer quote
        ts2 = ts1 + 1.0
        await stream.on_quote(_make_quote("kalshi", "CANON-X", ts=ts2))

        assert queue.qsize() == 1, "Second kalshi update + existing poly should emit again"
        pair2: MatchedPair = queue.get_nowait()
        assert pair2.kalshi_quote.timestamp == ts2, "Pair2 should have the newer kalshi quote"
        assert pair2.poly_quote.timestamp == ts1, "Pair2 should retain the existing poly quote"

    asyncio.run(runner())
