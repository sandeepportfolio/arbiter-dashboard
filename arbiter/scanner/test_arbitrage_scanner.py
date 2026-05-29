import asyncio
import time

from arbiter.config.settings import MARKET_MAP, ScannerConfig
from arbiter.scanner.arbitrage import ArbitrageScanner
from arbiter.utils.price_store import PricePoint, PriceStore


def test_scanner_requires_persistence_before_publish():
    async def runner():
        store = PriceStore(ttl=60)
        scanner = ArbitrageScanner(
            ScannerConfig(
                min_edge_cents=1.0,
                persistence_scans=3,
                max_position_usd=100.0,
                confidence_threshold=0.1,
                min_liquidity=10.0,
            ),
            store,
        )
        queue = scanner.subscribe()

        original_mapping = MARKET_MAP.get("TEST_AUTO")
        MARKET_MAP["TEST_AUTO"] = {
            "description": "Scanner persistence test market",
            "status": "confirmed",
            "allow_auto_trade": True,
            "mapping_score": 0.9,
            "resolution_match_status": "identical",
        }

        now = time.time()
        await store.put(
            PricePoint(
                platform="kalshi",
                canonical_id="TEST_AUTO",
                yes_price=0.40,
                no_price=0.60,
                yes_ask=0.40,
                no_ask=0.60,
                yes_volume=100,
                no_volume=100,
                timestamp=now,
                raw_market_id="K-TEST",
                yes_market_id="K-TEST",
                no_market_id="K-TEST",
                fee_rate=0.07,
                mapping_status="confirmed",
                mapping_score=0.9,
            )
        )
        await store.put(
            PricePoint(
                platform="polymarket",
                canonical_id="TEST_AUTO",
                yes_price=0.48,
                no_price=0.45,
                yes_ask=0.48,
                no_ask=0.45,
                yes_volume=150,
                no_volume=150,
                timestamp=now,
                raw_market_id="P-YES",
                yes_market_id="P-YES",
                no_market_id="P-NO",
                fee_rate=0.01,
                mapping_status="confirmed",
                mapping_score=0.9,
            )
        )

        try:
            first = await scanner.scan_once()
            second = await scanner.scan_once()
            third = await scanner.scan_once()

            assert first[0].status == "candidate"
            assert second[0].status == "candidate"
            assert third[0].status == "tradable"

            published = await asyncio.wait_for(queue.get(), timeout=0.2)
            assert published.canonical_id == "TEST_AUTO"
            assert published.status == "tradable"
        finally:
            if original_mapping is None:
                MARKET_MAP.pop("TEST_AUTO", None)
            else:
                MARKET_MAP["TEST_AUTO"] = original_mapping

    asyncio.run(runner())


def test_scanner_uses_fee_aware_polymarket_math():
    async def runner():
        store = PriceStore(ttl=60)
        scanner = ArbitrageScanner(
            ScannerConfig(
                min_edge_cents=1.0,
                persistence_scans=1,
                max_position_usd=100.0,
                confidence_threshold=0.1,
                min_liquidity=10.0,
            ),
            store,
        )

        original_mapping = MARKET_MAP.get("TEST_FEES")
        MARKET_MAP["TEST_FEES"] = {
            "description": "Scanner fee model test market",
            "status": "confirmed",
            "allow_auto_trade": True,
            "mapping_score": 0.95,
            "resolution_match_status": "identical",
        }

        now = time.time()
        await store.put(
            PricePoint(
                platform="kalshi",
                canonical_id="TEST_FEES",
                yes_price=0.40,
                no_price=0.60,
                yes_ask=0.40,
                no_ask=0.60,
                yes_volume=300,
                no_volume=300,
                timestamp=now,
                raw_market_id="K-FEES",
                yes_market_id="K-FEES",
                no_market_id="K-FEES",
                fee_rate=0.07,
                mapping_status="confirmed",
                mapping_score=0.95,
            )
        )
        await store.put(
            PricePoint(
                platform="polymarket",
                canonical_id="TEST_FEES",
                yes_price=0.52,
                no_price=0.55,
                yes_ask=0.52,
                no_ask=0.55,
                yes_volume=300,
                no_volume=300,
                timestamp=now,
                raw_market_id="P-FEES",
                yes_market_id="P-FEES-YES",
                no_market_id="P-FEES-NO",
                fee_rate=0.01,
                mapping_status="confirmed",
                mapping_score=0.95,
            )
        )

        try:
            opportunities = await scanner.scan_once()
            assert len(opportunities) == 1
            opportunity = opportunities[0]
            assert opportunity.status == "tradable"
            assert round(opportunity.no_fee, 4) == 0.0025
            assert opportunity.no_fee_rate == 0.01
            assert opportunity.yes_fee_rate == 0.07
            assert opportunity.net_edge_cents > 3.0
        finally:
            if original_mapping is None:
                MARKET_MAP.pop("TEST_FEES", None)
            else:
                MARKET_MAP["TEST_FEES"] = original_mapping

    asyncio.run(runner())


def test_scanner_rejects_wide_bid_ask_spread_on_primary_venue():
    """Reject opportunities where the venue we'd buy on has a bid-ask spread
    too wide to recover from a one-leg failure.

    Real incident (2026-05-08, DEM_HOUSE_2026): kalshi YES quoted bid=$0.27 /
    ask=$0.74 (47¢ spread). The arbitrage looked profitable on ASK/ASK math
    but every secondary-venue rejection forced an unwind at the YES BID,
    locking in -$4.70 to -$4.90 per trade for 22 consecutive trades.
    """
    async def runner():
        store = PriceStore(ttl=60)
        scanner = ArbitrageScanner(
            ScannerConfig(
                min_edge_cents=1.0,
                persistence_scans=1,
                max_position_usd=100.0,
                confidence_threshold=0.1,
                min_liquidity=10.0,
                max_bid_ask_spread_cents=15.0,
            ),
            store,
        )

        original_mapping = MARKET_MAP.get("TEST_WIDE_SPREAD")
        MARKET_MAP["TEST_WIDE_SPREAD"] = {
            "description": "Wide-spread market that should be rejected",
            "status": "confirmed",
            "allow_auto_trade": True,
            "mapping_score": 0.95,
            "resolution_match_status": "identical",
        }

        now = time.time()
        # Kalshi: YES bid $0.27 / ask $0.74 — 47¢ spread (the DEM_HOUSE_2026 pattern)
        await store.put(
            PricePoint(
                platform="kalshi",
                canonical_id="TEST_WIDE_SPREAD",
                yes_price=0.74,
                no_price=0.20,
                yes_bid=0.27,
                yes_ask=0.74,
                no_bid=0.20,
                no_ask=0.30,
                yes_volume=300,
                no_volume=300,
                timestamp=now,
                raw_market_id="K-WIDE",
                yes_market_id="K-WIDE",
                no_market_id="K-WIDE",
                fee_rate=0.07,
                mapping_status="confirmed",
                mapping_score=0.95,
            )
        )
        await store.put(
            PricePoint(
                platform="polymarket",
                canonical_id="TEST_WIDE_SPREAD",
                yes_price=0.78,
                no_price=0.20,
                yes_bid=0.77,
                yes_ask=0.78,
                no_bid=0.19,
                no_ask=0.20,
                yes_volume=300,
                no_volume=300,
                timestamp=now,
                raw_market_id="P-WIDE",
                yes_market_id="P-WIDE-YES",
                no_market_id="P-WIDE-NO",
                fee_rate=0.04,
                mapping_status="confirmed",
                mapping_score=0.95,
            )
        )

        try:
            opportunities = await scanner.scan_once()
            assert opportunities == [], (
                "Wide kalshi YES spread (27¢/74¢) must be rejected; "
                f"got {len(opportunities)} opportunities"
            )
        finally:
            if original_mapping is None:
                MARKET_MAP.pop("TEST_WIDE_SPREAD", None)
            else:
                MARKET_MAP["TEST_WIDE_SPREAD"] = original_mapping

    asyncio.run(runner())


def test_scanner_accepts_tight_bid_ask_spread_on_primary_venue():
    """Sanity-check the spread guard doesn't reject normal liquid markets."""
    async def runner():
        store = PriceStore(ttl=60)
        scanner = ArbitrageScanner(
            ScannerConfig(
                min_edge_cents=1.0,
                persistence_scans=1,
                max_position_usd=100.0,
                confidence_threshold=0.1,
                min_liquidity=10.0,
                max_bid_ask_spread_cents=15.0,
            ),
            store,
        )

        original_mapping = MARKET_MAP.get("TEST_TIGHT_SPREAD")
        MARKET_MAP["TEST_TIGHT_SPREAD"] = {
            "description": "Tight-spread liquid market",
            "status": "confirmed",
            "allow_auto_trade": True,
            "mapping_score": 0.95,
            "resolution_match_status": "identical",
        }

        now = time.time()
        await store.put(
            PricePoint(
                platform="kalshi",
                canonical_id="TEST_TIGHT_SPREAD",
                yes_price=0.40,
                no_price=0.60,
                yes_bid=0.39,
                yes_ask=0.40,
                no_bid=0.59,
                no_ask=0.60,
                yes_volume=300,
                no_volume=300,
                timestamp=now,
                raw_market_id="K-TIGHT",
                yes_market_id="K-TIGHT",
                no_market_id="K-TIGHT",
                fee_rate=0.07,
                mapping_status="confirmed",
                mapping_score=0.95,
            )
        )
        await store.put(
            PricePoint(
                platform="polymarket",
                canonical_id="TEST_TIGHT_SPREAD",
                yes_price=0.52,
                no_price=0.55,
                yes_bid=0.51,
                yes_ask=0.52,
                no_bid=0.54,
                no_ask=0.55,
                yes_volume=300,
                no_volume=300,
                timestamp=now,
                raw_market_id="P-TIGHT",
                yes_market_id="P-TIGHT-YES",
                no_market_id="P-TIGHT-NO",
                fee_rate=0.04,
                mapping_status="confirmed",
                mapping_score=0.95,
            )
        )

        try:
            opportunities = await scanner.scan_once()
            assert len(opportunities) == 1
            assert opportunities[0].canonical_id == "TEST_TIGHT_SPREAD"
        finally:
            if original_mapping is None:
                MARKET_MAP.pop("TEST_TIGHT_SPREAD", None)
            else:
                MARKET_MAP["TEST_TIGHT_SPREAD"] = original_mapping

    asyncio.run(runner())


def test_scanner_per_pair_liquidity_floor_unlocks_fx_arb_with_zero_size():
    """PROFITABILITY-02: an FX-containing pair with min_available_
    liquidity=0 (because IBKR's Client Portal doesn't broadcast size
    on FORECASTX binaries) must still reach tradable when the
    per-pair liquidity floor is 1. Without this, the 4.35-4.86¢
    FX arbs found 2026-05-27 are perpetually status=illiquid.
    """
    async def runner():
        store = PriceStore(ttl=300)
        scanner = ArbitrageScanner(
            ScannerConfig(
                min_edge_cents=4.0,            # relaxed for the test
                persistence_scans=1,
                max_position_usd=100.0,
                confidence_threshold=0.0,
                min_liquidity=25.0,            # global gate, FX should bypass
                max_bid_ask_spread_cents=0.0,
                # FX pair entry permits trades with reported volume=0.
                min_liquidity_by_pair={
                    "forecastex_kalshi": 0.0,
                    "forecastex_polymarket": 0.0,
                    "kalshi_polymarket": 25.0,
                },
            ),
            store,
        )
        original_mapping = MARKET_MAP.get("FX_LIQ_TEST")
        MARKET_MAP["FX_LIQ_TEST"] = {
            "description": "FX zero-size liquidity test",
            "status": "confirmed",
            "allow_auto_trade": True,
            "mapping_score": 0.95,
            "resolution_match_status": "identical",
        }
        fresh = time.time()
        # Kalshi side reports 200 volume; FX side reports 0 (IBKR feed)
        await store.put(
            PricePoint(
                platform="kalshi", canonical_id="FX_LIQ_TEST",
                yes_price=0.46, no_price=0.55,
                yes_volume=200, no_volume=200, timestamp=fresh,
                raw_market_id="K-FXLIQ", yes_market_id="K-FXLIQ", no_market_id="K-FXLIQ",
                fee_rate=0.07,
                mapping_status="confirmed", mapping_score=0.9,
                yes_bid=0.455, yes_ask=0.46, no_bid=0.545, no_ask=0.55,
            )
        )
        await store.put(
            PricePoint(
                platform="forecastex", canonical_id="FX_LIQ_TEST",
                yes_price=0.53, no_price=0.47,
                yes_volume=0, no_volume=0,     # ← IBKR reports zero size
                timestamp=fresh,
                raw_market_id="FX-FXLIQ", yes_market_id="FX-FXLIQ", no_market_id="FX-FXLIQ",
                fee_rate=0.005,
                mapping_status="confirmed", mapping_score=0.9,
                yes_bid=0.525, yes_ask=0.53, no_bid=0.465, no_ask=0.47,
            )
        )
        try:
            opps = await scanner.scan_once()
            fx_arbs = [
                o for o in opps
                if "forecastex" in (o.yes_platform, o.no_platform)
            ]
            assert fx_arbs, "K×FX arb must surface even when FX volume=0"
            tradable = [o for o in fx_arbs if o.status == "tradable"]
            assert tradable, (
                f"FX arb should reach tradable status under pair floor=1; "
                f"got {[(o.net_edge_cents, o.status, o.min_available_liquidity) for o in fx_arbs]}"
            )
        finally:
            if original_mapping is None:
                MARKET_MAP.pop("FX_LIQ_TEST", None)
            else:
                MARKET_MAP["FX_LIQ_TEST"] = original_mapping

    asyncio.run(runner())


def test_scanner_per_pair_liquidity_floor_still_blocks_kp_with_zero_size():
    """PROFITABILITY-02 relax-only: a K×P pair with zero volume must
    remain illiquid even when FX pair floors are set to 0.

    The FX-pair liquidity exemption is pair-specific; the K×P 25-contract
    floor must be unaffected. Regression guard: wrong key lookup or a
    'min of all pairs' bug would let zero-volume K×P slip through.
    """
    async def runner():
        store = PriceStore(ttl=300)
        scanner = ArbitrageScanner(
            ScannerConfig(
                min_edge_cents=4.0,
                persistence_scans=1,
                max_position_usd=100.0,
                confidence_threshold=0.0,
                min_liquidity=25.0,
                max_bid_ask_spread_cents=0.0,
                min_liquidity_by_pair={
                    "forecastex_kalshi": 0.0,
                    "forecastex_polymarket": 0.0,
                    "kalshi_polymarket": 25.0,   # K×P keeps global floor
                },
            ),
            store,
        )
        original_mapping = MARKET_MAP.get("KP_LIQ_BLOCK_TEST")
        MARKET_MAP["KP_LIQ_BLOCK_TEST"] = {
            "description": "K×P zero-volume must stay illiquid",
            "status": "confirmed",
            "allow_auto_trade": True,
            "mapping_score": 0.95,
            "resolution_match_status": "identical",
        }
        fresh = time.time()
        # Kalshi YES: 200 volume. Polymarket NO: 10 contracts — enough for
        # the 5-share Polymarket minimum so the opp surfaces, but well below
        # the 25-contract K×P liquidity floor so it stays illiquid.
        # min_available_liquidity = min(200, 10) = 10 < 25 → illiquid.
        await store.put(
            PricePoint(
                platform="kalshi", canonical_id="KP_LIQ_BLOCK_TEST",
                yes_price=0.46, no_price=0.55,
                yes_volume=200, no_volume=200, timestamp=fresh,
                raw_market_id="K-KPLB", yes_market_id="K-KPLB", no_market_id="K-KPLB",
                fee_rate=0.07,
                mapping_status="confirmed", mapping_score=0.9,
                yes_bid=0.455, yes_ask=0.46, no_bid=0.545, no_ask=0.55,
            )
        )
        await store.put(
            PricePoint(
                platform="polymarket", canonical_id="KP_LIQ_BLOCK_TEST",
                yes_price=0.52, no_price=0.47,
                yes_volume=10, no_volume=10, timestamp=fresh,
                raw_market_id="P-KPLB", yes_market_id="P-KPLB", no_market_id="P-KPLB",
                fee_rate=0.01,
                mapping_status="confirmed", mapping_score=0.9,
                yes_bid=0.515, yes_ask=0.52, no_bid=0.465, no_ask=0.47,
            )
        )
        try:
            opps = await scanner.scan_once()
            kp_opps = [
                o for o in opps
                if set([o.yes_platform, o.no_platform]) == {"kalshi", "polymarket"}
            ]
            tradable = [o for o in kp_opps if o.status == "tradable"]
            assert not tradable, (
                f"K×P with zero volume must NOT be tradable even with FX pair floor=0; "
                f"got {[(o.net_edge_cents, o.status, o.min_available_liquidity) for o in kp_opps]}"
            )
            # Should be present but illiquid — confirm the arb surfaces but is gated.
            illiquid = [o for o in kp_opps if o.status == "illiquid"]
            assert illiquid, (
                f"K×P zero-volume arb must appear as illiquid; "
                f"got {[(o.net_edge_cents, o.status) for o in kp_opps]}"
            )
        finally:
            if original_mapping is None:
                MARKET_MAP.pop("KP_LIQ_BLOCK_TEST", None)
            else:
                MARKET_MAP["KP_LIQ_BLOCK_TEST"] = original_mapping

    asyncio.run(runner())


def test_scanner_per_pair_edge_floor_unlocks_fx_arb_below_global_floor():
    """PROFITABILITY-01: a 5¢ net edge on a Kalshi×ForecastEx pair
    must clear the FX-pair floor (4.5¢) even when the global floor
    is 7¢. Reproduces the 2026-05-27 live finding: DEM_SENATE_2026
    K_YES + FX_NO at 7c gross / ~5c net was being thrown away.
    """
    async def runner():
        store = PriceStore(ttl=300)
        scanner = ArbitrageScanner(
            ScannerConfig(
                min_edge_cents=7.0,            # global K×P floor
                persistence_scans=1,
                max_position_usd=100.0,
                confidence_threshold=0.0,
                min_liquidity=10.0,
                max_bid_ask_spread_cents=0.0,
            ),
            store,
        )
        original_mapping = MARKET_MAP.get("FX_KP_PROFIT_TEST")
        MARKET_MAP["FX_KP_PROFIT_TEST"] = {
            "description": "K×FX cross-venue pair",
            "status": "confirmed",
            "allow_auto_trade": True,
            "mapping_score": 0.95,
            "resolution_match_status": "identical",
        }
        # Kalshi YES 0.46, ForecastEx NO 0.47 → 0.93 cost/pair = 7¢ gross.
        # After fees (~1.5¢ K + 0.5¢ FX = 2¢) → ~5¢ net edge.
        # 5¢ < global 7¢ floor but ≥ 4.5¢ FX-K pair floor → must trade.
        fresh = time.time()
        await store.put(
            PricePoint(
                platform="kalshi", canonical_id="FX_KP_PROFIT_TEST",
                yes_price=0.46, no_price=0.55,
                yes_volume=200, no_volume=200, timestamp=fresh,
                raw_market_id="K-FXKP", yes_market_id="K-FXKP", no_market_id="K-FXKP",
                fee_rate=0.07,
                mapping_status="confirmed", mapping_score=0.9,
                yes_bid=0.455, yes_ask=0.46, no_bid=0.545, no_ask=0.55,
            )
        )
        await store.put(
            PricePoint(
                platform="forecastex", canonical_id="FX_KP_PROFIT_TEST",
                yes_price=0.53, no_price=0.47,
                yes_volume=200, no_volume=200, timestamp=fresh,
                raw_market_id="FX-FXKP", yes_market_id="FX-FXKP", no_market_id="FX-FXKP",
                fee_rate=0.005,  # FX flat 0.5c/contract
                mapping_status="confirmed", mapping_score=0.9,
                yes_bid=0.525, yes_ask=0.53, no_bid=0.465, no_ask=0.47,
            )
        )
        try:
            opps = await scanner.scan_once()
            fx_arbs = [
                o for o in opps
                if "forecastex" in (o.yes_platform, o.no_platform)
            ]
            assert fx_arbs, (
                f"K×FX arb must clear pair floor; got {[(o.yes_platform, o.no_platform, o.net_edge_cents, o.status) for o in opps]}"
            )
            # Must be tradable (status='tradable') — net edge ≥ pair floor.
            assert any(o.status == "tradable" for o in fx_arbs), (
                f"FX arb should reach tradable status; got {[(o.net_edge_cents, o.status) for o in fx_arbs]}"
            )
        finally:
            if original_mapping is None:
                MARKET_MAP.pop("FX_KP_PROFIT_TEST", None)
            else:
                MARKET_MAP["FX_KP_PROFIT_TEST"] = original_mapping

    asyncio.run(runner())


def test_scanner_per_pair_edge_floor_still_blocks_kp_below_kp_floor():
    """Sanity: a K×P pair at 5¢ net edge MUST still be blocked
    because the K×P floor (7¢) is still the high-fee threshold —
    only FX-containing pairs get the looser floor."""
    async def runner():
        store = PriceStore(ttl=300)
        scanner = ArbitrageScanner(
            ScannerConfig(
                min_edge_cents=7.0,
                persistence_scans=1,
                max_position_usd=100.0,
                confidence_threshold=0.0,
                min_liquidity=10.0,
                max_bid_ask_spread_cents=0.0,
            ),
            store,
        )
        original_mapping = MARKET_MAP.get("KP_PROFIT_TEST")
        MARKET_MAP["KP_PROFIT_TEST"] = {
            "description": "K×P cross-venue pair",
            "status": "confirmed",
            "allow_auto_trade": True,
            "mapping_score": 0.95,
            "resolution_match_status": "identical",
        }
        fresh = time.time()
        # Sub-floor edge: 0.46 + 0.48 = 0.94 cost = 6¢ gross, ~4¢ net.
        await store.put(
            PricePoint(
                platform="kalshi", canonical_id="KP_PROFIT_TEST",
                yes_price=0.46, no_price=0.55,
                yes_volume=200, no_volume=200, timestamp=fresh,
                raw_market_id="K-KP", yes_market_id="K-KP", no_market_id="K-KP",
                fee_rate=0.07,
                mapping_status="confirmed", mapping_score=0.9,
                yes_bid=0.455, yes_ask=0.46, no_bid=0.545, no_ask=0.55,
            )
        )
        await store.put(
            PricePoint(
                platform="polymarket", canonical_id="KP_PROFIT_TEST",
                yes_price=0.52, no_price=0.48,
                yes_volume=200, no_volume=200, timestamp=fresh,
                raw_market_id="P-KP", yes_market_id="P-KP", no_market_id="P-KP",
                fee_rate=0.01,
                mapping_status="confirmed", mapping_score=0.9,
                yes_bid=0.515, yes_ask=0.52, no_bid=0.475, no_ask=0.48,
            )
        )
        try:
            opps = await scanner.scan_once()
            # K×P pair at sub-7c net edge → no tradable opps.
            tradable_kp = [
                o for o in opps
                if o.status == "tradable"
                and set([o.yes_platform, o.no_platform]) == {"kalshi", "polymarket"}
            ]
            assert not tradable_kp, (
                f"K×P below 7c floor must NOT be tradable; got {[(o.net_edge_cents, o.status) for o in tradable_kp]}"
            )
        finally:
            if original_mapping is None:
                MARKET_MAP.pop("KP_PROFIT_TEST", None)
            else:
                MARKET_MAP["KP_PROFIT_TEST"] = original_mapping

    asyncio.run(runner())


def test_scanner_phantom_edge_filter_blocks_stale_huge_edge():
    """Edges ≥ phantom threshold with stale quotes never reach publish.

    Reproduces the 85¢/36¢/28¢ phantom failures observed live 2026-05-26:
    one side hasn't updated since resolution news moved the other 80c+,
    creating a phantom 85¢ "arb" that disappears the moment the stale
    side ticks. The publisher must not surface these.
    """
    async def runner():
        store = PriceStore(ttl=300)
        scanner = ArbitrageScanner(
            ScannerConfig(
                min_edge_cents=1.0,
                persistence_scans=1,
                max_position_usd=100.0,
                confidence_threshold=0.0,
                min_liquidity=10.0,
                max_bid_ask_spread_cents=0.0,  # disable spread gate
                phantom_edge_threshold_cents=40.0,
                phantom_edge_max_age_seconds=5.0,
            ),
            store,
        )
        original_mapping = MARKET_MAP.get("PHANTOM_TEST")
        MARKET_MAP["PHANTOM_TEST"] = {
            "description": "Phantom-edge filter test market",
            "status": "confirmed",
            "allow_auto_trade": True,
            "mapping_score": 0.95,
            "resolution_match_status": "identical",
        }
        # 90¢ phantom: Polymarket quote is 30s stale — still showing
        # the pre-resolution-news prices that disagree wildly with the
        # now-resolved-by-news Kalshi side. Each venue's yes+no still
        # ≈ $1.00 so the binary-sanity guard doesn't fire; only the
        # cross-venue spread is the giveaway.
        old_ts = time.time() - 30.0
        fresh_ts = time.time()
        await store.put(
            PricePoint(
                platform="kalshi",
                canonical_id="PHANTOM_TEST",
                yes_price=0.95, no_price=0.05,    # fresh: resolution likely YES
                yes_volume=100, no_volume=100,
                timestamp=fresh_ts,
                raw_market_id="K-PH",
                yes_market_id="K-PH", no_market_id="K-PH",
                fee_rate=0.07,
                mapping_status="confirmed", mapping_score=0.9,
                yes_bid=0.94, yes_ask=0.96,
                no_bid=0.04, no_ask=0.06,
            )
        )
        await store.put(
            PricePoint(
                platform="polymarket",
                canonical_id="PHANTOM_TEST",
                yes_price=0.05, no_price=0.95,    # stale: opposite to Kalshi
                yes_volume=100, no_volume=100,
                timestamp=old_ts,
                raw_market_id="P-PH",
                yes_market_id="P-YES", no_market_id="P-NO",
                fee_rate=0.01,
                mapping_status="confirmed", mapping_score=0.9,
                yes_bid=0.04, yes_ask=0.06,
                no_bid=0.94, no_ask=0.96,
            )
        )
        try:
            opps = await scanner.scan_once()
            # Find the 85¢ side
            big = [o for o in opps if o.net_edge_cents >= 40.0]
            assert big, f"expected a >=40c edge among opps: {[(o.net_edge_cents, o.status) for o in opps]}"
            # Every >= phantom-threshold opp must be blocked from publish.
            for o in big:
                assert o.status != "tradable", (
                    f"phantom edge {o.net_edge_cents:.1f}¢ should not be tradable"
                )
        finally:
            if original_mapping is None:
                MARKET_MAP.pop("PHANTOM_TEST", None)
            else:
                MARKET_MAP["PHANTOM_TEST"] = original_mapping

    asyncio.run(runner())


def test_scanner_phantom_edge_filter_allows_huge_edge_with_fresh_quotes():
    """A 50¢ edge where BOTH sides are fresh-from-book stays tradable.

    The filter must only catch stale-quote phantoms, not legitimate
    deep dislocations that briefly happen on event resolution.
    """
    async def runner():
        store = PriceStore(ttl=300)
        scanner = ArbitrageScanner(
            ScannerConfig(
                min_edge_cents=1.0,
                persistence_scans=1,
                max_position_usd=100.0,
                confidence_threshold=0.0,
                min_liquidity=10.0,
                max_bid_ask_spread_cents=0.0,
                phantom_edge_threshold_cents=40.0,
                phantom_edge_max_age_seconds=5.0,
            ),
            store,
        )
        original_mapping = MARKET_MAP.get("PHANTOM_FRESH")
        MARKET_MAP["PHANTOM_FRESH"] = {
            "description": "Phantom-edge filter — fresh quotes pass",
            "status": "confirmed",
            "allow_auto_trade": True,
            "mapping_score": 0.95,
            "resolution_match_status": "identical",
        }
        # Both venues each have yes+no ≈ $1 (binary-sane), but cross-
        # venue dislocation is 80¢: buy YES on Kalshi at 0.10, buy NO
        # on Poly at 0.10 → cost $0.20 → edge 80¢.
        fresh_ts = time.time()
        await store.put(
            PricePoint(
                platform="kalshi", canonical_id="PHANTOM_FRESH",
                yes_price=0.10, no_price=0.90,
                yes_volume=100, no_volume=100, timestamp=fresh_ts,
                raw_market_id="K-PHF", yes_market_id="K-PHF", no_market_id="K-PHF",
                fee_rate=0.07,
                mapping_status="confirmed", mapping_score=0.9,
                yes_bid=0.09, yes_ask=0.11, no_bid=0.89, no_ask=0.91,
            )
        )
        await store.put(
            PricePoint(
                platform="polymarket", canonical_id="PHANTOM_FRESH",
                yes_price=0.90, no_price=0.10,
                yes_volume=100, no_volume=100, timestamp=fresh_ts,
                raw_market_id="P-PHF", yes_market_id="P-YES", no_market_id="P-NO",
                fee_rate=0.01,
                mapping_status="confirmed", mapping_score=0.9,
                yes_bid=0.89, yes_ask=0.91, no_bid=0.09, no_ask=0.11,
            )
        )
        try:
            opps = await scanner.scan_once()
            big = [o for o in opps if o.net_edge_cents >= 40.0]
            assert big, "expected a >=40c edge"
            assert any(o.status == "tradable" for o in big), (
                f"fresh-quote >=40c edge should reach tradable, got "
                f"{[(o.net_edge_cents, o.status, o.yes_quote_age_seconds, o.no_quote_age_seconds) for o in big]}"
            )
        finally:
            if original_mapping is None:
                MARKET_MAP.pop("PHANTOM_FRESH", None)
            else:
                MARKET_MAP["PHANTOM_FRESH"] = original_mapping

    asyncio.run(runner())


# ────────────────────────────────────────────────────────────────────────
# Position-sizing tests for _compute_position_size
# Bet sizing audit (2026-05-14): added fraction-of-balance cap, $-reserve
# floor, and edge-based scaling. These tests pin the math.
# ────────────────────────────────────────────────────────────────────────

def _sizing_price_point(platform: str, yes_price: float, no_price: float,
                        yes_volume: float = 10_000, no_volume: float = 10_000):
    """Build a PricePoint that won't bottleneck sizing on liquidity."""
    now = time.time()
    return PricePoint(
        platform=platform,
        canonical_id="SIZING",
        yes_price=yes_price,
        no_price=no_price,
        yes_ask=yes_price,
        no_ask=no_price,
        yes_volume=yes_volume,
        no_volume=no_volume,
        timestamp=now,
        raw_market_id=f"{platform}-SIZING",
        yes_market_id=f"{platform}-SIZING",
        no_market_id=f"{platform}-SIZING",
        fee_rate=0.0,
        mapping_status="confirmed",
        mapping_score=0.95,
    )


def test_compute_position_size_backward_compatible_with_no_balance_provider():
    """With no balance_provider, only the MAX_POSITION_USD + liquidity caps fire."""
    scanner = ArbitrageScanner(
        ScannerConfig(max_position_usd=10.0),
        PriceStore(ttl=60),
    )
    yes_pp = _sizing_price_point("kalshi", 0.40, 0.60)
    no_pp = _sizing_price_point("polymarket", 0.40, 0.60)
    # cost_per_pair = 1.00. Expect floor(10/1.00) = 10
    qty = scanner._compute_position_size(yes_pp, no_pp, 0.40, 0.60)
    assert qty == 10


def test_compute_position_size_capped_by_balance_fraction():
    """5% of $200 = $10 -> 10 contracts at $1 cost/pair. Cap is the active gate."""
    scanner = ArbitrageScanner(
        ScannerConfig(
            max_position_usd=1000.0,  # large enough not to bind
            max_balance_fraction_per_trade=0.05,
        ),
        PriceStore(ttl=60),
        balance_provider=lambda: {"kalshi": 200.0, "polymarket": 200.0},
    )
    yes_pp = _sizing_price_point("kalshi", 0.40, 0.60)
    no_pp = _sizing_price_point("polymarket", 0.40, 0.60)
    qty = scanner._compute_position_size(yes_pp, no_pp, 0.40, 0.60)
    # cost_per_pair = $1.00, 5% of $200 = $10 -> qty = 10
    assert qty == 10


def test_compute_position_size_uses_smaller_balance_for_fraction():
    """When balances differ, the smaller balance bounds the fraction cap."""
    scanner = ArbitrageScanner(
        ScannerConfig(
            max_position_usd=1000.0,
            max_balance_fraction_per_trade=0.05,
        ),
        PriceStore(ttl=60),
        # Kalshi $354.63, Polymarket $212.08 (current live state)
        balance_provider=lambda: {"kalshi": 354.63, "polymarket": 212.08},
    )
    yes_pp = _sizing_price_point("kalshi", 0.40, 0.60)
    no_pp = _sizing_price_point("polymarket", 0.40, 0.60)
    qty = scanner._compute_position_size(yes_pp, no_pp, 0.40, 0.60)
    # smaller balance = $212.08; 5% = $10.60; cost/pair = $1.00 -> qty = 10
    assert qty == 10


def test_compute_position_size_reserves_min_platform_usd_floor():
    """A $200 floor on a $210 balance leaves $10 spendable per leg (less 10%)."""
    scanner = ArbitrageScanner(
        ScannerConfig(
            max_position_usd=1000.0,
            min_platform_reserve_usd=200.0,
        ),
        PriceStore(ttl=60),
        balance_provider=lambda: {"kalshi": 210.0, "polymarket": 210.0},
    )
    yes_pp = _sizing_price_point("kalshi", 0.40, 0.60)
    no_pp = _sizing_price_point("polymarket", 0.40, 0.60)
    qty = scanner._compute_position_size(yes_pp, no_pp, 0.40, 0.60)
    # After $200 reserve: $10 each; 90% spendable = $9; on NO leg @ $0.60
    # that's floor(9/0.60) = 15. On YES leg @ $0.40 that's floor(9/0.40) = 22.
    # min(yes_cap, no_cap) = 15.
    assert qty == 15


def test_compute_position_size_blocks_when_below_reserve_floor():
    """If balance is below the reserve floor, qty must be 0."""
    scanner = ArbitrageScanner(
        ScannerConfig(
            max_position_usd=1000.0,
            min_platform_reserve_usd=300.0,  # higher than balance
        ),
        PriceStore(ttl=60),
        balance_provider=lambda: {"kalshi": 200.0, "polymarket": 200.0},
    )
    yes_pp = _sizing_price_point("kalshi", 0.40, 0.60)
    no_pp = _sizing_price_point("polymarket", 0.40, 0.60)
    qty = scanner._compute_position_size(yes_pp, no_pp, 0.40, 0.60)
    assert qty == 0


def test_compute_position_size_edge_scaling_grows_size_with_edge():
    """At min_edge gate, qty = 50% of fraction cap; at ref_edge, qty = 100%."""
    cfg = ScannerConfig(
        min_edge_cents=3.0,
        max_position_usd=1000.0,
        max_balance_fraction_per_trade=0.10,  # 10% of $200 = $20 -> 20 pairs
        edge_scaling_enabled=True,
        edge_scaling_min_fraction=0.5,
        edge_scaling_ref_cents=10.0,
    )
    scanner = ArbitrageScanner(
        cfg,
        PriceStore(ttl=60),
        balance_provider=lambda: {"kalshi": 200.0, "polymarket": 200.0},
    )
    yes_pp = _sizing_price_point("kalshi", 0.40, 0.60)
    no_pp = _sizing_price_point("polymarket", 0.40, 0.60)
    qty_thin = scanner._compute_position_size(yes_pp, no_pp, 0.40, 0.60,
                                              net_edge_cents=3.0)
    qty_rich = scanner._compute_position_size(yes_pp, no_pp, 0.40, 0.60,
                                              net_edge_cents=10.0)
    # 50% of 20 = 10 contracts at min edge; 100% at reference edge
    assert qty_thin == 10
    assert qty_rich == 20


def test_compute_position_size_edge_scaling_off_keeps_full_fraction():
    """When edge scaling disabled, fraction cap applies in full regardless of edge."""
    cfg = ScannerConfig(
        max_position_usd=1000.0,
        max_balance_fraction_per_trade=0.10,
        edge_scaling_enabled=False,
    )
    scanner = ArbitrageScanner(
        cfg,
        PriceStore(ttl=60),
        balance_provider=lambda: {"kalshi": 200.0, "polymarket": 200.0},
    )
    yes_pp = _sizing_price_point("kalshi", 0.40, 0.60)
    no_pp = _sizing_price_point("polymarket", 0.40, 0.60)
    qty_thin = scanner._compute_position_size(yes_pp, no_pp, 0.40, 0.60,
                                              net_edge_cents=3.0)
    qty_rich = scanner._compute_position_size(yes_pp, no_pp, 0.40, 0.60,
                                              net_edge_cents=10.0)
    # Both pin to the full fraction cap (no scaling).
    assert qty_thin == qty_rich == 20


def test_compute_position_size_legacy_default_path_unchanged():
    """Defaults (fraction=1.0, reserve=0, edge scaling off) preserve legacy
    qty = floor(0.9 * min_bal / leg_price). With $200 balance @ $0.60 NO leg:
    0.9 * 200 / 0.60 = 300. With max_position_usd=10, USD cap dominates -> 10."""
    scanner = ArbitrageScanner(
        ScannerConfig(max_position_usd=10.0),  # default new fields
        PriceStore(ttl=60),
        balance_provider=lambda: {"kalshi": 200.0, "polymarket": 200.0},
    )
    yes_pp = _sizing_price_point("kalshi", 0.40, 0.60)
    no_pp = _sizing_price_point("polymarket", 0.40, 0.60)
    qty = scanner._compute_position_size(yes_pp, no_pp, 0.40, 0.60)
    # Legacy gate: USD cap = 10 / 1.0 = 10. Reserve fraction (10%) = 0.9*200/0.60=300.
    # Result: 10.
    assert qty == 10
