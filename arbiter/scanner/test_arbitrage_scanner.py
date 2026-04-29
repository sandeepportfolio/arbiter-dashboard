import asyncio
import os
import time

import pytest

from arbiter.config.settings import MARKET_MAP, ScannerConfig
from arbiter.scanner.arbitrage import ArbitrageScanner, _flipped_view
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


def test_flipped_view_swaps_sides():
    pp = PricePoint(
        platform="polymarket",
        canonical_id="X",
        yes_price=0.30,
        no_price=0.70,
        yes_volume=100,
        no_volume=200,
        timestamp=time.time(),
        raw_market_id="poly-x",
        yes_market_id="poly-x-yes",
        no_market_id="poly-x-no",
        yes_bid=0.29,
        yes_ask=0.31,
        no_bid=0.69,
        no_ask=0.71,
    )
    flipped = _flipped_view(pp)
    assert flipped.yes_price == 0.70 and flipped.no_price == 0.30
    assert flipped.yes_ask == 0.71 and flipped.no_ask == 0.31
    assert flipped.yes_market_id == "poly-x-no" and flipped.no_market_id == "poly-x-yes"
    # Original is unmodified
    assert pp.yes_price == 0.30


def test_scanner_polarity_flipped_inverts_polymarket_sides_and_holds_manual(monkeypatch):
    """Polarity-flipped pair: Polymarket YES corresponds to Kalshi NO.

    Setup mimics a flipped pair where Team A wins:
      - Kalshi YES (Team A wins) ask $0.20
      - Polymarket YES (Team B wins) ask $0.60
      - Polymarket NO (Team A wins) ask $0.40

    Without polarity handling the scanner would pair Kalshi YES + Polymarket
    NO (both pointing at Team A wins) and emit a fake $0.40 edge — a
    same-direction pair, not a hedge. With the flip applied, Polymarket's
    sides swap: the scanner pairs Kalshi YES + Polymarket-flipped-NO (which
    is the original YES contract) for a true $0.20 hedge edge.
    """
    async def runner():
        # Default: ENABLE_POLARITY_FLIPPED_AUTO_TRADE off → status is manual
        monkeypatch.delenv("ENABLE_POLARITY_FLIPPED_AUTO_TRADE", raising=False)
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

        original_mapping = MARKET_MAP.get("TEST_FLIP")
        MARKET_MAP["TEST_FLIP"] = {
            "description": "Polarity-flipped scanner test",
            "status": "confirmed",
            "allow_auto_trade": True,
            "mapping_score": 0.92,
            "resolution_match_status": "identical",
            "polarity_flipped": True,
        }

        now = time.time()
        await store.put(
            PricePoint(
                platform="kalshi",
                canonical_id="TEST_FLIP",
                yes_price=0.20,
                no_price=0.80,
                yes_volume=300,
                no_volume=300,
                timestamp=now,
                raw_market_id="K-FLIP",
                yes_market_id="K-FLIP",
                no_market_id="K-FLIP",
                yes_bid=0.19,
                yes_ask=0.20,
                no_bid=0.79,
                no_ask=0.80,
                fee_rate=0.07,
                mapping_status="confirmed",
                mapping_score=0.92,
            )
        )
        await store.put(
            PricePoint(
                platform="polymarket",
                canonical_id="TEST_FLIP",
                yes_price=0.60,   # Team B wins (= Kalshi NO direction)
                no_price=0.40,    # Team A wins (= Kalshi YES direction)
                yes_volume=300,
                no_volume=300,
                timestamp=now,
                raw_market_id="P-FLIP",
                yes_market_id="P-FLIP-YES",
                no_market_id="P-FLIP-NO",
                yes_bid=0.59,
                yes_ask=0.60,
                no_bid=0.39,
                no_ask=0.40,
                fee_rate=0.01,
                mapping_status="confirmed",
                mapping_score=0.92,
            )
        )

        try:
            opportunities = await scanner.scan_once()
            assert opportunities, "expected at least one opportunity for flipped pair"
            best = opportunities[0]
            # Manual hold by default — operator must enable auto-trade
            assert best.status == "manual", best.status
            # After the flip, the scanner pairs Kalshi YES ($0.20) with
            # Polymarket-flipped NO (originally the YES contract at $0.60)
            # for a real $0.20 hedge edge — NOT the fake $0.40 that an
            # unflipped pairing of Kalshi YES + Polymarket NO would show.
            assert 0.18 < best.gross_edge < 0.22, best.gross_edge
            # The "no leg" market id should be the original YES contract id
            # because that's what we'd actually buy on Polymarket.
            assert best.no_market_id == "P-FLIP-YES"
        finally:
            if original_mapping is None:
                MARKET_MAP.pop("TEST_FLIP", None)
            else:
                MARKET_MAP["TEST_FLIP"] = original_mapping

    asyncio.run(runner())


def test_scanner_polarity_flipped_promotes_to_tradable_when_env_enabled(monkeypatch):
    async def runner():
        monkeypatch.setenv("ENABLE_POLARITY_FLIPPED_AUTO_TRADE", "true")
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
        original_mapping = MARKET_MAP.get("TEST_FLIP_ENABLED")
        MARKET_MAP["TEST_FLIP_ENABLED"] = {
            "description": "Flipped + env-enabled",
            "status": "confirmed",
            "allow_auto_trade": True,
            "mapping_score": 0.92,
            "resolution_match_status": "identical",
            "polarity_flipped": True,
        }
        now = time.time()
        await store.put(PricePoint(
            platform="kalshi", canonical_id="TEST_FLIP_ENABLED",
            yes_price=0.20, no_price=0.80, yes_volume=300, no_volume=300,
            timestamp=now, raw_market_id="K", yes_market_id="K", no_market_id="K",
            yes_bid=0.19, yes_ask=0.20, no_bid=0.79, no_ask=0.80,
            fee_rate=0.07, mapping_status="confirmed", mapping_score=0.92,
        ))
        await store.put(PricePoint(
            platform="polymarket", canonical_id="TEST_FLIP_ENABLED",
            yes_price=0.60, no_price=0.40, yes_volume=300, no_volume=300,
            timestamp=now, raw_market_id="P", yes_market_id="P-Y", no_market_id="P-N",
            yes_bid=0.59, yes_ask=0.60, no_bid=0.39, no_ask=0.40,
            fee_rate=0.01, mapping_status="confirmed", mapping_score=0.92,
        ))
        try:
            opps = await scanner.scan_once()
            assert opps[0].status == "tradable"
        finally:
            if original_mapping is None:
                MARKET_MAP.pop("TEST_FLIP_ENABLED", None)
            else:
                MARKET_MAP["TEST_FLIP_ENABLED"] = original_mapping

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
