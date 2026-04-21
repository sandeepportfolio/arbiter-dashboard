"""
Scale test: 1000 canonical pairs × 3 updates/sec for 30 seconds.

Total events: 3000/sec × 30s = 90,000 events.

Assertions:
- p99 match-to-emit latency ≤ 100ms
- backpressure_drops rate < 0.1% of total events
- no exceptions

Marked @pytest.mark.slow — excluded from default pytest run.
Pass --run-slow to include.
"""
from __future__ import annotations

import asyncio
import statistics
import time
from typing import List

import pytest

from arbiter.scanner.matched_pair_stream import MatchedPair, MatchedPairStream
from arbiter.utils.price_store import PricePoint

_N_CANONICALS = 1000
_UPDATES_PER_SEC = 3          # per canonical per second (both platforms combined → 1.5/sec each)
_DURATION_SEC = 30
_INTER_UPDATE_SEC = 1.0 / _UPDATES_PER_SEC  # 333ms between updates per canonical
_TOTAL_EVENTS = _N_CANONICALS * _UPDATES_PER_SEC * _DURATION_SEC  # 90,000


def _make_quote(platform: str, canonical_id: str) -> PricePoint:
    now = time.time()
    return PricePoint(
        platform=platform,
        canonical_id=canonical_id,
        yes_price=0.45,
        no_price=0.55,
        yes_volume=200.0,
        no_volume=200.0,
        timestamp=now,
        raw_market_id=f"{platform}-{canonical_id}",
        yes_market_id=f"{platform}-{canonical_id}-yes",
        no_market_id=f"{platform}-{canonical_id}-no",
    )


@pytest.mark.slow
def test_scale_1000_pairs_3_updates_per_sec():
    """
    Drive MatchedPairStream with 1000 canonicals × 3 updates/sec for 30s.

    Each 'update' consists of sending one kalshi + one poly quote for a canonical
    (the 3 updates/sec budget means each canonical sees ~333ms between update rounds).
    Spawns 1000 emitter coroutines in parallel via asyncio.create_task.

    Measures latency from PricePoint.timestamp to MatchedPair.matched_at.
    """

    async def runner():
        queue: asyncio.Queue = asyncio.Queue(maxsize=10000)
        stream = MatchedPairStream(output_queue=queue)

        latencies: List[float] = []
        total_emitted = 0
        exception_count = 0

        # Consumer coroutine: drain the queue and record latencies
        async def consumer():
            nonlocal total_emitted
            while True:
                try:
                    pair: MatchedPair = await asyncio.wait_for(queue.get(), timeout=0.5)
                    # Latency = matched_at - quote timestamp (use the newer quote's ts)
                    quote_ts = max(pair.kalshi_quote.timestamp, pair.poly_quote.timestamp)
                    latency_ms = (pair.matched_at - quote_ts) * 1000.0
                    latencies.append(latency_ms)
                    total_emitted += 1
                except asyncio.TimeoutError:
                    break
                except asyncio.CancelledError:
                    break

        # Emitter coroutine: for a given canonical, send interleaved updates for duration
        async def emitter(canonical_id: str):
            nonlocal exception_count
            interval = _INTER_UPDATE_SEC
            end_time = time.monotonic() + _DURATION_SEC
            try:
                while time.monotonic() < end_time:
                    await stream.on_quote(_make_quote("kalshi", canonical_id))
                    await stream.on_quote(_make_quote("polymarket", canonical_id))
                    await asyncio.sleep(interval)
            except Exception:
                exception_count += 1

        # Seed all kalshi sides first so initial poly updates immediately match
        for i in range(_N_CANONICALS):
            await stream.on_quote(_make_quote("kalshi", f"SCALE-{i}"))

        # Drain seed results (no latency measurement on seed)
        while not queue.empty():
            queue.get_nowait()

        # Launch emitters + consumer
        consumer_task = asyncio.create_task(consumer())
        emitter_tasks = [
            asyncio.create_task(emitter(f"SCALE-{i}"))
            for i in range(_N_CANONICALS)
        ]

        # Wait for all emitters to finish
        await asyncio.gather(*emitter_tasks)

        # Allow consumer to drain remaining queue items
        await asyncio.sleep(1.0)
        consumer_task.cancel()
        try:
            await consumer_task
        except asyncio.CancelledError:
            pass

        # --- Assertions ---

        total_events = _N_CANONICALS * _UPDATES_PER_SEC * _DURATION_SEC

        assert exception_count == 0, f"{exception_count} exceptions occurred during scale test"

        assert len(latencies) > 0, "No latency measurements recorded — consumer never received pairs"

        p99_ms = statistics.quantiles(latencies, n=100)[98]  # 99th percentile

        backpressure_rate = stream.backpressure_drops / max(total_events, 1)

        assert p99_ms <= 100.0, (
            f"p99 match-to-emit latency {p99_ms:.2f}ms exceeds 100ms limit"
        )
        assert backpressure_rate < 0.001, (
            f"Backpressure drop rate {backpressure_rate:.4%} exceeds 0.1% limit "
            f"({stream.backpressure_drops} drops / {total_events} events)"
        )

        # Log stats for commit message
        p50_ms = statistics.median(latencies)
        print(
            f"\n[scale test] emitted={total_emitted} | "
            f"p50={p50_ms:.2f}ms p99={p99_ms:.2f}ms | "
            f"backpressure_drops={stream.backpressure_drops} "
            f"({backpressure_rate:.4%}) | "
            f"debounce_coalesced={stream.debounce_coalesced} | "
            f"throttle_drops={stream.throttle_drops}"
        )

    asyncio.run(runner())
