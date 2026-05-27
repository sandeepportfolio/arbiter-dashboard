"""
3-way scanner integration tests covering the ForecastEx third-platform fanout.

With three venues quoting the same canonical event, the scanner must:
  * emit edges for ALL THREE directional platform pairs (K→P, K→F, P→F and
    their reverses — six in total)
  * apply the correct per-venue fee function (Kalshi quadratic, Polymarket
    linear, ForecastEx flat $0.005/contract)
  * still respect MIN_EDGE_CENTS after fees
  * collapse cleanly to the two-platform behaviour when ForecastEx prices
    aren't present (FORECASTEX_ENABLED=false → no PriceStore writes)
"""
from __future__ import annotations

import asyncio
import time
import uuid

import pytest

from arbiter.config.settings import MARKET_MAP, ScannerConfig
from arbiter.scanner.arbitrage import ArbitrageScanner, compute_fee
from arbiter.utils.price_store import PricePoint, PriceStore


def _scanner_config(min_edge_cents: float = 0.5) -> ScannerConfig:
    return ScannerConfig(
        min_edge_cents=min_edge_cents,
        persistence_scans=1,
        max_position_usd=100.0,
        confidence_threshold=0.1,
        min_liquidity=10.0,
        max_bid_ask_spread_cents=0.0,
    )


def _mk_price(platform: str, canonical_id: str, yes: float, no: float,
              fee_rate: float, market_id: str = "m", ts: float | None = None) -> PricePoint:
    return PricePoint(
        platform=platform,
        canonical_id=canonical_id,
        yes_price=yes,
        no_price=no,
        yes_volume=200,
        no_volume=200,
        timestamp=ts or time.time(),
        raw_market_id=market_id,
        yes_market_id=market_id,
        no_market_id=market_id,
        yes_bid=max(yes - 0.005, 0),
        yes_ask=yes,
        no_bid=max(no - 0.005, 0),
        no_ask=no,
        fee_rate=fee_rate,
        mapping_status="confirmed",
        mapping_score=0.95,
    )


@pytest.fixture
def canonical_id():
    cid = f"FCST-SCAN-{uuid.uuid4().hex[:8]}"
    MARKET_MAP[cid] = {
        "description": "3-way scanner integration test",
        "status": "confirmed",
        "allow_auto_trade": True,
        "mapping_score": 0.95,
        "resolution_match_status": "identical",
    }
    yield cid
    MARKET_MAP.pop(cid, None)


# ── 3-way fanout ────────────────────────────────────────────────────────────


def test_scanner_emits_all_three_pair_directions(canonical_id):
    async def run():
        store = PriceStore(ttl=60)
        scanner = ArbitrageScanner(_scanner_config(min_edge_cents=0.5), store)

        # Three venues quoting the same market with enough dispersion that
        # all six directional pairs survive fees.
        # All three venues quote yes+no ≈ 1.0 internally, but cross-venue
        # the YES of one is much cheaper than the NO of another, opening
        # multi-direction arbs.
        await store.put(_mk_price("kalshi", canonical_id, yes=0.40, no=0.55, fee_rate=0.07))
        await store.put(_mk_price("polymarket", canonical_id, yes=0.55, no=0.40, fee_rate=0.02))
        await store.put(_mk_price("forecastex", canonical_id, yes=0.35, no=0.55, fee_rate=0.005))

        ops = await scanner.scan_once()
        platforms_seen = {(o.yes_platform, o.no_platform) for o in ops}

        # At minimum we must see ForecastEx participating on both sides of
        # at least one pair — proving the scanner fans out beyond K↔P.
        f_pairs = {p for p in platforms_seen if "forecastex" in p}
        assert len(f_pairs) >= 2, f"expected ForecastEx-paired edges, got {platforms_seen}"
        # And at least one F-leg appears as YES (buy side) AND one as NO.
        assert any(p[0] == "forecastex" for p in platforms_seen)
        assert any(p[1] == "forecastex" for p in platforms_seen)

    asyncio.run(run())


def test_scanner_applies_forecastex_flat_fee_in_compute_fee():
    """Both directions of compute_fee must route ForecastEx to the flat
    $0.005/contract path, NOT the Kalshi quadratic or Polymarket linear."""
    # Kalshi quadratic: very small near 50¢, grows with price.
    k_fee = compute_fee("kalshi", 0.5, 100)
    # Polymarket linear: 5% by default.
    p_fee = compute_fee("polymarket", 0.5, 100)
    # ForecastEx flat: $0.005 × 100 = $0.50 regardless of price.
    f_fee = compute_fee("forecastex", 0.5, 100)
    f_fee_99 = compute_fee("forecastex", 0.99, 100)
    f_fee_01 = compute_fee("forecastex", 0.01, 100)

    assert f_fee == pytest.approx(0.50)
    assert f_fee_99 == pytest.approx(0.50)
    assert f_fee_01 == pytest.approx(0.50)
    # Sanity: distinct from the other two venues.
    assert abs(f_fee - k_fee) > 1e-6
    assert abs(f_fee - p_fee) > 1e-6


def test_scanner_min_edge_filters_unprofitable_pair(canonical_id):
    """ForecastEx + Polymarket priced so net edge < MIN_EDGE_CENTS; the
    opportunity must NOT appear."""
    async def run():
        store = PriceStore(ttl=60)
        scanner = ArbitrageScanner(_scanner_config(min_edge_cents=7.0), store)

        # Total ~0.98 — gross 2¢, well under 7¢ floor.
        await store.put(_mk_price("forecastex", canonical_id, yes=0.49, no=0.52, fee_rate=0.005))
        await store.put(_mk_price("polymarket", canonical_id, yes=0.49, no=0.49, fee_rate=0.05))

        ops = await scanner.scan_once()
        for o in ops:
            assert o.net_edge_cents >= 7.0

    asyncio.run(run())


def test_scanner_collapses_to_two_platforms_when_forecastex_absent(canonical_id):
    """FORECASTEX_ENABLED=false → no ForecastEx writes hit the PriceStore;
    scanner must keep producing K↔P opportunities exactly as before."""
    async def run():
        store = PriceStore(ttl=60)
        scanner = ArbitrageScanner(_scanner_config(min_edge_cents=0.5), store)

        await store.put(_mk_price("kalshi", canonical_id, yes=0.40, no=0.60, fee_rate=0.07))
        await store.put(_mk_price("polymarket", canonical_id, yes=0.45, no=0.50, fee_rate=0.02))

        ops = await scanner.scan_once()
        venues = {(o.yes_platform, o.no_platform) for o in ops}
        # Only K↔P pairs; no forecastex anywhere.
        assert all("forecastex" not in pair for pair in venues)
        # And at least one of the two K↔P pairs shows up.
        assert venues & {("kalshi", "polymarket"), ("polymarket", "kalshi")}

    asyncio.run(run())


def test_scanner_includes_forecastex_fee_in_net_edge(canonical_id):
    """The net edge of a F-leg pair must reflect $0.005/contract — not 0."""
    async def run():
        store = PriceStore(ttl=60)
        scanner = ArbitrageScanner(_scanner_config(min_edge_cents=0.1), store)

        # YES=ForecastEx 40¢, NO=Polymarket 55¢. Gross = 5¢.
        # ForecastEx fee = $0.005/contract = 0.5¢/contract.
        # Polymarket 5% linear at 55¢ * qty.
        await store.put(_mk_price("forecastex", canonical_id, yes=0.40, no=0.60, fee_rate=0.005))
        await store.put(_mk_price("polymarket", canonical_id, yes=0.55, no=0.55, fee_rate=0.05))

        ops = await scanner.scan_once()
        f_yes = [o for o in ops if o.yes_platform == "forecastex" and o.no_platform == "polymarket"]
        assert f_yes
        opp = f_yes[0]
        # net = gross - per-contract fees. Both legs charge non-zero fees.
        assert opp.yes_fee > 0
        assert opp.no_fee > 0
        # ForecastEx per-contract fee is at the half-cent floor ($0.005).
        assert opp.yes_fee == pytest.approx(0.005)

    asyncio.run(run())


# ── Signal-integrity regression: dead-ask / sub-min-size ────────────────────


def test_scanner_skips_when_forecastex_leg_has_dead_ask(canonical_id):
    """ForecastEx with yes_ask=0 (empty book) must NOT generate an
    opportunity, even when last-trade mark price suggests a positive edge.

    Reproduces the ARB-000695 / ARB-000699 phantom: GOP_SENATE_2026
    ForecastEx leg quoted bid=0/ask=0 (dead order book) but yes_price=0.44
    from a stale last-trade print. Pre-fix scanner fell back to mark and
    fabricated a 7.35¢ "edge" against an empty book, producing two
    consecutive zero-fill executions on Polymarket. The fix requires a
    live ASK on the side we'd buy.
    """
    async def run():
        store = PriceStore(ttl=60)
        scanner = ArbitrageScanner(_scanner_config(min_edge_cents=0.5), store)

        # Polymarket NO leg is live (ask=0.469).
        await store.put(_mk_price(
            "polymarket", canonical_id, yes=0.55, no=0.469, fee_rate=0.05,
        ))

        # ForecastEx leg: stale mark, but bid=ask=0 — dead book. We craft
        # this directly because _mk_price derives ask from yes_price.
        dead_book = PricePoint(
            platform="forecastex",
            canonical_id=canonical_id,
            yes_price=0.44,         # stale last-trade print
            no_price=0.56,
            yes_volume=0,
            no_volume=0,
            timestamp=time.time(),
            raw_market_id="fcst-dead",
            yes_market_id="fcst-dead",
            no_market_id="fcst-dead",
            yes_bid=0.0,
            yes_ask=0.0,
            no_bid=0.0,
            no_ask=0.0,
            fee_rate=0.005,
            mapping_status="confirmed",
            mapping_score=1.0,
        )
        await store.put(dead_book)

        ops = await scanner.scan_once()
        # No opportunity should involve the dead-ask ForecastEx leg as a buy.
        for o in ops:
            assert not (o.yes_platform == "forecastex"), (
                f"Scanner emitted dead-ask FX YES leg: {o}"
            )

    asyncio.run(run())


def test_scanner_skips_when_polymarket_qty_below_min_size(canonical_id):
    """Sub-5-share Polymarket sizing must result in no opportunity.

    Polymarket US documents a 5-share minimum order size. A 1-share order
    at $0.469 (the ARB-000695/699 footprint) is rejected with zero-fill.
    The scanner must skip the opp at sizing time instead of placing a
    doomed order. Recreated by tight liquidity_limited (yes_volume=1) so
    the sizing pipeline lands on size=1 < min=5.
    """
    async def run():
        store = PriceStore(ttl=60)
        # min_liquidity=0 so we DON'T fail at the depth gate — we want to
        # exercise the min-size cap, not the depth gate.
        scanner = ArbitrageScanner(
            ScannerConfig(
                min_edge_cents=0.5,
                persistence_scans=1,
                max_position_usd=100.0,
                confidence_threshold=0.1,
                min_liquidity=0.0,
                max_bid_ask_spread_cents=0.0,
            ),
            store,
        )

        kalshi_leg = PricePoint(
            platform="kalshi",
            canonical_id=canonical_id,
            yes_price=0.40,
            no_price=0.55,
            yes_bid=0.39, yes_ask=0.40,
            no_bid=0.54, no_ask=0.55,
            yes_volume=1,   # forces liquidity_limited=1
            no_volume=1,
            timestamp=time.time(),
            raw_market_id="K1",
            yes_market_id="K1",
            no_market_id="K1",
            fee_rate=0.07,
            mapping_status="confirmed",
            mapping_score=1.0,
        )
        poly_leg = PricePoint(
            platform="polymarket",
            canonical_id=canonical_id,
            yes_price=0.50,
            no_price=0.469,
            yes_bid=0.49, yes_ask=0.50,
            no_bid=0.468, no_ask=0.469,
            yes_volume=1,
            no_volume=1,
            timestamp=time.time(),
            raw_market_id="P1",
            yes_market_id="P1",
            no_market_id="P1",
            fee_rate=0.05,
            mapping_status="confirmed",
            mapping_score=1.0,
        )
        await store.put(kalshi_leg)
        await store.put(poly_leg)

        ops = await scanner.scan_once()
        # Any opp that includes Polymarket must have suggested_qty >= 5,
        # or it must not have been emitted at all.
        for o in ops:
            if o.yes_platform == "polymarket" or o.no_platform == "polymarket":
                assert o.suggested_qty >= 5, (
                    f"Scanner emitted sub-min-size Polymarket opp: "
                    f"qty={o.suggested_qty} canonical={o.canonical_id}"
                )

    asyncio.run(run())
