"""Tests for arbiter.execution.adapters.kalshi.KalshiAdapter (EXEC-01, EXEC-03, EXEC-04).

Covers:
- Protocol conformance (runtime isinstance)
- FOK order body shape (time_in_force, count_fp, yes/no_price_dollars, client_order_id)
- Status mapping (executed/canceled/pending/resting)
- Refusal paths (no auth / invalid price / circuit open) — never touches the wire
- Error paths (non-2xx / exception) — returns FAILED without raising
- check_depth (sufficient / insufficient / empty book / non-200)
- cancel_order (200 / 204 / 404 / no auth)
"""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from arbiter.execution.adapters import PlatformAdapter
from arbiter.execution.adapters.kalshi import KalshiAdapter
from arbiter.execution.engine import Order, OrderStatus


# ─── Fixtures ─────────────────────────────────────────────────────────────

def _config():
    cfg = SimpleNamespace()
    cfg.kalshi = SimpleNamespace(
        base_url="https://api.elections.kalshi.test/trade-api/v2",
    )
    return cfg


def _auth(authenticated: bool = True):
    auth = MagicMock()
    auth.is_authenticated = authenticated
    auth.get_headers = MagicMock(return_value={"Authorization": "test-sig"})
    return auth


def _circuit(can_execute: bool = True):
    circuit = MagicMock()
    circuit.can_execute = MagicMock(return_value=can_execute)
    circuit.record_success = MagicMock()
    circuit.record_failure = MagicMock()
    return circuit


def _rate_limiter():
    rl = MagicMock()
    rl.acquire = AsyncMock(return_value=None)
    return rl


def _session_with_post(status: int, body_text: str):
    """MagicMock session whose .post(...) async-context-manager returns a response."""
    session = MagicMock()
    resp = MagicMock()
    resp.status = status
    resp.text = AsyncMock(return_value=body_text)
    session.post.return_value.__aenter__ = AsyncMock(return_value=resp)
    session.post.return_value.__aexit__ = AsyncMock(return_value=False)
    return session


def _session_with_get(status: int, body_text: str):
    session = MagicMock()
    resp = MagicMock()
    resp.status = status
    resp.text = AsyncMock(return_value=body_text)
    session.get.return_value.__aenter__ = AsyncMock(return_value=resp)
    session.get.return_value.__aexit__ = AsyncMock(return_value=False)
    return session


def _session_with_delete(status: int):
    session = MagicMock()
    resp = MagicMock()
    resp.status = status
    session.delete.return_value.__aenter__ = AsyncMock(return_value=resp)
    session.delete.return_value.__aexit__ = AsyncMock(return_value=False)
    return session


def _make_adapter(session, *, authenticated: bool = True, can_execute: bool = True):
    return KalshiAdapter(
        config=_config(),
        session=session,
        auth=_auth(authenticated),
        rate_limiter=_rate_limiter(),
        circuit=_circuit(can_execute),
    )


# ─── Protocol conformance ────────────────────────────────────────────────

def test_kalshi_adapter_satisfies_protocol():
    adapter = _make_adapter(_session_with_post(200, "{}"))
    assert isinstance(adapter, PlatformAdapter), \
        "KalshiAdapter must satisfy PlatformAdapter Protocol via structural typing"


# ─── place_fok body shape ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_fok_request_body_shape_yes_side():
    body = json.dumps({
        "order": {
            "order_id": "K-1",
            "status": "executed",
            "fill_count_fp": "10.00",
            "yes_price_dollars": "0.5500",
        },
    })
    session = _session_with_post(200, body)
    adapter = _make_adapter(session)
    order = await adapter.place_fok("ARB-000001", "TICKER", "DEM_HOUSE", "yes", 0.55, 10)

    called_kwargs = session.post.call_args.kwargs
    posted = called_kwargs["json"]
    assert posted["time_in_force"] == "fill_or_kill"
    assert posted["count_fp"] == "10.00"
    assert posted["yes_price_dollars"] == "0.5500"
    assert "no_price_dollars" not in posted
    assert posted["action"] == "buy"
    assert posted["type"] == "limit"
    assert posted["side"] == "yes"
    assert posted["ticker"] == "TICKER"
    assert posted["client_order_id"].startswith("ARB-000001-YES-")
    assert order.status == OrderStatus.FILLED
    assert order.fill_qty == 10.0


@pytest.mark.asyncio
async def test_fok_request_body_shape_no_side():
    body = json.dumps({
        "order": {
            "order_id": "K-2",
            "status": "executed",
            "fill_count_fp": "5.00",
            "no_price_dollars": "0.4500",
        },
    })
    session = _session_with_post(200, body)
    adapter = _make_adapter(session)
    await adapter.place_fok("ARB-000002", "TICKER", "DEM", "no", 0.45, 5)
    posted = session.post.call_args.kwargs["json"]
    assert posted["no_price_dollars"] == "0.4500"
    assert "yes_price_dollars" not in posted
    assert posted["client_order_id"].startswith("ARB-000002-NO-")
    assert posted["time_in_force"] == "fill_or_kill"


# ─── Status mapping ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_fok_full_fill():
    body = json.dumps({
        "order": {
            "order_id": "K-1",
            "status": "executed",
            "fill_count_fp": "10.00",
            "yes_price_dollars": "0.5500",
        },
    })
    adapter = _make_adapter(_session_with_post(200, body))
    order = await adapter.place_fok("ARB-1", "T", "C", "yes", 0.55, 10)
    assert order.status == OrderStatus.FILLED
    assert order.fill_qty == 10.0


@pytest.mark.asyncio
async def test_fok_cancelled():
    body = json.dumps({
        "order": {"order_id": "K-2", "status": "canceled", "fill_count_fp": "0"},
    })
    adapter = _make_adapter(_session_with_post(200, body))
    order = await adapter.place_fok("ARB-2", "T", "C", "yes", 0.55, 10)
    assert order.status == OrderStatus.CANCELLED


@pytest.mark.asyncio
async def test_fok_pending():
    body = json.dumps({
        "order": {"order_id": "K-3", "status": "pending", "fill_count_fp": "0"},
    })
    adapter = _make_adapter(_session_with_post(200, body))
    order = await adapter.place_fok("ARB-3", "T", "C", "yes", 0.55, 10)
    assert order.status == OrderStatus.PENDING


@pytest.mark.asyncio
async def test_fok_unexpected_resting_does_not_raise():
    body = json.dumps({
        "order": {"order_id": "K-4", "status": "resting", "fill_count_fp": "0"},
    })
    adapter = _make_adapter(_session_with_post(200, body))
    order = await adapter.place_fok("ARB-4", "T", "C", "yes", 0.55, 10)
    assert order.status == OrderStatus.SUBMITTED


# ─── Refusal paths (no HTTP call) ────────────────────────────────────────

@pytest.mark.asyncio
async def test_fok_rejects_when_no_auth():
    session = _session_with_post(200, "{}")
    adapter = _make_adapter(session, authenticated=False)
    order = await adapter.place_fok("ARB-X", "T", "C", "yes", 0.55, 10)
    assert order.status == OrderStatus.FAILED
    assert "Kalshi auth not configured" in order.error
    assert not session.post.called


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_price", [0, 1, -0.01, 1.5])
async def test_fok_rejects_invalid_price(bad_price):
    session = _session_with_post(200, "{}")
    adapter = _make_adapter(session)
    order = await adapter.place_fok("ARB-X", "T", "C", "yes", bad_price, 10)
    assert order.status == OrderStatus.FAILED
    assert "Invalid price" in order.error
    assert not session.post.called


@pytest.mark.asyncio
async def test_fok_circuit_open_short_circuits():
    session = _session_with_post(200, "{}")
    adapter = _make_adapter(session, can_execute=False)
    order = await adapter.place_fok("ARB-X", "T", "C", "yes", 0.55, 10)
    assert order.status == OrderStatus.FAILED
    assert "circuit open" in order.error.lower()
    assert not session.post.called


# ─── Error paths (never raises) ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_fok_non_2xx_returns_failed_with_status_in_error():
    # 500 exercises the generic non-2xx branch. 429 has dedicated handling
    # (SAFE-04) verified by test_place_fok_429_applies_retry_after.
    session = _session_with_post(500, "internal server error")
    adapter = _make_adapter(session)
    order = await adapter.place_fok("ARB-Y", "T", "C", "yes", 0.55, 10)
    assert order.status == OrderStatus.FAILED
    assert "500" in order.error
    assert "internal server error" in order.error
    adapter.circuit.record_failure.assert_called()


@pytest.mark.asyncio
async def test_fok_exception_returns_failed():
    session = MagicMock()
    session.post.side_effect = RuntimeError("network melted")
    adapter = _make_adapter(session)
    order = await adapter.place_fok("ARB-Z", "T", "C", "yes", 0.55, 10)
    assert order.status == OrderStatus.FAILED
    assert "Kalshi request exception" in order.error
    assert "network melted" in order.error
    adapter.circuit.record_failure.assert_called()


@pytest.mark.asyncio
async def test_fok_success_records_circuit_success():
    body = json.dumps({
        "order": {
            "order_id": "K-OK",
            "status": "executed",
            "fill_count_fp": "10.00",
            "yes_price_dollars": "0.5500",
        },
    })
    adapter = _make_adapter(_session_with_post(200, body))
    order = await adapter.place_fok("ARB-OK", "T", "C", "yes", 0.55, 10)
    assert order.status == OrderStatus.FILLED
    adapter.circuit.record_success.assert_called()


# ─── check_depth ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_check_depth_sufficient():
    body = json.dumps({"orderbook": {"yes": [[55, 5], [56, 10], [57, 20]]}})
    session = _session_with_get(200, body)
    adapter = _make_adapter(session)
    sufficient, best = await adapter.check_depth("TICKER", "yes", required_qty=10)
    assert sufficient is True
    assert abs(best - 0.55) < 1e-9


@pytest.mark.asyncio
async def test_check_depth_insufficient():
    body = json.dumps({"orderbook": {"yes": [[55, 3], [56, 4]]}})
    session = _session_with_get(200, body)
    adapter = _make_adapter(session)
    sufficient, _ = await adapter.check_depth("TICKER", "yes", required_qty=10)
    assert sufficient is False


@pytest.mark.asyncio
async def test_check_depth_empty_book():
    body = json.dumps({"orderbook": {"yes": []}})
    session = _session_with_get(200, body)
    adapter = _make_adapter(session)
    sufficient, best = await adapter.check_depth("TICKER", "yes", required_qty=1)
    assert sufficient is False
    assert best == 0.0


@pytest.mark.asyncio
async def test_check_depth_non_200():
    session = _session_with_get(404, "")
    adapter = _make_adapter(session)
    sufficient, best = await adapter.check_depth("TICKER", "yes", required_qty=10)
    assert sufficient is False
    assert best == 0.0


# ─── best_executable_price ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_best_executable_price_returns_level_price_when_sweeping_book():
    """3 contracts at 55¢ + 4 at 56¢ + 3 at 57¢ = 10 needed; FOK must price at
    57¢ to absorb the full 10 contracts. Returning 55¢ (top of book) would
    cause Kalshi to reject with fill_or_kill_insufficient_resting_volume."""
    body = json.dumps({"orderbook": {"yes": [[55, 3], [56, 4], [57, 5]]}})
    session = _session_with_get(200, body)
    adapter = _make_adapter(session)
    fillable, price = await adapter.best_executable_price("T", "yes", required_qty=10)
    assert fillable is True
    assert abs(price - 0.57) < 1e-9


@pytest.mark.asyncio
async def test_best_executable_price_returns_top_when_top_level_sufficient():
    body = json.dumps({"orderbook": {"yes": [[55, 50], [56, 10]]}})
    session = _session_with_get(200, body)
    adapter = _make_adapter(session)
    fillable, price = await adapter.best_executable_price("T", "yes", required_qty=10)
    assert fillable is True
    assert abs(price - 0.55) < 1e-9


@pytest.mark.asyncio
async def test_best_executable_price_insufficient_returns_false():
    body = json.dumps({"orderbook": {"yes": [[55, 3], [56, 2]]}})
    session = _session_with_get(200, body)
    adapter = _make_adapter(session)
    fillable, price = await adapter.best_executable_price("T", "yes", required_qty=10)
    assert fillable is False
    assert abs(price - 0.56) < 1e-9


@pytest.mark.asyncio
async def test_best_executable_price_empty_book():
    body = json.dumps({"orderbook": {"yes": []}})
    session = _session_with_get(200, body)
    adapter = _make_adapter(session)
    fillable, price = await adapter.best_executable_price("T", "yes", required_qty=1)
    assert fillable is False
    assert price == 0.0


@pytest.mark.asyncio
async def test_best_executable_price_non_200_returns_false():
    session = _session_with_get(500, "")
    adapter = _make_adapter(session)
    fillable, price = await adapter.best_executable_price("T", "yes", required_qty=10)
    assert fillable is False
    assert price == 0.0


# ─── orderbook_fp (current Kalshi shape) ──────────────────────────────────
#
# Live Kalshi (post-2026 fixed-point migration) returns
# ``{"orderbook_fp": {"yes_dollars": [...], "no_dollars": [...]}}`` where
# each side is a list of BIDS for that side (highest price = best bid).
# To buy YES we walk the NO bids: yes_ask_price = 1 - no_bid_price, with
# liquidity = qty at that NO bid.  These tests pin the new semantics so the
# scanner→executor pipeline never silently regresses to ``best_price=0.0``.


def _orderbook_fp_body(yes_dollars=None, no_dollars=None):
    """Construct the live ``orderbook_fp`` payload shape Kalshi returns."""
    return json.dumps({
        "orderbook_fp": {
            "yes_dollars": yes_dollars or [],
            "no_dollars": no_dollars or [],
        }
    })


@pytest.mark.asyncio
async def test_check_depth_orderbook_fp_buying_yes_walks_no_bids():
    # NO bids at $0.15 (250 ct), $0.16 (300 ct), $0.17 (1000 ct).
    # Best NO bid = $0.17 → YES ask = 1 - 0.17 = $0.83.
    # Required qty 200: top NO bid alone (1000 ct) suffices.
    body = _orderbook_fp_body(no_dollars=[
        ["0.15", "250"],
        ["0.16", "300"],
        ["0.17", "1000"],
    ])
    adapter = _make_adapter(_session_with_get(200, body))
    sufficient, best = await adapter.check_depth("T", "yes", required_qty=200)
    assert sufficient is True
    assert abs(best - 0.83) < 1e-9


@pytest.mark.asyncio
async def test_check_depth_orderbook_fp_buying_no_walks_yes_bids():
    # YES bids at $0.80 (50 ct), $0.81 (60 ct), $0.82 (70 ct).
    # Best YES bid = $0.82 → NO ask = 1 - 0.82 = $0.18.
    body = _orderbook_fp_body(yes_dollars=[
        ["0.80", "50"],
        ["0.81", "60"],
        ["0.82", "70"],
    ])
    adapter = _make_adapter(_session_with_get(200, body))
    sufficient, best = await adapter.check_depth("T", "no", required_qty=70)
    assert sufficient is True
    assert abs(best - 0.18) < 1e-9


@pytest.mark.asyncio
async def test_check_depth_orderbook_fp_insufficient_returns_best_price():
    # Top NO bid is $0.17 with only 100 ct, plus $0.16 with 50 ct.
    # 150 ct total < required 500 → insufficient, but best YES ask
    # ($0.83) must still be reported so the caller logs a useful number.
    body = _orderbook_fp_body(no_dollars=[
        ["0.16", "50"],
        ["0.17", "100"],
    ])
    adapter = _make_adapter(_session_with_get(200, body))
    sufficient, best = await adapter.check_depth("T", "yes", required_qty=500)
    assert sufficient is False
    assert abs(best - 0.83) < 1e-9


@pytest.mark.asyncio
async def test_check_depth_orderbook_fp_empty_opposite_book():
    # YES has bids but NO does not — to BUY YES we need NO bids, so we must
    # report (False, 0.0).  Regression for the ``orderbook_fp`` path that
    # wrongly returned legacy-format zero when the new keys were present.
    body = _orderbook_fp_body(yes_dollars=[["0.80", "100"]], no_dollars=[])
    adapter = _make_adapter(_session_with_get(200, body))
    sufficient, best = await adapter.check_depth("T", "yes", required_qty=10)
    assert sufficient is False
    assert best == 0.0


@pytest.mark.asyncio
async def test_best_executable_price_orderbook_fp_walks_to_required_qty():
    # NO bids at $0.17 (40 ct), $0.16 (50 ct), $0.15 (200 ct).
    # Required 80 ct → must walk to the $0.16 level (40+50=90 ≥ 80),
    # so we'd pay YES ask = 1 - 0.16 = $0.84 to absorb the full size.
    body = _orderbook_fp_body(no_dollars=[
        ["0.15", "200"],
        ["0.16", "50"],
        ["0.17", "40"],
    ])
    adapter = _make_adapter(_session_with_get(200, body))
    fillable, price = await adapter.best_executable_price("T", "yes", required_qty=80)
    assert fillable is True
    assert abs(price - 0.84) < 1e-9


@pytest.mark.asyncio
async def test_best_executable_price_orderbook_fp_top_level_sufficient():
    body = _orderbook_fp_body(no_dollars=[["0.17", "500"]])
    adapter = _make_adapter(_session_with_get(200, body))
    fillable, price = await adapter.best_executable_price("T", "yes", required_qty=100)
    assert fillable is True
    assert abs(price - 0.83) < 1e-9


@pytest.mark.asyncio
async def test_check_depth_orderbook_fp_real_world_payload():
    # Verbatim payload captured from production Kalshi (CONTROLH-2026-D)
    # to lock in that the helper survives whitespace, decimal-point qty,
    # and full ten-level depth without regression.
    body = json.dumps({"orderbook_fp": {
        "no_dollars": [
            ["0.0800", "1440.00"], ["0.0900", "1000.00"],
            ["0.1000", "2931.00"], ["0.1100", "3700.00"],
            ["0.1200", "1860.00"], ["0.1300", "2368.00"],
            ["0.1400", "49126.03"], ["0.1500", "16934.00"],
            ["0.1600", "9096.87"], ["0.1700", "64259.19"],
        ],
        "yes_dollars": [
            ["0.4900", "10000.00"], ["0.5900", "10000.00"],
            ["0.6000", "100.00"], ["0.6500", "150.00"],
            ["0.7700", "10000.00"], ["0.7800", "5767.00"],
            ["0.7900", "2215.00"], ["0.8000", "113994.06"],
            ["0.8100", "108516.81"], ["0.8200", "45334.76"],
        ],
    }})
    adapter = _make_adapter(_session_with_get(200, body))
    sufficient, best = await adapter.check_depth("CONTROLH-2026-D", "yes", required_qty=100)
    assert sufficient is True
    # Top NO bid is $0.17 → YES ask $0.83
    assert abs(best - 0.83) < 1e-9


# ─── place_ioc (IOC time-in-force for secondary leg) ─────────────────────


@pytest.mark.asyncio
async def test_place_ioc_sends_immediate_or_cancel_tif():
    """Secondary-leg IOC must request time_in_force=immediate_or_cancel.

    Without this the engine cannot route the secondary leg as IOC and falls
    back to FOK — recreating the soft-naked-leg pattern from production
    where a stale-by-one-tick book killed the entire order instead of
    accepting the partial fill we'd actually take.
    """
    body = json.dumps({"order": {
        "order_id": "K-IOC-1", "status": "executed",
        "fill_count_fp": "10.00", "yes_price_dollars": "0.5500",
    }})
    session = _session_with_post(200, body)
    adapter = _make_adapter(session)
    order = await adapter.place_ioc("ARB-IOC", "TICKER", "CAN", "yes", 0.55, 10)

    posted = session.post.call_args.kwargs["json"]
    assert posted["time_in_force"] == "immediate_or_cancel"
    assert posted["yes_price_dollars"] == "0.5500"
    assert posted["count_fp"] == "10.00"
    assert order.status == OrderStatus.FILLED


@pytest.mark.asyncio
async def test_place_fok_default_tif_unchanged():
    """Existing place_fok callers must keep getting FOK (regression test for
    the time_in_force kwarg refactor)."""
    body = json.dumps({"order": {
        "order_id": "K-FOK-1", "status": "executed",
        "fill_count_fp": "10.00", "yes_price_dollars": "0.5500",
    }})
    session = _session_with_post(200, body)
    adapter = _make_adapter(session)
    await adapter.place_fok("ARB-FOK", "TICKER", "CAN", "yes", 0.55, 10)
    posted = session.post.call_args.kwargs["json"]
    assert posted["time_in_force"] == "fill_or_kill"


@pytest.mark.asyncio
async def test_place_fok_explicit_tif_override_accepted():
    """A caller can pass time_in_force=immediate_or_cancel directly to place_fok."""
    body = json.dumps({"order": {
        "order_id": "K-X-1", "status": "executed",
        "fill_count_fp": "5.00", "no_price_dollars": "0.4500",
    }})
    session = _session_with_post(200, body)
    adapter = _make_adapter(session)
    await adapter.place_fok(
        "ARB-X", "TICKER", "CAN", "no", 0.45, 5,
        time_in_force="immediate_or_cancel",
    )
    posted = session.post.call_args.kwargs["json"]
    assert posted["time_in_force"] == "immediate_or_cancel"


# ─── fill_price extraction (regression: NO orders mis-priced as 1-no_price) ─


@pytest.mark.asyncio
async def test_no_order_fill_price_uses_no_price_dollars_not_yes():
    """A NO buy that fills at $0.10 must report fill_price=$0.10, NOT $0.90.

    Kalshi returns BOTH yes_price_dollars (1 - no_price) and no_price_dollars
    on every order response.  The previous parser read yes_price_dollars
    first regardless of side, which mis-reported every NO fill onto the YES
    scale and silently caused the engine to think it had paid 9x more cash
    than it actually did — poisoning max_affordable_secondary calc and
    sending the IOC at a useless limit.
    """
    body = json.dumps({"order": {
        "order_id": "K-NO-1",
        "client_order_id": "ARB-X-NO-abc",
        "status": "executed",
        "fill_count_fp": "10.00",
        "no_price_dollars": "0.1000",       # actual fill price
        "yes_price_dollars": "0.9000",      # YES-scale equivalent (= 1 - 0.10)
        "taker_fill_cost_dollars": "1.00",  # cash actually spent
        "taker_fees_dollars": "0.07",
    }})
    session = _session_with_post(200, body)
    adapter = _make_adapter(session)
    order = await adapter.place_fok("ARB-X", "TICKER", "CAN", "no", 0.10, 10)
    # taker_fill_cost ($1.00) / fill_qty (10) = $0.10
    assert order.status == OrderStatus.FILLED
    assert abs(order.fill_price - 0.10) < 1e-9, (
        f"NO order fill_price should be $0.10 (taker_fill_cost/qty), "
        f"got {order.fill_price}"
    )


@pytest.mark.asyncio
async def test_yes_order_fill_price_uses_yes_price_dollars():
    """Mirror of the NO test: YES orders must read yes_price_dollars and
    return the actual fill price, not the NO-scale equivalent."""
    body = json.dumps({"order": {
        "order_id": "K-YES-1",
        "status": "executed",
        "fill_count_fp": "10.00",
        "yes_price_dollars": "0.5500",
        "no_price_dollars": "0.4500",
        "taker_fill_cost_dollars": "5.50",
    }})
    session = _session_with_post(200, body)
    adapter = _make_adapter(session)
    order = await adapter.place_fok("ARB-Y", "TICKER", "CAN", "yes", 0.55, 10)
    assert abs(order.fill_price - 0.55) < 1e-9


@pytest.mark.asyncio
async def test_fill_price_prefers_taker_fill_cost_over_limit_fields():
    """taker_fill_cost_dollars / fill_count_fp is the most accurate fill
    price (it's the actual cash that left our account).  When present, it
    must override the limit-price echo fields, which only reflect what we
    SENT not what we got."""
    # Limit was $0.20 but actually filled at $0.15 average (cheaper levels in book).
    body = json.dumps({"order": {
        "order_id": "K-AVG-1",
        "status": "executed",
        "fill_count_fp": "10.00",
        "no_price_dollars": "0.2000",        # limit (not actual)
        "yes_price_dollars": "0.8000",       # mirror of limit
        "taker_fill_cost_dollars": "1.50",   # actual cash → avg $0.15
    }})
    session = _session_with_post(200, body)
    adapter = _make_adapter(session)
    order = await adapter.place_fok("ARB-A", "TICKER", "CAN", "no", 0.20, 10)
    assert abs(order.fill_price - 0.15) < 1e-9, (
        f"fill_price should be taker_fill_cost/qty ($0.15), got {order.fill_price}"
    )


@pytest.mark.asyncio
async def test_fill_price_falls_back_to_side_correct_field_when_no_taker_cost():
    """If taker_fill_cost_dollars is absent (older Kalshi formats), fall
    back to the side-correct *_price_dollars field, NOT the opposite-side
    field.  This is the core regression."""
    body = json.dumps({"order": {
        "order_id": "K-NO-2",
        "status": "executed",
        "fill_count_fp": "5.00",
        "no_price_dollars": "0.0700",
        "yes_price_dollars": "0.9300",
        # taker_fill_cost_dollars deliberately absent
    }})
    session = _session_with_post(200, body)
    adapter = _make_adapter(session)
    order = await adapter.place_fok("ARB-B", "TICKER", "CAN", "no", 0.10, 5)
    assert abs(order.fill_price - 0.07) < 1e-9


# ─── place_unwind_sell ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_place_unwind_sell_posts_sell_ioc_at_panic_price():
    body = json.dumps({"order": {
        "order_id": "K-UNWIND-1",
        "status": "executed",
        "fill_count_fp": "10.0",
        "yes_price_dollars": "0.4500",
    }})
    session = _session_with_post(200, body)
    adapter = _make_adapter(session)
    order = await adapter.place_unwind_sell(
        "ARB-7-UNWIND", "TICKER", "C", "yes", qty=10,
    )
    assert order.status == OrderStatus.FILLED
    assert order.fill_qty == 10.0
    # Verify order body shape: action=sell, IOC, panic price 0.01
    posted_body = session.post.call_args.kwargs["json"]
    assert posted_body["action"] == "sell"
    assert posted_body["time_in_force"] == "immediate_or_cancel"
    assert posted_body["yes_price_dollars"] == "0.0100"
    assert posted_body["count_fp"] == "10.00"


@pytest.mark.asyncio
async def test_place_unwind_sell_returns_failed_when_no_auth():
    session = _session_with_post(200, "{}")
    adapter = _make_adapter(session, authenticated=False)
    order = await adapter.place_unwind_sell(
        "ARB-NA", "T", "C", "yes", qty=5,
    )
    assert order.status == OrderStatus.FAILED
    assert "auth not configured" in (order.error or "").lower()


@pytest.mark.asyncio
async def test_place_unwind_sell_returns_failed_when_circuit_open():
    session = _session_with_post(200, "{}")
    adapter = _make_adapter(session, can_execute=False)
    order = await adapter.place_unwind_sell(
        "ARB-CO", "T", "C", "yes", qty=5,
    )
    assert order.status == OrderStatus.FAILED
    assert "circuit open" in (order.error or "").lower()


@pytest.mark.asyncio
async def test_place_unwind_sell_no_side_passes_no_price():
    body = json.dumps({"order": {"order_id": "K-U", "status": "executed", "fill_count_fp": "5"}})
    session = _session_with_post(200, body)
    adapter = _make_adapter(session)
    await adapter.place_unwind_sell("ARB-N", "T", "C", "no", qty=5)
    posted_body = session.post.call_args.kwargs["json"]
    assert posted_body["side"] == "no"
    assert posted_body["no_price_dollars"] == "0.0100"
    assert "yes_price_dollars" not in posted_body


# ─── cancel_order ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_cancel_returns_true_on_204():
    session = _session_with_delete(204)
    adapter = _make_adapter(session)
    order = Order(
        order_id="K-1", platform="kalshi", market_id="T", canonical_id="C",
        side="yes", price=0.5, quantity=1, status=OrderStatus.SUBMITTED,
    )
    assert await adapter.cancel_order(order) is True


@pytest.mark.asyncio
async def test_cancel_returns_true_on_200():
    session = _session_with_delete(200)
    adapter = _make_adapter(session)
    order = Order(
        order_id="K-2", platform="kalshi", market_id="T", canonical_id="C",
        side="yes", price=0.5, quantity=1, status=OrderStatus.SUBMITTED,
    )
    assert await adapter.cancel_order(order) is True


@pytest.mark.asyncio
async def test_cancel_returns_false_on_404():
    session = _session_with_delete(404)
    adapter = _make_adapter(session)
    order = Order(
        order_id="K-MISSING", platform="kalshi", market_id="T", canonical_id="C",
        side="yes", price=0.5, quantity=1, status=OrderStatus.SUBMITTED,
    )
    assert await adapter.cancel_order(order) is False


@pytest.mark.asyncio
async def test_cancel_returns_false_on_no_auth():
    session = _session_with_delete(204)
    adapter = _make_adapter(session, authenticated=False)
    order = Order(
        order_id="K-NA", platform="kalshi", market_id="T", canonical_id="C",
        side="yes", price=0.5, quantity=1, status=OrderStatus.SUBMITTED,
    )
    assert await adapter.cancel_order(order) is False
    assert not session.delete.called


# ─── CR-02: external_client_order_id population ──────────────────────────

@pytest.mark.asyncio
async def test_place_fok_returns_external_client_order_id():
    """CR-02 regression: Order.external_client_order_id carries the Kalshi
    client_order_id (the engine-chosen ARB-prefixed string), even when Kalshi
    returns a different server-assigned order_id in the response.
    """
    body = json.dumps({
        "order": {
            "order_id": "KALSHI-SERVER-XYZ-123",
            "status": "executed",
            "fill_count_fp": "10.00",
            "yes_price_dollars": "0.5500",
        },
    })
    session = _session_with_post(200, body)
    adapter = _make_adapter(session)
    order = await adapter.place_fok("ARB-000042", "TICKER", "CID", "yes", 0.55, 10)
    assert order.external_client_order_id is not None
    assert order.external_client_order_id.startswith("ARB-000042-YES-")
    # The order_id is the Kalshi server id — explicitly different from external_client_order_id
    assert order.order_id == "KALSHI-SERVER-XYZ-123"
    assert order.order_id != order.external_client_order_id


# Alias for VALIDATION.md row 02.1-01-01 naming
test_place_fok_populates_external_client_order_id = test_place_fok_returns_external_client_order_id


# ─── SAFE-04: rate-limiter acquire-before-I/O and 429 handling ───────────


def _tracking_rate_limiter(call_log):
    """Rate limiter whose acquire() records into call_log."""
    async def _acquire():
        call_log.append(("rate_limiter.acquire", None))

    rl = MagicMock()
    rl.acquire = AsyncMock(side_effect=_acquire)
    rl.apply_retry_after = MagicMock(return_value=3.0)
    return rl


def _tracking_session_with_post(status, body_text, call_log, headers=None):
    """Session whose .post(...) records into call_log and returns status/body."""
    session = MagicMock()
    resp = MagicMock()
    resp.status = status
    resp.headers = headers or {}
    resp.text = AsyncMock(return_value=body_text)

    def _post(*args, **kwargs):
        call_log.append(("session.post", kwargs.get("json")))
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=resp)
        cm.__aexit__ = AsyncMock(return_value=False)
        return cm

    session.post = MagicMock(side_effect=_post)
    return session


def _tracking_session_with_delete(status, call_log):
    session = MagicMock()
    resp = MagicMock()
    resp.status = status
    resp.headers = {}

    def _delete(*args, **kwargs):
        call_log.append(("session.delete", args))
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=resp)
        cm.__aexit__ = AsyncMock(return_value=False)
        return cm

    session.delete = MagicMock(side_effect=_delete)
    return session


@pytest.mark.asyncio
async def test_place_fok_acquires_rate_token_before_http():
    """SAFE-04: rate_limiter.acquire() MUST be awaited BEFORE session.post()."""
    call_log: list = []
    body = json.dumps({
        "order": {
            "order_id": "K-RL-1",
            "status": "executed",
            "fill_count_fp": "10.00",
            "yes_price_dollars": "0.5500",
        },
    })
    session = _tracking_session_with_post(200, body, call_log)
    adapter = KalshiAdapter(
        config=_config(),
        session=session,
        auth=_auth(True),
        rate_limiter=_tracking_rate_limiter(call_log),
        circuit=_circuit(True),
    )
    await adapter.place_fok("ARB-RL-1", "T", "C", "yes", 0.55, 10)

    # Ordering check: acquire must come before any session.post
    acquire_idx = next(
        (i for i, (k, _) in enumerate(call_log) if k == "rate_limiter.acquire"), None,
    )
    post_idx = next(
        (i for i, (k, _) in enumerate(call_log) if k == "session.post"), None,
    )
    assert acquire_idx is not None, f"rate_limiter.acquire never called; log={call_log}"
    assert post_idx is not None, f"session.post never called; log={call_log}"
    assert acquire_idx < post_idx, (
        f"rate_limiter.acquire must be awaited BEFORE session.post "
        f"(acquire_idx={acquire_idx} post_idx={post_idx} log={call_log})"
    )


@pytest.mark.asyncio
async def test_place_fok_429_applies_retry_after():
    """SAFE-04: 429 triggers apply_retry_after + circuit.record_failure + FAILED
    order + NO retry (FOK semantics)."""
    call_log: list = []
    session = _tracking_session_with_post(
        429, "rate limited", call_log, headers={"Retry-After": "3"},
    )
    rate_limiter = _tracking_rate_limiter(call_log)
    rate_limiter.apply_retry_after = MagicMock(return_value=3.0)
    circuit = _circuit(True)
    adapter = KalshiAdapter(
        config=_config(),
        session=session,
        auth=_auth(True),
        rate_limiter=rate_limiter,
        circuit=circuit,
    )
    order = await adapter.place_fok("ARB-RL-429", "T", "C", "yes", 0.55, 10)

    # 1. apply_retry_after was called with ("3", fallback_delay=2.0, reason="kalshi_429")
    assert rate_limiter.apply_retry_after.called, (
        "apply_retry_after not invoked on 429 response"
    )
    # Inspect call_args — header must be "3", fallback 2.0, reason kalshi_429
    args, kwargs = rate_limiter.apply_retry_after.call_args
    # Header can be positional arg[0] or kwarg "retry_after"
    header_arg = args[0] if args else kwargs.get("retry_after")
    assert header_arg == "3", f"expected retry_after header '3', got {header_arg!r}"
    # fallback must be 2.0 — either positional or kwarg
    if len(args) >= 2:
        assert args[1] == 2.0 or kwargs.get("fallback_delay") == 2.0
    else:
        assert kwargs.get("fallback_delay") == 2.0
    # reason must be "kalshi_429"
    reason = kwargs.get("reason")
    assert reason == "kalshi_429", f"expected reason='kalshi_429', got {reason!r}"

    # 2. Circuit failure recorded
    assert circuit.record_failure.called, "circuit.record_failure not called on 429"

    # 3. Order is FAILED with "rate_limited" in error
    assert order.status == OrderStatus.FAILED
    assert "rate_limited" in (order.error or ""), (
        f"expected 'rate_limited' in order.error, got {order.error!r}"
    )

    # 4. session.post called exactly once — NO retry
    post_calls = [c for c in call_log if c[0] == "session.post"]
    assert len(post_calls) == 1, (
        f"expected exactly 1 session.post call (no retry on 429 for FOK), "
        f"got {len(post_calls)}: {post_calls}"
    )


@pytest.mark.asyncio
async def test_cancel_order_acquires_rate_token():
    """SAFE-04: cancel_order acquires a rate-limit token before the DELETE."""
    call_log: list = []
    session = _tracking_session_with_delete(204, call_log)
    adapter = KalshiAdapter(
        config=_config(),
        session=session,
        auth=_auth(True),
        rate_limiter=_tracking_rate_limiter(call_log),
        circuit=_circuit(True),
    )
    order = Order(
        order_id="K-CAN", platform="kalshi", market_id="T", canonical_id="C",
        side="yes", price=0.5, quantity=1, status=OrderStatus.SUBMITTED,
    )
    await adapter.cancel_order(order)

    acquire_idx = next(
        (i for i, (k, _) in enumerate(call_log) if k == "rate_limiter.acquire"), None,
    )
    delete_idx = next(
        (i for i, (k, _) in enumerate(call_log) if k == "session.delete"), None,
    )
    assert acquire_idx is not None, "rate_limiter.acquire never called during cancel_order"
    assert delete_idx is not None, "session.delete never called"
    assert acquire_idx < delete_idx, (
        f"acquire must come before delete (acquire_idx={acquire_idx} "
        f"delete_idx={delete_idx})"
    )


@pytest.mark.asyncio
async def test_cancel_all_acquires_token_per_chunk(monkeypatch):
    """SAFE-04 + SAFE-05: cancel_all acquires at least one rate-limit token
    per chunk when there are open orders. With plan 03-05's real
    implementation, a single open order means exactly one chunk → one acquire.
    """
    call_log: list = []

    def _delete_factory(url, json=None, headers=None):
        call_log.append(("session.delete", json))
        resp = MagicMock()
        resp.status = 204
        resp.headers = {}
        resp.text = AsyncMock(return_value="")
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=resp)
        cm.__aexit__ = AsyncMock(return_value=False)
        return cm

    session = MagicMock()
    session.delete = MagicMock(side_effect=_delete_factory)

    # Seed one open order so cancel_all has a chunk to process.
    one_order = Order(
        order_id="K-OPEN-1",
        platform="kalshi",
        market_id="T",
        canonical_id="",
        side="yes",
        price=0.5,
        quantity=1,
        status=OrderStatus.SUBMITTED,
    )

    async def _fake_list(self=None):
        return [one_order]

    monkeypatch.setattr(
        KalshiAdapter, "_list_all_open_orders", _fake_list, raising=False,
    )

    adapter = KalshiAdapter(
        config=_config(),
        session=session,
        auth=_auth(True),
        rate_limiter=_tracking_rate_limiter(call_log),
        circuit=_circuit(True),
    )
    await adapter.cancel_all()
    acquires = [c for c in call_log if c[0] == "rate_limiter.acquire"]
    assert len(acquires) >= 1, (
        f"cancel_all must acquire at least one rate-limit token; log={call_log}"
    )


# ─── SAFE-05: cancel_all full implementation (chunked batched DELETE) ─────


@pytest.mark.asyncio
async def test_cancel_all_chunks_orders_in_20s(monkeypatch):
    """SAFE-05: cancel_all chunks open orders into 20-sized batches and invokes
    DELETE /portfolio/orders/batched per chunk. 45 orders → 3 chunks.

    Verifies:
    - Session.delete called exactly 3 times (ceil(45/20)).
    - rate_limiter.acquire called at least 3 times (per chunk).
    - Returned list carries 45 cancelled order_ids.
    """
    # 45 open orders with unique order_ids.
    open_orders = [
        Order(
            order_id=f"K-OPEN-{i:03d}",
            platform="kalshi",
            market_id="TICKER",
            canonical_id="",
            side="yes",
            price=0.5,
            quantity=10,
            status=OrderStatus.SUBMITTED,
        )
        for i in range(45)
    ]

    call_log: list = []
    delete_bodies: list = []

    # Session whose .delete(...) returns 200 with a body echoing the ids
    # from the POSTed payload (JSON of the DELETE body).
    def _delete_factory(url, json=None, headers=None):
        delete_bodies.append(json or {})
        call_log.append(("session.delete", len((json or {}).get("ids", []))))
        resp = MagicMock()
        resp.status = 200
        # Build a response body echoing the ids we posted, no errors.
        body_data = {
            "results": [
                {"order_id": oid, "error": None} for oid in (json or {}).get("ids", [])
            ]
        }
        resp.text = AsyncMock(return_value=json_dumps(body_data))
        resp.headers = {}
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=resp)
        cm.__aexit__ = AsyncMock(return_value=False)
        return cm

    session = MagicMock()
    session.delete = MagicMock(side_effect=_delete_factory)

    rate_limiter = _tracking_rate_limiter(call_log)
    adapter = KalshiAdapter(
        config=_config(),
        session=session,
        auth=_auth(True),
        rate_limiter=rate_limiter,
        circuit=_circuit(True),
    )

    # Monkeypatch the (new) list-open-orders helper so the test doesn't depend
    # on Kalshi HTTP traffic for discovery.
    async def _fake_list_all_open(self=None):
        return list(open_orders)

    monkeypatch.setattr(
        KalshiAdapter, "_list_all_open_orders", _fake_list_all_open, raising=False,
    )

    cancelled = await adapter.cancel_all()

    # 3 DELETE calls for 45 orders @ 20/chunk = 3 chunks.
    delete_calls = [c for c in call_log if c[0] == "session.delete"]
    assert len(delete_calls) == 3, (
        f"expected 3 DELETE calls for 45 orders, got {len(delete_calls)}: {call_log}"
    )

    # Each chunk ≤ 20 orders.
    for idx, (_, count) in enumerate(delete_calls):
        assert count <= 20, f"chunk {idx} had {count} orders (must be ≤ 20)"

    # rate_limiter.acquire called at least 3 times (one per chunk).
    acquires = [c for c in call_log if c[0] == "rate_limiter.acquire"]
    assert len(acquires) >= 3, (
        f"expected ≥3 acquires (one per chunk), got {len(acquires)}: {call_log}"
    )

    # Returned list contains all 45 ids.
    assert len(cancelled) == 45, f"expected 45 cancelled ids, got {len(cancelled)}"
    assert set(cancelled) == {o.order_id for o in open_orders}


def json_dumps(obj):
    """Helper for the mock above — tests should not import the stdlib at top."""
    return json.dumps(obj)
