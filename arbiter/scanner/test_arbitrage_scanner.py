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
