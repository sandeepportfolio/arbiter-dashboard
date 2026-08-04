"""Tests for the ClawArbs feed consumer.

Non-negotiables under test: value_bets are NEVER consumed; only
ARB_DETECTED two-leg entries with a Kalshi anchor parse; injection goes
through monitor.alert_opportunity with an opportunity priced from OUR
books; unmapped tickers become discovery candidates; edge floor enforced;
dedup prevents re-injection.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from arbiter.feeds.clawarbs import ClawArbsFeed, FeedArb, parse_feed

PAYLOAD = {
    "fetched_at": "2026-08-03T23:00:00+00:00",
    "count": 3,
    "opportunities": [
        {
            "type": "ARB_DETECTED",
            "opportunity_id": "kalshi:KXWTAMATCH-X|polymarket:123",
            "match_name": "A v B",
            "market_type": "moneyline_2way",
            "edge_net_pct": 5.2,
            "legs": [
                {"venue": "kalshi", "external_id": "KXWTAMATCH-X",
                 "side": "YES", "price": 0.40},
                {"venue": "polymarket", "external_id": "123",
                 "side": "NO", "price": 0.52},
            ],
        },
        {
            "type": "ARB_DETECTED",
            "opportunity_id": "polymarket:9|predictiv:8",
            "match_name": "C v D",
            "market_type": "moneyline_2way",
            "edge_net_pct": 7.0,
            "legs": [
                {"venue": "polymarket", "external_id": "9",
                 "side": "YES", "price": 0.30},
                {"venue": "predictiv", "external_id": "8",
                 "side": "NO", "price": 0.60},
            ],
        },
        {
            "type": "SOMETHING_ELSE",
            "opportunity_id": "x",
            "legs": [],
        },
    ],
    "value_bet_count": 2,
    "value_bets": [
        {"type": "VALUE_BET", "value_bet_id": "vb|1",
         "leg": {"venue": "kalshi", "external_id": "KXVB-1"}},
        {"type": "VALUE_BET", "value_bet_id": "vb|2",
         "leg": {"venue": "polymarket", "external_id": "999"}},
    ],
}


def test_parse_feed_takes_only_kalshi_anchored_arbs():
    arbs = parse_feed(PAYLOAD)
    # The predictiv/polymarket pair has no Kalshi anchor; the non-ARB type
    # is dropped; ONLY the kalshi-anchored arb survives.
    assert len(arbs) == 1
    assert arbs[0].kalshi_ticker == "KXWTAMATCH-X"
    assert arbs[0].feed_edge_net_pct == 5.2


def test_parse_feed_never_touches_value_bets():
    """A payload whose value_bets contain ARB_DETECTED-shaped garbage must
    still yield nothing from that array — the parser must not read it."""
    poisoned = dict(PAYLOAD)
    poisoned["opportunities"] = []
    poisoned["value_bets"] = [{
        "type": "ARB_DETECTED",
        "opportunity_id": "vb-disguised",
        "legs": [
            {"venue": "kalshi", "external_id": "KXTRAP", "side": "YES",
             "price": 0.1},
            {"venue": "polymarket", "external_id": "1", "side": "NO",
             "price": 0.2},
        ],
    }]
    assert parse_feed(poisoned) == []


def _feed(monitor=None, mapping=None, opp=None, add_candidate=None):
    monitor = monitor or SimpleNamespace(alert_opportunity=AsyncMock())
    store = SimpleNamespace(
        get_by_platform=AsyncMock(return_value=mapping),
        add_candidate=add_candidate or AsyncMock(),
    )
    scanner = SimpleNamespace(
        config=SimpleNamespace(min_edge_cents=7.0),
        build_opportunity_for_canonical=AsyncMock(return_value=opp),
    )
    feed = ClawArbsFeed(
        monitor=monitor, price_store=None,
        mapping_store=store, scanner=scanner,
    )
    feed.fetch_feed = AsyncMock(return_value=PAYLOAD)
    return feed, monitor, store, scanner


def test_mapped_confirmed_arb_injects_through_monitor():
    mapping = SimpleNamespace(
        canonical_id="WTA_X", status="confirmed", allow_auto_trade=True,
    )
    opp = SimpleNamespace(net_edge_cents=8.4)
    feed, monitor, store, scanner = _feed(mapping=mapping, opp=opp)
    asyncio.run(feed.poll_once())
    monitor.alert_opportunity.assert_awaited_once_with(opp)
    assert feed.stats["injected"] == 1
    # Dedup: same opportunity_id must not re-inject on the next poll.
    asyncio.run(feed.poll_once())
    assert monitor.alert_opportunity.await_count == 1


def test_below_floor_is_not_injected():
    mapping = SimpleNamespace(
        canonical_id="WTA_X", status="confirmed", allow_auto_trade=True,
    )
    opp = SimpleNamespace(net_edge_cents=3.0)
    feed, monitor, *_ = _feed(mapping=mapping, opp=opp)
    asyncio.run(feed.poll_once())
    monitor.alert_opportunity.assert_not_awaited()
    assert feed.stats["below_floor"] == 1


def test_custom_edge_floor_env(monkeypatch):
    monkeypatch.setenv("CLAWARBS_MIN_EDGE_CENTS", "2.5")
    mapping = SimpleNamespace(
        canonical_id="WTA_X", status="confirmed", allow_auto_trade=True,
    )
    opp = SimpleNamespace(net_edge_cents=3.0)
    feed, monitor, *_ = _feed(mapping=mapping, opp=opp)
    asyncio.run(feed.poll_once())
    monitor.alert_opportunity.assert_awaited_once()


def test_unmapped_ticker_becomes_candidate():
    add_candidate = AsyncMock()
    feed, monitor, store, _ = _feed(mapping=None, add_candidate=add_candidate)
    asyncio.run(feed.poll_once())
    monitor.alert_opportunity.assert_not_awaited()
    add_candidate.assert_awaited_once()
    kwargs = add_candidate.await_args.kwargs
    assert kwargs["platform"] == "kalshi"
    assert kwargs["platform_market_id"] == "KXWTAMATCH-X"
    assert feed.stats["unmapped"] == 1
    assert feed.stats["candidates_written"] == 1


def test_pseudo_ticker_never_becomes_candidate():
    """Feed pseudo-ids (kalshi:san-francisco-v-texas:NO) can never match a
    real Kalshi market — they must not litter the review conveyor."""
    payload = {
        "opportunities": [{
            "type": "ARB_DETECTED",
            "opportunity_id": "kalshi:sf-v-tex|polymarket:1",
            "match_name": "SF v TEX",
            "market_type": "moneyline_2way",
            "edge_net_pct": 4.0,
            "legs": [
                {"venue": "kalshi", "external_id": "kalshi:san-francisco-v-texas:NO",
                 "side": "NO", "price": 0.13},
                {"venue": "polymarket", "external_id": "1",
                 "side": "YES", "price": 0.84},
            ],
        }],
    }
    add_candidate = AsyncMock()
    feed, monitor, *_ = _feed(mapping=None, add_candidate=add_candidate)
    feed.fetch_feed = AsyncMock(return_value=payload)
    asyncio.run(feed.poll_once())
    add_candidate.assert_not_awaited()
    assert feed.stats["candidates_written"] == 0


def test_non_auto_trade_mapping_not_injected():
    mapping = SimpleNamespace(
        canonical_id="WTA_X", status="confirmed", allow_auto_trade=False,
    )
    feed, monitor, *_ = _feed(mapping=mapping, opp=SimpleNamespace(net_edge_cents=9.0))
    asyncio.run(feed.poll_once())
    monitor.alert_opportunity.assert_not_awaited()


def test_injection_sends_claw_telegram_alert():
    """Operator directive: Claw trades must reach the Telegram alerts
    channel — an injection pushes a tagged alert (and never blocks
    queueing if the notifier fails)."""
    mapping = SimpleNamespace(
        canonical_id="WTA_X", status="confirmed", allow_auto_trade=True,
    )
    opp = SimpleNamespace(net_edge_cents=8.4)
    notifier = SimpleNamespace(send=AsyncMock())
    monitor = SimpleNamespace(
        alert_opportunity=AsyncMock(), notifier=notifier,
    )
    feed, _m, *_ = _feed(monitor=monitor, mapping=mapping, opp=opp)
    asyncio.run(feed.poll_once())
    monitor.alert_opportunity.assert_awaited_once()
    notifier.send.assert_awaited_once()
    msg = notifier.send.await_args.args[0]
    assert "CLAW FEED ARB QUEUED" in msg
    assert getattr(opp, "source", "") == "clawarbs"


def test_notifier_failure_does_not_block_injection():
    mapping = SimpleNamespace(
        canonical_id="WTA_X", status="confirmed", allow_auto_trade=True,
    )
    opp = SimpleNamespace(net_edge_cents=8.4)
    notifier = SimpleNamespace(send=AsyncMock(side_effect=RuntimeError("down")))
    monitor = SimpleNamespace(
        alert_opportunity=AsyncMock(), notifier=notifier,
    )
    feed, _m, *_ = _feed(monitor=monitor, mapping=mapping, opp=opp)
    asyncio.run(feed.poll_once())
    monitor.alert_opportunity.assert_awaited_once()
    assert feed.stats["injected"] == 1
