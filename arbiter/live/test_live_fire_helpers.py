"""Unit tests for arbiter.live.live_fire_helpers (B-2 + B-3 resolution).

Non-live — mocked PriceStore + mocked adapter clients only; no network I/O.
Verifies:
  1-4. build_opportunity_from_quotes (B-3): None when quotes missing / single platform,
       valid opp on tradable cross, None when suggested_qty exceeds per_leg_cap_usd.
  5-6. fetch_kalshi_platform_fee (B-2): calls adapter.session.get with /portfolio/fills
       and sums fee_cents/100.0; 0.0 when no fills.
  7-8. fetch_polymarket_platform_fee (B-2): calls adapter client's get_trades(market=...)
       and sums fees; AssertionError when condition_id cannot be resolved.
  9-10. write_pre_trade_requote (W-3): emits valid JSON with original + requoted keys.

AsyncMock.assert_awaited asserts each helper really hits the underlying adapter path —
the anti-pattern being defended against is a NotImplementedError stub that silently
passes reconcile. Every helper here must prove it calls the real platform client.
"""
from __future__ import annotations

import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from arbiter.live.live_fire_helpers import (
    POLYGON_SETTLEMENT_WAIT_SECONDS,
    PRE_EXECUTION_OPERATOR_ABORT_SECONDS,
    TEST_PER_LEG_USD_CEILING,
    build_opportunity_from_quotes,
    fetch_kalshi_platform_fee,
    fetch_polymarket_platform_fee,
    write_pre_trade_requote,
)
from arbiter.utils.price_store import PricePoint


# ─── Module constants ────────────────────────────────────────────────────────


def test_module_constants_are_set():
    """W-6 + RESEARCH Q5: 60s operator-abort + 60s Polygon settlement."""
    assert PRE_EXECUTION_OPERATOR_ABORT_SECONDS == 60.0, (
        "W-6: CLAUDE.md 'Safety > speed' requires 60s operator-abort, "
        f"got {PRE_EXECUTION_OPERATOR_ABORT_SECONDS}"
    )
    assert POLYGON_SETTLEMENT_WAIT_SECONDS == 60.0, (
        "RESEARCH Q5 decision: 60s Polygon settlement wait, "
        f"got {POLYGON_SETTLEMENT_WAIT_SECONDS}"
    )
    assert TEST_PER_LEG_USD_CEILING == 10.0, (
        "Belt above PHASE5_MAX_ORDER_USD ($10); got "
        f"{TEST_PER_LEG_USD_CEILING}"
    )


# ─── Helpers for build_opportunity_from_quotes tests ─────────────────────────


def _price_point(
    *,
    platform: str,
    canonical_id: str = "CAN-X",
    yes_price: float,
    no_price: float,
    yes_volume: float = 100.0,
    no_volume: float = 100.0,
    fee_rate: float = 0.0,
):
    return PricePoint(
        platform=platform,
        canonical_id=canonical_id,
        yes_price=yes_price,
        no_price=no_price,
        yes_volume=yes_volume,
        no_volume=no_volume,
        timestamp=time.time(),
        raw_market_id=f"{platform}-raw",
        yes_market_id=f"{platform}-yes",
        no_market_id=f"{platform}-no",
        fee_rate=fee_rate,
        mapping_status="confirmed",
        mapping_score=0.9,
    )


def _mock_price_store(snapshot_result):
    """Build a mock PriceStore whose get_all_for_market is an AsyncMock."""
    store = MagicMock()
    store.get_all_for_market = AsyncMock(return_value=snapshot_result)
    return store


# ─── Test 1 — build_opportunity_from_quotes: empty snapshot ──────────────────


async def test_build_opportunity_returns_none_when_no_quotes():
    """No quotes at all -> None (not an exception)."""
    store = _mock_price_store({})
    result = await build_opportunity_from_quotes(
        store, "CAN-X", per_leg_cap_usd=10.0,
    )
    assert result is None
    store.get_all_for_market.assert_awaited_with("CAN-X")


# ─── Test 2 — build_opportunity_from_quotes: single platform ─────────────────


async def test_build_opportunity_returns_none_with_single_platform():
    """Only one platform has quotes -> no cross-platform arb possible -> None."""
    snapshot = {
        "kalshi": _price_point(platform="kalshi", yes_price=0.55, no_price=0.44),
    }
    store = _mock_price_store(snapshot)
    result = await build_opportunity_from_quotes(
        store, "CAN-X", per_leg_cap_usd=10.0,
    )
    assert result is None


# ─── Test 3 — build_opportunity_from_quotes: tradable cross ──────────────────


async def test_build_opportunity_returns_valid_opp_for_tradable_cross():
    """Cross-platform edge above the 1¢ floor and within $10 notional -> ArbitrageOpportunity."""
    # Kalshi yes=0.50, Polymarket no=0.45 -> gross 0.05 before fees.
    # With small fees this should clear the 1¢ min_edge_cents and the $10 per-leg cap.
    snapshot = {
        "kalshi": _price_point(
            platform="kalshi", yes_price=0.50, no_price=0.51,
            yes_volume=20.0, no_volume=20.0,
        ),
        "polymarket": _price_point(
            platform="polymarket", yes_price=0.56, no_price=0.45,
            yes_volume=20.0, no_volume=20.0, fee_rate=0.02,
        ),
    }
    store = _mock_price_store(snapshot)
    opp = await build_opportunity_from_quotes(
        store, "CAN-X", per_leg_cap_usd=10.0,
    )
    assert opp is not None, "expected a tradable opportunity from the cross"
    assert opp.canonical_id == "CAN-X"
    assert opp.net_edge_cents > 0
    # The builder must pick the higher-edge pairing. At least one leg is kalshi-yes
    # and one is polymarket-no (or vice versa — the direction depends on fees).
    platforms = {opp.yes_platform, opp.no_platform}
    assert platforms == {"kalshi", "polymarket"}
    # Per-leg notional cap
    assert opp.suggested_qty * opp.yes_price <= 10.0 + 1e-6
    assert opp.suggested_qty * opp.no_price <= 10.0 + 1e-6


# ─── Test 4 — build_opportunity_from_quotes: over per-leg cap ────────────────


async def test_build_opportunity_returns_none_when_suggested_qty_exceeds_cap():
    """Even with a large tradable edge, suggested_qty*price must stay <= per_leg_cap_usd.

    We set per_leg_cap_usd VERY small ($0.10) — any suggested_qty >= 1 at any price >= $0.10
    would exceed the cap, so the helper must return None.
    """
    snapshot = {
        "kalshi": _price_point(
            platform="kalshi", yes_price=0.50, no_price=0.51,
            yes_volume=100.0, no_volume=100.0,
        ),
        "polymarket": _price_point(
            platform="polymarket", yes_price=0.56, no_price=0.45,
            yes_volume=100.0, no_volume=100.0,
        ),
    }
    store = _mock_price_store(snapshot)
    # per_leg_cap_usd = $0.10 (lower than any realistic single-contract notional)
    opp = await build_opportunity_from_quotes(
        store, "CAN-X", per_leg_cap_usd=0.10,
    )
    assert opp is None, (
        "expected None: suggested_qty=1 at price=$0.45+ would exceed $0.10 cap; "
        f"got opp={opp!r}"
    )


# ─── Test 5 — fetch_kalshi_platform_fee: happy path ──────────────────────────


def _make_kalshi_adapter_with_fills_response(body: dict):
    """Build a MagicMock Kalshi adapter whose .session.get returns `body` on json()."""
    # Build the async-context-manager response mock.
    response = MagicMock()
    response.json = AsyncMock(return_value=body)
    response.raise_for_status = MagicMock(return_value=None)

    get_cm = MagicMock()
    get_cm.__aenter__ = AsyncMock(return_value=response)
    get_cm.__aexit__ = AsyncMock(return_value=False)

    session = MagicMock()
    session.get = MagicMock(return_value=get_cm)

    auth = MagicMock()
    auth.get_headers = MagicMock(return_value={"KALSHI-ACCESS-KEY": "stub"})

    config = SimpleNamespace(kalshi=SimpleNamespace(
        base_url="https://api.elections.kalshi.com/trade-api/v2",
    ))

    adapter = SimpleNamespace(
        session=session,
        auth=auth,
        config=config,
    )
    return adapter, session, auth


async def test_fetch_kalshi_platform_fee_happy_path_sums_fee_cents():
    """Sums fee_cents across fills matching order_id; ignores other orders; returns USD."""
    body = {
        "fills": [
            {"order_id": "abc", "fee_cents": 23},
            {"order_id": "other-order", "fee_cents": 99},
            {"order_id": "abc", "fee_cents": 7},
        ],
    }
    adapter, session, auth = _make_kalshi_adapter_with_fills_response(body)

    fee = await fetch_kalshi_platform_fee(adapter, "abc")

    # 23 + 7 = 30 cents = $0.30
    assert abs(fee - 0.30) < 1e-9, f"expected 0.30, got {fee}"

    # Proves the helper actually called the Kalshi fills endpoint (B-2 anti-stub).
    session.get.assert_called_once()
    call = session.get.call_args
    called_url = call.args[0] if call.args else call.kwargs.get("url", "")
    assert called_url.endswith("/portfolio/fills"), (
        f"expected URL ending in /portfolio/fills, got {called_url!r}"
    )
    params = call.kwargs.get("params") or {}
    assert params.get("order_id") == "abc", (
        f"expected order_id=abc in params, got {params!r}"
    )
    # Auth headers must be fetched (authenticated request).
    auth.get_headers.assert_called_once()


# ─── Test 6 — fetch_kalshi_platform_fee: no fills ────────────────────────────


async def test_fetch_kalshi_platform_fee_returns_zero_when_no_fills():
    """Empty fills list -> 0.0 (not None, not exception)."""
    adapter, _, _ = _make_kalshi_adapter_with_fills_response({"fills": []})
    fee = await fetch_kalshi_platform_fee(adapter, "abc")
    assert fee == 0.0


# ─── Test 7 — fetch_polymarket_platform_fee: happy path ──────────────────────


def _make_polymarket_adapter_with_trades_response(trades, condition_id="0xabcd"):
    """Build a mock Polymarket adapter exposing _get_client() -> clob with get_trades.

    The real PolymarketAdapter uses a `clob_client_factory` callable stored at
    `self._get_client`. We mirror that shape so the helper is drop-in.
    """
    clob = MagicMock()
    clob.get_trades = MagicMock(return_value=trades)

    adapter = SimpleNamespace(
        _get_client=lambda: clob,
        # Condition-id lookup cache: helper falls back to this when Polymarket's
        # order record does not carry the condition_id directly.
        _order_condition_index={"abc": condition_id},
    )
    return adapter, clob


async def test_fetch_polymarket_platform_fee_happy_path_sums_fees():
    """Sums fee_usd on trades matching order_id; calls get_trades(market=condition_id)."""
    trades = [
        {"order_id": "abc", "fee_usd": 0.04},
        {"order_id": "other", "fee_usd": 0.99},
        {"order_id": "abc", "fee_usd": 0.02},
    ]
    adapter, clob = _make_polymarket_adapter_with_trades_response(trades)

    fee = await fetch_polymarket_platform_fee(adapter, "abc")

    assert abs(fee - 0.06) < 1e-9, f"expected 0.06, got {fee}"
    clob.get_trades.assert_called_once()
    call_kwargs = clob.get_trades.call_args.kwargs
    assert call_kwargs.get("market") == "0xabcd", (
        f"expected market='0xabcd' in get_trades kwargs, got {call_kwargs!r}"
    )


# ─── Test 8 — fetch_polymarket_platform_fee: missing condition_id ────────────


async def test_fetch_polymarket_platform_fee_raises_when_condition_id_missing():
    """No cached condition_id for this order_id -> AssertionError with helpful message."""
    # Build an adapter whose _order_condition_index is empty -> helper cannot resolve.
    clob = MagicMock()
    clob.get_trades = MagicMock(return_value=[])
    adapter = SimpleNamespace(
        _get_client=lambda: clob,
        _order_condition_index={},  # empty — cannot look up order_id -> condition_id
    )

    with pytest.raises(AssertionError) as excinfo:
        await fetch_polymarket_platform_fee(adapter, "abc")
    msg = str(excinfo.value)
    assert "condition_id" in msg.lower(), (
        f"expected error message to mention 'condition_id'; got {msg!r}"
    )
    # Critical: when the cache miss is a startup bug (adapter did not persist
    # the mapping), we MUST NOT silently return 0.0 and mask a reconcile breach.
    clob.get_trades.assert_not_called()


# ─── Test 9 — write_pre_trade_requote: single opp ───────────────────────────


def test_write_pre_trade_requote_writes_valid_json(tmp_path):
    """Helper emits a JSON file with at least a 'requoted' key."""
    requoted = SimpleNamespace()
    requoted.to_dict = lambda: {
        "canonical_id": "CAN-X",
        "net_edge_cents": 1.75,
    }

    out_path = write_pre_trade_requote(tmp_path, requoted)

    assert out_path.exists()
    assert out_path.name == "pre_trade_requote.json"
    payload = json.loads(out_path.read_text())
    assert "requoted" in payload
    assert payload["requoted"]["canonical_id"] == "CAN-X"


# ─── Test 10 — write_pre_trade_requote: original + requoted ──────────────────


def test_write_pre_trade_requote_includes_original_when_provided(tmp_path):
    """When original_opp is passed, JSON has both 'original' and 'requoted' keys."""
    original = SimpleNamespace()
    original.to_dict = lambda: {"canonical_id": "CAN-X", "net_edge_cents": 2.0}
    requoted = SimpleNamespace()
    requoted.to_dict = lambda: {"canonical_id": "CAN-X", "net_edge_cents": 1.75}

    out_path = write_pre_trade_requote(tmp_path, requoted, original_opp=original)

    payload = json.loads(out_path.read_text())
    assert "original" in payload
    assert "requoted" in payload
    assert payload["original"]["net_edge_cents"] == 2.0
    assert payload["requoted"]["net_edge_cents"] == 1.75
