"""Tests for the StrandedMitigationEngine.

Pin every branch of the decision tree against canonical inputs that
mirror the live stranded corpus (penny longshots, polymarket midterms,
cross-book recovery candidates). The engine is pure logic — no I/O —
so tests stay synchronous-fast.
"""
from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from arbiter.recovery.mitigation_engine import (
    CLOSE_NOW,
    COMPLETE_ARB,
    HOLD_TO_SETTLE,
    MANUAL_REVIEW,
    MitigationConfig,
    MitigationEngine,
    build_profitability_snapshot,
)
from arbiter.recovery.stranded_reconciler import StrandedPosition


def _pos(**overrides) -> StrandedPosition:
    defaults = dict(
        platform="kalshi",
        market_id="K-X",
        side="YES",
        qty=10.0,
        cost_basis_usd=5.0,
        mtm_usd=5.5,
        unrealized_usd=0.5,
        best_bid=0.55,
        best_ask=0.56,
        title="Stub market",
        first_seen_ts=time.time(),
        last_seen_ts=time.time(),
    )
    defaults.update(overrides)
    return StrandedPosition(**defaults)


def _adapter(supports_close=True, supports_fok=True):
    a = MagicMock()
    if supports_close:
        a.place_unwind_sell = AsyncMock()
    else:
        if hasattr(a, "place_unwind_sell"):
            del a.place_unwind_sell
    if supports_fok:
        a.place_fok = AsyncMock()
    else:
        if hasattr(a, "place_fok"):
            del a.place_fok
    return a


def test_profitability_snapshot_penny_longshot():
    """23-share NHL Hart-trophy lot at 2¢ avg, current bid 3¢."""
    pos = _pos(
        platform="kalshi", market_id="KXNHLHART-T-DUBOIS",
        qty=23, cost_basis_usd=0.46,
        best_bid=0.03, best_ask=0.04,
    )
    prof = build_profitability_snapshot(pos)
    assert prof.cost_per_share == pytest.approx(0.02)
    assert prof.mtm_usd == pytest.approx(0.69)        # 23 * 0.03
    # Even tiny fee swamps recoverable value.
    assert prof.fees_to_close_usd > 0
    assert prof.is_underwater is False                  # mtm > cost
    # EV using implied prob ≈ 4% over 23 shares ≈ $0.92
    assert prof.expected_value_at_settle_usd == pytest.approx(0.92)


def test_profitability_snapshot_polymarket_midterm():
    """50-NO lot on poly midterms, paid ~$0.21 avg, current bid 0.20."""
    pos = _pos(
        platform="polymarket", market_id="paccc-usho-midterms-2026-11-03-dem",
        side="NO", qty=50, cost_basis_usd=10.30,
        best_bid=0.20, best_ask=0.22,
    )
    prof = build_profitability_snapshot(pos)
    assert prof.cost_per_share == pytest.approx(0.206)
    assert prof.mtm_usd == pytest.approx(10.0)
    # Implied prob (taking the NO side at 0.22 ask) → ~22% so EV ≈ 50 * 0.22 = $11
    assert prof.expected_value_at_settle_usd == pytest.approx(11.0)


# ─── Decision tree tests ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_decide_hold_to_settle_for_penny():
    eng = MitigationEngine(
        adapters={"kalshi": _adapter()},
        market_map_provider=lambda: {},
    )
    pos = _pos(qty=23, cost_basis_usd=0.46, best_bid=0.03, best_ask=0.04)
    d = await eng.decide(pos)
    assert d.action == HOLD_TO_SETTLE
    assert "penny" in d.rationale.lower()
    assert d.autonomous_ok is True


@pytest.mark.asyncio
async def test_decide_close_now_when_underwater_and_close_advantage():
    """Position bought at 0.70/sh, market now 0.10 bid / 0.12 ask.
    Holding has tiny EV (12%); closing recovers some cash."""
    eng = MitigationEngine(
        adapters={"polymarket": _adapter()},
        market_map_provider=lambda: {},
    )
    pos = _pos(
        platform="polymarket", market_id="p-x",
        qty=50, cost_basis_usd=35.0,
        best_bid=0.10, best_ask=0.12,
    )
    d = await eng.decide(pos)
    assert d.action == CLOSE_NOW
    assert d.close_qty == 50
    assert d.close_price == pytest.approx(0.10)
    # Underwater + autonomous since notional is within $30 cap? cost=$35 → above cap, so NOT autonomous
    assert d.autonomous_ok is False


@pytest.mark.asyncio
async def test_decide_hold_when_implied_prob_above_close_proceeds():
    """50 NO bought at 0.20/sh, market 0.20 bid / 0.30 ask.
    Holding EV: 50 * 0.30 = $15 vs close ≈ $10 → HOLD wins."""
    eng = MitigationEngine(
        adapters={"polymarket": _adapter()},
        market_map_provider=lambda: {},
    )
    pos = _pos(
        platform="polymarket", market_id="p-y", side="NO",
        qty=50, cost_basis_usd=10.0,
        best_bid=0.20, best_ask=0.30,
    )
    d = await eng.decide(pos)
    assert d.action == HOLD_TO_SETTLE


@pytest.mark.asyncio
async def test_decide_small_no_bbo_holds_silently():
    """Small position (notional ≤ unactionable cap) with no BBO →
    HOLD_TO_SETTLE silently. The operator literally cannot close
    when there's no book, so a MANUAL_REVIEW alert is pure noise.
    Reproduces the KXMLBNL-26-PHI alert (70 YES @ $0.06 = $4.20)
    that the user got 2026-05-26 — must NOT alert anymore.
    """
    eng = MitigationEngine(
        adapters={"kalshi": _adapter()},
        market_map_provider=lambda: {},
        config=MitigationConfig(unactionable_max_notional_usd=10.0),
    )
    pos = _pos(qty=70, cost_basis_usd=4.20, best_bid=0.0, best_ask=0.0)
    d = await eng.decide(pos)
    assert d.action == HOLD_TO_SETTLE
    assert d.autonomous_ok is True
    assert "unactionable" in d.rationale.lower()


@pytest.mark.asyncio
async def test_decide_large_no_bbo_still_alerts_operator():
    """Above the unactionable cap, no BBO → MANUAL_REVIEW.
    Operator needs visibility on a $50 stranded position even when
    the book is currently empty (they may want to monitor for book
    reappearing and time a manual close)."""
    eng = MitigationEngine(
        adapters={"kalshi": _adapter()},
        market_map_provider=lambda: {},
        config=MitigationConfig(unactionable_max_notional_usd=10.0),
    )
    pos = _pos(qty=100, cost_basis_usd=50.0, best_bid=0.0, best_ask=0.0)
    d = await eng.decide(pos)
    assert d.action == MANUAL_REVIEW
    assert d.autonomous_ok is False


@pytest.mark.asyncio
async def test_decide_small_no_adapter_holds_silently():
    """Small position on a venue with no execution adapter → silent
    HOLD. Same logic: operator can't act, alert is noise."""
    eng = MitigationEngine(
        adapters={},                       # no kalshi adapter
        market_map_provider=lambda: {},
        config=MitigationConfig(unactionable_max_notional_usd=10.0),
    )
    pos = _pos(qty=10, cost_basis_usd=5.0, best_bid=0.30, best_ask=0.32)
    d = await eng.decide(pos)
    assert d.action == HOLD_TO_SETTLE
    assert d.autonomous_ok is True


@pytest.mark.asyncio
async def test_decide_large_no_adapter_still_alerts_operator():
    eng = MitigationEngine(
        adapters={},
        market_map_provider=lambda: {},
        config=MitigationConfig(unactionable_max_notional_usd=10.0),
    )
    pos = _pos(qty=100, cost_basis_usd=50.0, best_bid=0.30, best_ask=0.32)
    d = await eng.decide(pos)
    assert d.action == MANUAL_REVIEW
    assert "no adapter" in d.rationale.lower()


# ─── COMPLETE_ARB tests ─────────────────────────────────────────────────


class _FakePriceStore:
    def __init__(self, quotes_by_canonical):
        self._quotes = quotes_by_canonical

    async def get_all_for_market(self, canonical_id):
        return self._quotes.get(canonical_id, {})


def _pp(platform, *, yes_price, no_price, yes_ask=None, no_ask=None,
        yes_market_id=None, no_market_id=None, **kw):
    """Lightweight PricePoint stub — only the fields the engine reads."""
    return SimpleNamespace(
        platform=platform,
        yes_price=yes_price, no_price=no_price,
        yes_ask=yes_ask if yes_ask is not None else yes_price,
        no_ask=no_ask if no_ask is not None else no_price,
        yes_market_id=yes_market_id or f"{platform}-YES",
        no_market_id=no_market_id or f"{platform}-NO",
        raw_market_id=yes_market_id or f"{platform}-YES",
        **kw,
    )


@pytest.mark.asyncio
async def test_decide_complete_arb_when_opposite_leg_priced_for_positive_edge():
    """Stranded YES on polymarket at 0.40/sh. Kalshi quotes NO at 0.50
    ask → combined cost 0.90, leaving 10c gross edge - fees → COMPLETE_ARB."""
    market_map = {
        "MIDTERMS_X": {
            "status": "confirmed",
            "polymarket": "POLY-MIDTERMS-Y",
            "kalshi": "KX-MIDTERMS-Y",
        },
    }
    quotes = {
        "MIDTERMS_X": {
            "polymarket": _pp("polymarket", yes_price=0.40, no_price=0.55,
                              yes_ask=0.40, no_ask=0.55,
                              yes_market_id="POLY-MIDTERMS-Y",
                              no_market_id="POLY-MIDTERMS-N"),
            "kalshi": _pp("kalshi", yes_price=0.45, no_price=0.50,
                          yes_ask=0.46, no_ask=0.50,
                          yes_market_id="KX-MIDTERMS-Y",
                          no_market_id="KX-MIDTERMS-N"),
        },
    }
    eng = MitigationEngine(
        price_store=_FakePriceStore(quotes),
        market_map_provider=lambda: market_map,
        adapters={
            "polymarket": _adapter(),
            "kalshi": _adapter(),
        },
        config=MitigationConfig(complete_arb_min_edge_cents=3.0),
    )
    pos = _pos(
        platform="polymarket", market_id="POLY-MIDTERMS-Y", side="YES",
        qty=20, cost_basis_usd=8.0,    # 0.40/sh
        best_bid=0.39, best_ask=0.41,
    )
    d = await eng.decide(pos)
    assert d.action == COMPLETE_ARB
    assert d.complete_arb_venue == "kalshi"
    assert d.complete_arb_side == "NO"
    assert d.complete_arb_market_id == "KX-MIDTERMS-N"
    assert d.complete_arb_qty == 20
    assert d.complete_arb_net_edge_cents >= 3.0


@pytest.mark.asyncio
async def test_decide_does_not_complete_arb_when_edge_too_thin():
    market_map = {
        "MIDTERMS_THIN": {
            "status": "confirmed",
            "polymarket": "POLY-THIN-Y",
            "kalshi": "KX-THIN-Y",
        },
    }
    # Same-side ask makes combined cost > $1, so net edge < 0.
    quotes = {
        "MIDTERMS_THIN": {
            "kalshi": _pp("kalshi", yes_price=0.40, no_price=0.65,
                          yes_ask=0.40, no_ask=0.65,
                          yes_market_id="KX-THIN-Y", no_market_id="KX-THIN-N"),
        },
    }
    eng = MitigationEngine(
        price_store=_FakePriceStore(quotes),
        market_map_provider=lambda: market_map,
        adapters={
            "polymarket": _adapter(),
            "kalshi": _adapter(),
        },
        config=MitigationConfig(complete_arb_min_edge_cents=3.0),
    )
    pos = _pos(
        platform="polymarket", market_id="POLY-THIN-Y", side="YES",
        qty=20, cost_basis_usd=8.0,
        best_bid=0.39, best_ask=0.41,
    )
    d = await eng.decide(pos)
    # Should fall through to hold/close, NOT COMPLETE_ARB.
    assert d.action != COMPLETE_ARB
