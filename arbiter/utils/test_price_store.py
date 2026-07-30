

def test_dead_market_entries_are_pruned():
    """Delisted markets must not pin memory forever: _mem entries and
    _history keys whose newest data is older than the prune horizon are
    evicted by the periodic sweep inside put() (leak audit 2026-07-30:
    every canonical_id ever quoted was retained for process lifetime)."""
    import asyncio
    import time as _time

    from arbiter.utils.price_store import PricePoint, PriceStore

    async def runner():
        store = PriceStore(ttl=10)
        old_ts = _time.time() - 100000
        await store.put(PricePoint(
            platform="kalshi", canonical_id="DEAD_MARKET",
            yes_price=0.5, no_price=0.5, yes_volume=1.0, no_volume=1.0,
            timestamp=old_ts, raw_market_id="dead-1",
        ))
        # Backdate the history entry too (put() stamps whatever the
        # PricePoint carried, which is already old here).
        store._prune_counter = store.PRUNE_EVERY - 1
        await store.put(PricePoint(
            platform="kalshi", canonical_id="LIVE_MARKET",
            yes_price=0.6, no_price=0.4, yes_volume=1.0, no_volume=1.0,
            timestamp=_time.time(), raw_market_id="live-1",
        ))
        return store

    store = asyncio.run(runner())
    assert "price:kalshi:DEAD_MARKET" not in store._mem
    assert "DEAD_MARKET" not in store._history
    assert "price:kalshi:LIVE_MARKET" in store._mem
    assert "LIVE_MARKET" in store._history
