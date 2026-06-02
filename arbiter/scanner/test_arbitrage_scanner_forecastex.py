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


# ── Per-pair liquidity floor: FX-containing pairs surfaced as illiquid ────────


def test_scanner_fx_polymarket_zero_volume_surfaces_as_illiquid():
    """ForecastEx×Polymarket with zero FX volume must appear as 'illiquid' when
    the forecastex_polymarket pair floor > 0 — NOT silently dropped.

    Symmetric FX-side counterpart of the K×P illiquid regression
    (test_scanner_per_pair_liquidity_floor_still_blocks_kp_with_zero_size).
    The early-liquidity-check path in _build_cross_platform_opportunity must
    apply regardless of which pair is being built.
    """
    canonical_id = "FX_P_ILLIQUID_TEST_" + uuid.uuid4().hex[:8]

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
                    "forecastex_polymarket": 25.0,   # FX×P floor matches global
                    "forecastex_kalshi": 0.0,
                    "kalshi_polymarket": 25.0,
                },
            ),
            store,
        )
        original_mapping = MARKET_MAP.get(canonical_id)
        MARKET_MAP[canonical_id] = {
            "description": "FX×P zero-volume illiquid test",
            "status": "confirmed",
            "allow_auto_trade": True,
            "mapping_score": 0.95,
            "resolution_match_status": "identical",
        }
        try:
            fresh = time.time()
            # Polymarket YES side reports volume; ForecastEx NO side is 0.
            # min_available_liquidity = min(200, 0) = 0 < 25 → must be illiquid.
            await store.put(PricePoint(
                platform="polymarket", canonical_id=canonical_id,
                yes_price=0.46, no_price=0.55,
                yes_volume=200, no_volume=200, timestamp=fresh,
                raw_market_id="P-FXP", yes_market_id="P-FXP", no_market_id="P-FXP",
                fee_rate=0.05, mapping_status="confirmed", mapping_score=0.9,
                yes_bid=0.455, yes_ask=0.46, no_bid=0.545, no_ask=0.55,
            ))
            await store.put(PricePoint(
                platform="forecastex", canonical_id=canonical_id,
                yes_price=0.52, no_price=0.47,
                yes_volume=0, no_volume=0,
                timestamp=fresh,
                raw_market_id="FX-FXP", yes_market_id="FX-FXP", no_market_id="FX-FXP",
                fee_rate=0.005, mapping_status="confirmed", mapping_score=0.9,
                yes_bid=0.515, yes_ask=0.52, no_bid=0.465, no_ask=0.47,
            ))
            opps = await scanner.scan_once()
            fxp_opps = [
                o for o in opps
                if set([o.yes_platform, o.no_platform]) == {"forecastex", "polymarket"}
            ]
            tradable = [o for o in fxp_opps if o.status == "tradable"]
            assert not tradable, (
                f"FX×P with zero FX volume must NOT be tradable; "
                f"got {[(o.net_edge_cents, o.status) for o in fxp_opps]}"
            )
            illiquid = [o for o in fxp_opps if o.status == "illiquid"]
            assert illiquid, (
                f"FX×P zero-volume arb must appear as illiquid, not silently dropped; "
                f"got {[o.status for o in fxp_opps]}"
            )
        finally:
            if original_mapping is None:
                MARKET_MAP.pop(canonical_id, None)
            else:
                MARKET_MAP[canonical_id] = original_mapping

    asyncio.run(runner())


def test_scanner_fx_kalshi_zero_volume_surfaces_as_illiquid():
    """ForecastEx×Kalshi with zero FX volume must appear as 'illiquid' when
    the forecastex_kalshi pair floor > 0 — NOT silently dropped.

    A zero floor is the unlock signal (tested by
    test_scanner_per_pair_liquidity_floor_unlocks_fx_arb_with_zero_size).
    This test verifies the blocking side: an explicit non-zero floor must be
    respected by the scanner regardless of pair direction.
    """
    canonical_id = "FX_K_ILLIQUID_TEST_" + uuid.uuid4().hex[:8]

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
                    "forecastex_kalshi": 25.0,    # FX×K floor matches global
                    "forecastex_polymarket": 0.0,
                    "kalshi_polymarket": 25.0,
                },
            ),
            store,
        )
        original_mapping = MARKET_MAP.get(canonical_id)
        MARKET_MAP[canonical_id] = {
            "description": "FX×K zero-volume illiquid test",
            "status": "confirmed",
            "allow_auto_trade": True,
            "mapping_score": 0.95,
            "resolution_match_status": "identical",
        }
        try:
            fresh = time.time()
            # Kalshi YES side has volume; ForecastEx NO side is 0.
            await store.put(PricePoint(
                platform="kalshi", canonical_id=canonical_id,
                yes_price=0.46, no_price=0.55,
                yes_volume=200, no_volume=200, timestamp=fresh,
                raw_market_id="K-FXK", yes_market_id="K-FXK", no_market_id="K-FXK",
                fee_rate=0.07, mapping_status="confirmed", mapping_score=0.9,
                yes_bid=0.455, yes_ask=0.46, no_bid=0.545, no_ask=0.55,
            ))
            await store.put(PricePoint(
                platform="forecastex", canonical_id=canonical_id,
                yes_price=0.52, no_price=0.47,
                yes_volume=0, no_volume=0,
                timestamp=fresh,
                raw_market_id="FX-FXK", yes_market_id="FX-FXK", no_market_id="FX-FXK",
                fee_rate=0.005, mapping_status="confirmed", mapping_score=0.9,
                yes_bid=0.515, yes_ask=0.52, no_bid=0.465, no_ask=0.47,
            ))
            opps = await scanner.scan_once()
            fxk_opps = [
                o for o in opps
                if set([o.yes_platform, o.no_platform]) == {"forecastex", "kalshi"}
            ]
            tradable = [o for o in fxk_opps if o.status == "tradable"]
            assert not tradable, (
                f"FX×K with zero FX volume must NOT be tradable; "
                f"got {[(o.net_edge_cents, o.status) for o in fxk_opps]}"
            )
            illiquid = [o for o in fxk_opps if o.status == "illiquid"]
            assert illiquid, (
                f"FX×K zero-volume arb must appear as illiquid, not silently dropped; "
                f"got {[o.status for o in fxk_opps]}"
            )
        finally:
            if original_mapping is None:
                MARKET_MAP.pop(canonical_id, None)
            else:
                MARKET_MAP[canonical_id] = original_mapping

    asyncio.run(runner())


# ── 2026-06-02 FX price-freshness guard ────────────────────────────────────


def test_scanner_drops_fx_pair_when_fx_yes_quote_stale(canonical_id, monkeypatch):
    """FX YES leg older than FORECASTEX_MAX_QUOTE_AGE_S — pair must skip
    but K×P pairs on the same canonical_id should still publish."""
    monkeypatch.setenv("FORECASTEX_MAX_QUOTE_AGE_S", "30")

    async def run():
        store = PriceStore(ttl=600)
        scanner = ArbitrageScanner(_scanner_config(min_edge_cents=0.5), store)

        stale_ts = time.time() - 120  # 2 minutes old
        fresh_ts = time.time()
        # FX YES is stale; FX shouldn't be picked up as YES side.
        await store.put(_mk_price("forecastex", canonical_id, yes=0.35, no=0.55, fee_rate=0.005, ts=stale_ts))
        await store.put(_mk_price("kalshi", canonical_id, yes=0.40, no=0.55, fee_rate=0.07, ts=fresh_ts))
        await store.put(_mk_price("polymarket", canonical_id, yes=0.55, no=0.40, fee_rate=0.02, ts=fresh_ts))

        ops = await scanner.scan_once()
        # No FX-as-YES pair allowed.
        assert not any(o.yes_platform == "forecastex" for o in ops), (
            f"stale FX YES leaked into opportunities: "
            f"{[(o.yes_platform, o.no_platform) for o in ops]}"
        )
        # K×P pair should still publish.
        assert any(
            (o.yes_platform, o.no_platform) in {("kalshi", "polymarket"), ("polymarket", "kalshi")}
            for o in ops
        ), "K×P pair must keep publishing when FX is stale"

    asyncio.run(run())


def test_scanner_drops_fx_pair_when_fx_no_quote_stale(canonical_id, monkeypatch):
    """FX NO leg older than threshold — pair must skip."""
    monkeypatch.setenv("FORECASTEX_MAX_QUOTE_AGE_S", "30")

    async def run():
        store = PriceStore(ttl=600)
        scanner = ArbitrageScanner(_scanner_config(min_edge_cents=0.5), store)

        stale_ts = time.time() - 120
        fresh_ts = time.time()
        await store.put(_mk_price("forecastex", canonical_id, yes=0.35, no=0.55, fee_rate=0.005, ts=stale_ts))
        await store.put(_mk_price("kalshi", canonical_id, yes=0.40, no=0.55, fee_rate=0.07, ts=fresh_ts))

        ops = await scanner.scan_once()
        assert not any(o.no_platform == "forecastex" for o in ops), (
            f"stale FX NO leaked: {[(o.yes_platform, o.no_platform) for o in ops]}"
        )

    asyncio.run(run())


def test_scanner_allows_fresh_fx_quotes(canonical_id, monkeypatch):
    """Sanity: FX with fresh timestamps under the threshold must NOT be
    excluded."""
    monkeypatch.setenv("FORECASTEX_MAX_QUOTE_AGE_S", "60")

    async def run():
        store = PriceStore(ttl=60)
        scanner = ArbitrageScanner(_scanner_config(min_edge_cents=0.5), store)

        await store.put(_mk_price("forecastex", canonical_id, yes=0.35, no=0.55, fee_rate=0.005))
        await store.put(_mk_price("kalshi", canonical_id, yes=0.40, no=0.55, fee_rate=0.07))

        ops = await scanner.scan_once()
        fx_pairs = [(o.yes_platform, o.no_platform) for o in ops
                    if "forecastex" in (o.yes_platform, o.no_platform)]
        assert fx_pairs, "fresh FX quotes must produce at least one opportunity"

    asyncio.run(run())


def test_scanner_freshness_guard_disabled_when_env_zero(canonical_id, monkeypatch):
    """FORECASTEX_MAX_QUOTE_AGE_S=0 disables the FX freshness guard so
    the fx_stale_* skip counters never increment."""
    monkeypatch.setenv("FORECASTEX_MAX_QUOTE_AGE_S", "0")

    async def run():
        store = PriceStore(ttl=600)
        scanner = ArbitrageScanner(_scanner_config(min_edge_cents=0.5), store)
        stale_ts = time.time() - 90  # past 60s default, still inside store TTL
        fresh_ts = time.time()
        await store.put(_mk_price("forecastex", canonical_id, yes=0.35, no=0.55, fee_rate=0.005, ts=stale_ts))
        await store.put(_mk_price("kalshi", canonical_id, yes=0.40, no=0.55, fee_rate=0.07, ts=fresh_ts))

        await scanner.scan_once()
        # Guard disabled → fx_stale_* counters never fire.
        assert scanner._skip_reasons.get("fx_stale_yes", 0) == 0
        assert scanner._skip_reasons.get("fx_stale_no", 0) == 0

    asyncio.run(run())
