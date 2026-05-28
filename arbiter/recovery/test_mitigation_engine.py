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
    """Aged position bought at 0.16/sh, market now 0.10 bid / 0.12 ask.
    Position is >24h old so the age-bracket loss cap is 50%; realised
    loss here ≈ 37% sits within that ceiling, so the close fires."""
    eng = MitigationEngine(
        adapters={"polymarket": _adapter()},
        market_map_provider=lambda: {},
    )
    pos = _pos(
        platform="polymarket", market_id="p-x",
        qty=50, cost_basis_usd=8.0,
        best_bid=0.10, best_ask=0.12,
        first_seen_ts=time.time() - 25 * 3600.0,  # 25h old → 24h+ bracket
    )
    d = await eng.decide(pos)
    assert d.action == CLOSE_NOW
    assert d.close_qty == 50
    assert d.close_price == pytest.approx(0.10)
    # Notional ($8) is within the default $30 autonomous cap.
    assert d.autonomous_ok is True


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


# ─── Age-bracketed loss-cut tests ─────────────────────────────────────


@pytest.mark.asyncio
async def test_decide_young_position_holds_when_close_would_lock_loss():
    """0-6h old + underwater → HOLD (young bracket allows 0% loss).

    Engine should refuse to crystallise any loss in the first six hours
    because fresh strands are often transient bookkeeping drift the
    engine can still resolve via COMPLETE_ARB on the next quote update.
    """
    eng = MitigationEngine(
        adapters={"polymarket": _adapter()},
        market_map_provider=lambda: {},
    )
    pos = _pos(
        platform="polymarket", market_id="p-young",
        qty=50, cost_basis_usd=8.0,
        best_bid=0.10, best_ask=0.12,
        first_seen_ts=time.time() - 1 * 3600.0,  # 1h old → 0-6h bracket
    )
    d = await eng.decide(pos)
    assert d.action == HOLD_TO_SETTLE
    assert "age-bracket" in d.rationale.lower()
    assert "0-6h" in d.rationale


@pytest.mark.asyncio
async def test_decide_mature_position_closes_within_25pct_loss_cap():
    """6-24h old + 20% loss → CLOSE_NOW (within the 25% cap)."""
    eng = MitigationEngine(
        adapters={"polymarket": _adapter()},
        market_map_provider=lambda: {},
    )
    # qty=50, cost=$10 (0.20/sh), bid=$0.16 → close proceeds ~$8 →
    # realised loss ~20% sits inside the 6-24h 25% cap.
    pos = _pos(
        platform="polymarket", market_id="p-mature",
        qty=50, cost_basis_usd=10.0,
        best_bid=0.16, best_ask=0.18,
        first_seen_ts=time.time() - 12 * 3600.0,  # 12h → 6-24h
    )
    d = await eng.decide(pos)
    assert d.action == CLOSE_NOW
    assert d.close_qty == 50
    assert "6-24h" in d.rationale


@pytest.mark.asyncio
async def test_decide_mature_position_holds_when_loss_exceeds_25pct():
    """6-24h old + 50% loss → HOLD (exceeds bracket cap)."""
    eng = MitigationEngine(
        adapters={"polymarket": _adapter()},
        market_map_provider=lambda: {},
    )
    # cost $10 (0.20/sh), bid $0.05 → close ~$2.5 → 75% loss > 25% cap.
    pos = _pos(
        platform="polymarket", market_id="p-mature-deep",
        qty=50, cost_basis_usd=10.0,
        best_bid=0.05, best_ask=0.07,
        first_seen_ts=time.time() - 12 * 3600.0,
    )
    d = await eng.decide(pos)
    assert d.action == HOLD_TO_SETTLE
    assert "age-bracket" in d.rationale.lower()


@pytest.mark.asyncio
async def test_decide_aged_position_closes_up_to_50pct_loss():
    """24h+ old + 40% loss → CLOSE_NOW (within the 50% cap)."""
    eng = MitigationEngine(
        adapters={"polymarket": _adapter()},
        market_map_provider=lambda: {},
    )
    # cost $10, bid $0.12 → close ~$6 → 40% loss within 50% cap.
    pos = _pos(
        platform="polymarket", market_id="p-aged",
        qty=50, cost_basis_usd=10.0,
        best_bid=0.12, best_ask=0.14,
        first_seen_ts=time.time() - 30 * 3600.0,  # 30h → 24h+
    )
    d = await eng.decide(pos)
    assert d.action == CLOSE_NOW
    assert "24h+" in d.rationale


@pytest.mark.asyncio
async def test_decide_aged_position_holds_when_loss_exceeds_50pct():
    """24h+ old + 80% loss → HOLD (exceeds even the 50% aged cap)."""
    eng = MitigationEngine(
        adapters={"polymarket": _adapter()},
        market_map_provider=lambda: {},
    )
    # cost $10, bid $0.04 → close ~$2 → 80% loss > 50% cap.
    pos = _pos(
        platform="polymarket", market_id="p-aged-deep",
        qty=50, cost_basis_usd=10.0,
        best_bid=0.04, best_ask=0.06,
        first_seen_ts=time.time() - 30 * 3600.0,
    )
    d = await eng.decide(pos)
    assert d.action == HOLD_TO_SETTLE


# ─── Settlement-proximity tests ───────────────────────────────────────


@pytest.mark.asyncio
async def test_decide_settlement_proximity_overrides_close():
    """Settlement within 24h → HOLD even if loss-bracket math would close.

    Paying spread to exit a position that resolves in hours is never
    rational; settlement itself zeroes-or-pays the lot for free.
    """
    eng = MitigationEngine(
        adapters={"polymarket": _adapter()},
        market_map_provider=lambda: {},
    )
    pos = _pos(
        platform="polymarket", market_id="p-near-settle",
        qty=50, cost_basis_usd=10.0,
        best_bid=0.12, best_ask=0.14,
        first_seen_ts=time.time() - 30 * 3600.0,  # 30h old
        expiry_ts=time.time() + 6 * 3600.0,        # resolves in 6h
    )
    d = await eng.decide(pos)
    assert d.action == HOLD_TO_SETTLE
    assert "settlement-proximity" in d.rationale.lower()


@pytest.mark.asyncio
async def test_decide_far_from_settlement_does_not_trigger_proximity_hold():
    """Settlement >24h away → normal cost-benefit applies."""
    eng = MitigationEngine(
        adapters={"polymarket": _adapter()},
        market_map_provider=lambda: {},
    )
    pos = _pos(
        platform="polymarket", market_id="p-far-settle",
        qty=50, cost_basis_usd=10.0,
        best_bid=0.12, best_ask=0.14,
        first_seen_ts=time.time() - 30 * 3600.0,
        expiry_ts=time.time() + 7 * 24 * 3600.0,   # 7 days out
    )
    d = await eng.decide(pos)
    # 40% loss within 50% bracket → CLOSE_NOW fires.
    assert d.action == CLOSE_NOW
