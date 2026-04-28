"""Tests for the arbitrage Telegram alert gate and message formatter.

The bug these tests defend against: a vague alert that named the market
category ("U.S Senate Midterm Winner") rather than the specific outcome,
and that fired on a phantom $0.04 Kalshi last_price paired with a real
$0.47 Polymarket ask. Operators couldn't tell what to trade and the
"49¢ edge" wasn't real.
"""
from __future__ import annotations

import asyncio
import time

import pytest

from arbiter.monitor.balance import (
    ALERT_MAX_QUOTE_AGE_SECONDS,
    ALERT_MIN_PRICE,
    BalanceMonitor,
    _alert_is_safe_to_send,
    _alert_outcome_is_specific,
    _format_arb_alert,
    _pick_alert_outcome,
)
from arbiter.scanner.arbitrage import (
    ArbitrageOpportunity,
    extract_outcome_metadata,
)
from arbiter.utils.price_store import PricePoint


# ── Fixtures ──────────────────────────────────────────────────────────


def _make_safe_opp(**overrides) -> ArbitrageOpportunity:
    """An opportunity that passes every gate by default. Tests override
    one field at a time to exercise individual rejection paths."""
    base = dict(
        canonical_id="DEM_SENATE_2026",
        description="U.S Senate Midterm Winner",
        yes_platform="kalshi",
        yes_price=0.52,
        yes_fee=0.005,
        yes_market_id="KXSENATE-2026-D",
        no_platform="polymarket",
        no_price=0.45,
        no_fee=0.005,
        no_market_id="0xabcdef0123456789",
        gross_edge=0.03,
        total_fees=0.012,
        net_edge=0.018,
        net_edge_cents=3.0,
        suggested_qty=15,
        max_profit_usd=1.80,
        timestamp=time.time(),
        confidence=0.86,
        status="tradable",
        persistence_count=3,
        quote_age_seconds=5.0,
        min_available_liquidity=100.0,
        mapping_status="confirmed",
        mapping_score=0.95,
        yes_outcome_name="Democrats",
        no_outcome_name="Will Democrats win Senate Majority in 2026?",
        yes_question="Will Democrats win Senate Majority in 2026?",
        no_question="Will Democrats win Senate Majority in 2026?",
        yes_bid=0.50,
        yes_ask=0.52,
        no_bid=0.43,
        no_ask=0.45,
        yes_quote_age_seconds=3.0,
        no_quote_age_seconds=5.0,
    )
    base.update(overrides)
    return ArbitrageOpportunity(**base)


# ── extract_outcome_metadata: scanner-side helper ──────────────────────


def test_extract_outcome_metadata_kalshi_uses_yes_subtitle_for_yes_side():
    pp = PricePoint(
        platform="kalshi",
        canonical_id="DEM_SENATE_2026",
        yes_price=0.52, no_price=0.48,
        yes_volume=100, no_volume=100,
        timestamp=time.time(),
        raw_market_id="KXSENATE-2026-D",
        metadata={
            "market_title": "Will Democrats win Senate Majority in 2026?",
            "yes_sub_title": "Democrats",
            "no_sub_title": "Republicans",
        },
    )
    outcome, question = extract_outcome_metadata(pp, "yes")
    assert outcome == "Democrats"
    assert question == "Will Democrats win Senate Majority in 2026?"


def test_extract_outcome_metadata_kalshi_uses_no_subtitle_for_no_side():
    pp = PricePoint(
        platform="kalshi",
        canonical_id="DEM_SENATE_2026",
        yes_price=0.52, no_price=0.48,
        yes_volume=100, no_volume=100,
        timestamp=time.time(),
        raw_market_id="KXSENATE-2026-D",
        metadata={
            "market_title": "Will Democrats win Senate Majority in 2026?",
            "yes_sub_title": "Democrats",
            "no_sub_title": "Republicans",
        },
    )
    outcome, _ = extract_outcome_metadata(pp, "no")
    assert outcome == "Republicans"


def test_extract_outcome_metadata_polymarket_uses_question():
    pp = PricePoint(
        platform="polymarket",
        canonical_id="DEM_SENATE_2026",
        yes_price=0.52, no_price=0.48,
        yes_volume=100, no_volume=100,
        timestamp=time.time(),
        raw_market_id="0xabc",
        metadata={"question": "Will Democrats win Senate Majority in 2026?"},
    )
    outcome, question = extract_outcome_metadata(pp, "yes")
    assert outcome == "Will Democrats win Senate Majority in 2026?"
    assert question == "Will Democrats win Senate Majority in 2026?"


def test_extract_outcome_metadata_returns_empty_when_metadata_missing():
    pp = PricePoint(
        platform="kalshi",
        canonical_id="X",
        yes_price=0.5, no_price=0.5,
        yes_volume=0, no_volume=0,
        timestamp=time.time(),
        raw_market_id="K-X",
        metadata={},
    )
    outcome, question = extract_outcome_metadata(pp, "yes")
    assert outcome == ""
    assert question == ""


# ── Gate: happy path ──────────────────────────────────────────────────


def test_safe_opportunity_passes_gate():
    opp = _make_safe_opp()
    assert _alert_is_safe_to_send(opp) is True


# ── Gate: outcome specificity (the original bug) ──────────────────────


def test_gate_rejects_when_outcome_name_matches_canonical_description():
    """The reported bug: alert showed 'U.S Senate Midterm Winner' as the
    outcome — that's the canonical description, not the specific side."""
    opp = _make_safe_opp(
        yes_outcome_name="U.S Senate Midterm Winner",
        no_outcome_name="U.S Senate Midterm Winner",
        yes_question="U.S Senate Midterm Winner",
        no_question="U.S Senate Midterm Winner",
    )
    assert _alert_is_safe_to_send(opp) is False


def test_gate_rejects_when_outcome_name_matches_canonical_with_punctuation_diff():
    """Defensive: 'u.s. senate midterm winner' should be treated as same as
    'U.S Senate Midterm Winner' since punctuation/case shouldn't save a
    vague match."""
    opp = _make_safe_opp(
        yes_outcome_name="u.s. senate midterm winner",
        no_outcome_name="U.S SENATE MIDTERM WINNER",
    )
    assert _alert_is_safe_to_send(opp) is False


def test_gate_rejects_when_both_outcome_names_blank():
    opp = _make_safe_opp(yes_outcome_name="", no_outcome_name="")
    assert _alert_is_safe_to_send(opp) is False


def test_gate_passes_when_only_one_side_has_specific_outcome():
    opp = _make_safe_opp(
        yes_outcome_name="Democrats",
        no_outcome_name="",
    )
    assert _alert_is_safe_to_send(opp) is True


# ── Gate: price floor (the phantom-$0.04 bug) ─────────────────────────


def test_gate_rejects_yes_price_below_floor():
    opp = _make_safe_opp(yes_price=0.04, gross_edge=0.51, net_edge_cents=49.0)
    assert _alert_is_safe_to_send(opp) is False


def test_gate_rejects_no_price_below_floor():
    opp = _make_safe_opp(no_price=0.03)
    assert _alert_is_safe_to_send(opp) is False


def test_gate_passes_at_price_floor_boundary():
    opp = _make_safe_opp(
        yes_price=ALERT_MIN_PRICE,
        no_price=ALERT_MIN_PRICE,
        gross_edge=0.90,
        total_fees=0.005,
        net_edge=0.895,
        net_edge_cents=89.5,
    )
    assert _alert_is_safe_to_send(opp) is True


# ── Gate: per-side quote age ──────────────────────────────────────────


def test_gate_rejects_stale_yes_quote():
    opp = _make_safe_opp(yes_quote_age_seconds=ALERT_MAX_QUOTE_AGE_SECONDS + 1)
    assert _alert_is_safe_to_send(opp) is False


def test_gate_rejects_stale_no_quote():
    opp = _make_safe_opp(no_quote_age_seconds=ALERT_MAX_QUOTE_AGE_SECONDS + 1)
    assert _alert_is_safe_to_send(opp) is False


def test_gate_falls_back_to_aggregate_age_when_per_side_missing():
    """Older opportunities (built before per-side ages existed) only have
    quote_age_seconds. Gate must still enforce against the aggregate."""
    opp = _make_safe_opp(
        yes_quote_age_seconds=0.0,
        no_quote_age_seconds=0.0,
        quote_age_seconds=ALERT_MAX_QUOTE_AGE_SECONDS + 5,
    )
    assert _alert_is_safe_to_send(opp) is False


# ── Gate: existing checks still hold ──────────────────────────────────


def test_gate_rejects_unconfirmed_mapping():
    opp = _make_safe_opp(mapping_status="candidate")
    assert _alert_is_safe_to_send(opp) is False


def test_gate_rejects_low_net_edge():
    opp = _make_safe_opp(net_edge_cents=2.9)
    assert _alert_is_safe_to_send(opp) is False


def test_gate_rejects_low_confidence():
    opp = _make_safe_opp(confidence=0.49)
    assert _alert_is_safe_to_send(opp) is False


def test_gate_rejects_non_tradable_status():
    opp = _make_safe_opp(status="review")
    assert _alert_is_safe_to_send(opp) is False


# ── Helpers ───────────────────────────────────────────────────────────


def test_alert_outcome_is_specific_picks_one_side():
    opp = _make_safe_opp(yes_outcome_name="Democrats", no_outcome_name="")
    assert _alert_outcome_is_specific(opp) is True


def test_pick_alert_outcome_prefers_specific_side():
    opp = _make_safe_opp(
        description="U.S Senate Midterm Winner",
        yes_outcome_name="U.S Senate Midterm Winner",  # vague
        no_outcome_name="Democrats win Senate Majority",  # specific
    )
    assert _pick_alert_outcome(opp) == "Democrats win Senate Majority"


# ── Formatter ─────────────────────────────────────────────────────────


def test_format_alert_includes_specific_outcome_in_header():
    opp = _make_safe_opp()
    msg = _format_arb_alert(opp)
    assert "Democrats" in msg
    # Header should not display the vague canonical when something better exists
    header_line = msg.split("\n", 1)[0]
    assert "U.S Senate Midterm Winner" not in header_line


def test_format_alert_includes_market_ids_for_both_legs():
    opp = _make_safe_opp()
    msg = _format_arb_alert(opp)
    assert "KXSENATE-2026-D" in msg
    # Polymarket token may be shortened, so just check a recognizable prefix
    assert "0xabcdef" in msg


def test_format_alert_includes_per_side_quote_age():
    opp = _make_safe_opp(yes_quote_age_seconds=3.0, no_quote_age_seconds=5.0)
    msg = _format_arb_alert(opp)
    assert "3s old" in msg
    assert "5s old" in msg


def test_format_alert_includes_bid_ask_for_both_legs():
    opp = _make_safe_opp(yes_bid=0.50, yes_ask=0.52, no_bid=0.43, no_ask=0.45)
    msg = _format_arb_alert(opp)
    assert "$0.500/$0.520" in msg
    assert "$0.430/$0.450" in msg


def test_format_alert_marks_yes_and_no_sides_explicitly():
    opp = _make_safe_opp()
    msg = _format_arb_alert(opp)
    assert "BUY <b>YES</b>" in msg
    assert "BUY <b>NO</b>" in msg
    assert "KALSHI" in msg
    assert "POLYMARKET" in msg


def test_format_alert_includes_canonical_id_for_audit():
    opp = _make_safe_opp()
    msg = _format_arb_alert(opp)
    assert "DEM_SENATE_2026" in msg


def test_format_alert_shows_net_and_gross_edge():
    opp = _make_safe_opp(gross_edge=0.03, total_fees=0.012, net_edge_cents=3.0)
    msg = _format_arb_alert(opp)
    assert "3.0¢" in msg  # appears for both gross and net
    assert "1.2¢ fees" in msg


def test_format_alert_handles_missing_bid_ask_gracefully():
    opp = _make_safe_opp(yes_bid=0.0, yes_ask=0.0, no_bid=0.0, no_ask=0.0)
    msg = _format_arb_alert(opp)
    assert "n/a" in msg


# ── End-to-end via BalanceMonitor.alert_opportunity ───────────────────


class _FakeCollector:
    async def fetch_balance(self):
        return None


class _CapturingNotifier:
    def __init__(self):
        self.sent: list[str] = []

    async def send(self, message: str, parse_mode: str = "HTML", *, dedup_key=None):
        self.sent.append(message)
        return True

    async def close(self):
        return None


def _make_monitor():
    from arbiter.config.settings import AlertConfig

    cfg = AlertConfig()
    cfg.cooldown = 0.0  # disable cooldown for the test
    monitor = BalanceMonitor(cfg, {"kalshi": _FakeCollector()})
    monitor.notifier = _CapturingNotifier()
    return monitor


def test_alert_opportunity_sends_when_safe():
    async def runner():
        monitor = _make_monitor()
        opp = _make_safe_opp()
        await monitor.alert_opportunity(opp)
        return monitor.notifier.sent

    sent = asyncio.run(runner())
    assert len(sent) == 1
    msg = sent[0]
    assert "Democrats" in msg
    assert "U.S Senate Midterm Winner" not in msg.split("\n", 1)[0]


def test_alert_opportunity_drops_phantom_low_price_arb():
    """Reproduces the original bug: stale Kalshi $0.04 + Polymarket $0.47.
    Gate must reject before any Telegram send happens."""
    async def runner():
        monitor = _make_monitor()
        opp = _make_safe_opp(
            yes_price=0.04,
            no_price=0.47,
            gross_edge=0.49,
            total_fees=0.015,
            net_edge=0.475,
            net_edge_cents=47.5,
            yes_outcome_name="U.S Senate Midterm Winner",
            no_outcome_name="U.S Senate Midterm Winner",
        )
        await monitor.alert_opportunity(opp)
        return monitor.notifier.sent

    sent = asyncio.run(runner())
    assert sent == [], "Phantom-edge alert must be suppressed by the gate"


def test_alert_opportunity_drops_when_outcome_is_only_canonical():
    async def runner():
        monitor = _make_monitor()
        opp = _make_safe_opp(
            yes_outcome_name="U.S Senate Midterm Winner",
            no_outcome_name="U.S Senate Midterm Winner",
        )
        await monitor.alert_opportunity(opp)
        return monitor.notifier.sent

    assert asyncio.run(runner()) == []


def test_alert_opportunity_drops_when_quote_too_old():
    async def runner():
        monitor = _make_monitor()
        opp = _make_safe_opp(yes_quote_age_seconds=120.0)
        await monitor.alert_opportunity(opp)
        return monitor.notifier.sent

    assert asyncio.run(runner()) == []
