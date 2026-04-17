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
async def test_cancel_all_acquires_token_per_chunk():
    """SAFE-04: cancel_all (even the plan-03-01 stub) acquires at least one token.

    Plan 03-05 will replace the stub with chunked batch cancels (one acquire per
    chunk). For now the stub can log+return [], but acquiring at least once is
    the forward-compat invariant this test enforces.
    """
    call_log: list = []
    session = _tracking_session_with_delete(204, call_log)
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
