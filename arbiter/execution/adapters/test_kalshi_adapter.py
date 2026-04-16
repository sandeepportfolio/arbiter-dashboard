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
    session = _session_with_post(429, "rate limited")
    adapter = _make_adapter(session)
    order = await adapter.place_fok("ARB-Y", "T", "C", "yes", 0.55, 10)
    assert order.status == OrderStatus.FAILED
    assert "429" in order.error
    assert "rate limited" in order.error
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
