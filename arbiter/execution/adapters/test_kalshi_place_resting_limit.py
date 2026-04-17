"""Tests for KalshiAdapter.place_resting_limit (Plan 04-02.1 scope-expansion).

Context: Three independent Wave 2 agents (04-03, 04-06, 04-07) hit the same
gap — KalshiAdapter exposed only ``place_fok``, but SAFE-01 kill-switch
live-fire (Plan 04-05) requires a resting order that survives >=5 seconds so
the kill-switch can trip and cancel it mid-life. Adding ``place_resting_limit``
closes that gap and removes the ``adapter._client`` / raw-HTTP workarounds
scattered across 04-03/04-06/04-07 test files.

Method contract:
- Same signature shape as ``place_fok`` (``arb_id``, ``market_id``,
  ``canonical_id``, ``side``, ``price``, ``qty``) — price is a float in (0, 1)
  using the dollar convention the rest of the adapter uses.
- Order body: ``time_in_force`` field is OMITTED (absence = GTC/resting).
- Returns Order; ``SUBMITTED`` when Kalshi responds ``status="resting"``.
- PHASE4_MAX_ORDER_USD hard-lock applied BEFORE any HTTP call (Pitfall 8).
- Circuit-breaker + rate-limiter integration mirrors ``place_fok``.

Follows root-conftest async dispatch style (``@pytest.mark.asyncio`` marker
with the custom ``pytest_pyfunc_call`` hook that wraps each coroutine in
``asyncio.run``).
"""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from arbiter.execution.adapters.kalshi import KalshiAdapter
from arbiter.execution.engine import Order, OrderStatus


# ─── Fixtures (mirror test_kalshi_adapter.py style) ──────────────────────

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
    rl.apply_retry_after = MagicMock(return_value=3.0)
    return rl


def _session_with_post(status: int, body_text: str, headers=None):
    session = MagicMock()
    resp = MagicMock()
    resp.status = status
    resp.text = AsyncMock(return_value=body_text)
    resp.headers = headers or {}
    session.post.return_value.__aenter__ = AsyncMock(return_value=resp)
    session.post.return_value.__aexit__ = AsyncMock(return_value=False)
    return session


def _tracking_rate_limiter(call_log):
    """Rate limiter whose acquire() records ordering into call_log."""
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


def _make_adapter(session, *, authenticated: bool = True, can_execute: bool = True):
    return KalshiAdapter(
        config=_config(),
        session=session,
        auth=_auth(authenticated),
        rate_limiter=_rate_limiter(),
        circuit=_circuit(can_execute),
    )


# ─── HTTP body shape: time_in_force is OMITTED + correct price field ─────

@pytest.mark.asyncio
async def test_resting_limit_body_omits_time_in_force_yes_side():
    """A resting Kalshi order must NOT carry ``time_in_force=fill_or_kill``
    (that is what makes it FOK). Absence of the field = GTC/resting at Kalshi.
    ``yes_price_dollars`` is set; ``no_price_dollars`` is not.
    """
    body = json.dumps({
        "order": {
            "order_id": "K-REST-1",
            "status": "resting",
            "client_order_id": "ARB-000100-YES-abc12345",
            "fill_count_fp": "0",
            "yes_price_dollars": "0.5500",
        },
    })
    session = _session_with_post(201, body)
    adapter = _make_adapter(session)
    order = await adapter.place_resting_limit(
        "ARB-000100", "TICKER", "CAN", "yes", 0.55, 10,
    )
    posted = session.post.call_args.kwargs["json"]
    # CRITICAL: no time_in_force — absence is what makes it a resting order.
    assert "time_in_force" not in posted, (
        f"place_resting_limit must NOT set time_in_force; got {posted!r}"
    )
    assert posted["type"] == "limit"
    assert posted["action"] == "buy"
    assert posted["side"] == "yes"
    assert posted["ticker"] == "TICKER"
    assert posted["count_fp"] == "10.00"
    assert posted["yes_price_dollars"] == "0.5500"
    assert "no_price_dollars" not in posted
    assert posted["client_order_id"].startswith("ARB-000100-YES-")
    # Resting order → SUBMITTED (not FILLED; the order is on the book).
    assert order.status == OrderStatus.SUBMITTED
    assert order.order_id == "K-REST-1"


@pytest.mark.asyncio
async def test_resting_limit_body_omits_time_in_force_no_side():
    body = json.dumps({
        "order": {
            "order_id": "K-REST-2",
            "status": "resting",
            "client_order_id": "ARB-000101-NO-def67890",
            "fill_count_fp": "0",
            "no_price_dollars": "0.4500",
        },
    })
    session = _session_with_post(201, body)
    adapter = _make_adapter(session)
    await adapter.place_resting_limit(
        "ARB-000101", "TICKER", "CAN", "no", 0.45, 5,
    )
    posted = session.post.call_args.kwargs["json"]
    assert "time_in_force" not in posted
    assert posted["no_price_dollars"] == "0.4500"
    assert "yes_price_dollars" not in posted
    assert posted["client_order_id"].startswith("ARB-000101-NO-")


# ─── Returns Order with status=SUBMITTED + order_id from response ────────

@pytest.mark.asyncio
async def test_resting_limit_returns_submitted_with_platform_order_id():
    """The happy path for a resting order: Kalshi accepts it, returns
    status=resting, and the adapter maps that to OrderStatus.SUBMITTED so
    the engine knows the order is live on the book (not yet terminal).
    """
    body = json.dumps({
        "order": {
            "order_id": "K-SERVER-ABCDEF",
            "status": "resting",
            "client_order_id": "ARB-000200-YES-deadbeef",
            "fill_count_fp": "0",
            "yes_price_dollars": "0.5000",
        },
    })
    session = _session_with_post(201, body)
    adapter = _make_adapter(session)
    order = await adapter.place_resting_limit(
        "ARB-000200", "T", "CID", "yes", 0.50, 4,
    )
    assert order.status == OrderStatus.SUBMITTED
    assert order.order_id == "K-SERVER-ABCDEF"
    # CR-02 parity: external_client_order_id carries the engine-chosen key.
    assert order.external_client_order_id is not None
    assert order.external_client_order_id.startswith("ARB-000200-YES-")
    # Platform-assigned order_id differs from the engine's client_order_id.
    assert order.order_id != order.external_client_order_id


# ─── PHASE4_MAX_ORDER_USD hard-lock (belt above RiskManager) ─────────────

@pytest.mark.asyncio
async def test_resting_limit_hardlock_rejects_when_notional_exceeds_cap(monkeypatch):
    """Phase 4 blast-radius hard-lock: notional = qty * price > cap → FAILED
    WITHOUT any HTTP call. Mirrors PolymarketAdapter.place_fok hard-lock.

    PHASE4_MAX_ORDER_USD=5, qty=10, price=0.60 → notional=6.00 > 5 → rejected.
    """
    monkeypatch.setenv("PHASE4_MAX_ORDER_USD", "5")
    session = _session_with_post(201, "{}")
    adapter = _make_adapter(session)
    result = await adapter.place_resting_limit(
        "ARB-HL-1", "T", "C", "yes", 0.60, 10,
    )
    assert result.status == OrderStatus.FAILED
    assert "PHASE4_MAX_ORDER_USD hard-lock" in result.error
    assert "$6.00" in result.error
    assert "$5.00" in result.error
    assert not session.post.called, "HTTP call must NOT happen when hard-lock trips"


@pytest.mark.asyncio
async def test_resting_limit_hardlock_allows_when_notional_under_cap(monkeypatch):
    """Under-cap notional proceeds to HTTP (we assert session.post WAS called)."""
    monkeypatch.setenv("PHASE4_MAX_ORDER_USD", "5")
    body = json.dumps({
        "order": {
            "order_id": "K-OK",
            "status": "resting",
            "client_order_id": "ARB-HL-2-YES-cafef00d",
            "fill_count_fp": "0",
            "yes_price_dollars": "0.4000",
        },
    })
    session = _session_with_post(201, body)
    adapter = _make_adapter(session)
    await adapter.place_resting_limit("ARB-HL-2", "T", "C", "yes", 0.40, 10)
    assert session.post.called, "HTTP call must happen when notional under cap"


@pytest.mark.asyncio
async def test_resting_limit_hardlock_boundary_exact_equal_allowed(monkeypatch):
    """Strict > comparison: notional == cap is ALLOWED (matches
    test_polymarket_phase4_hardlock.py:test_hardlock_boundary_exact_equal_allowed).
    """
    monkeypatch.setenv("PHASE4_MAX_ORDER_USD", "5")
    body = json.dumps({
        "order": {
            "order_id": "K-EQ",
            "status": "resting",
            "client_order_id": "ARB-HL-3-YES-abc",
            "fill_count_fp": "0",
            "yes_price_dollars": "1.0000",
        },
    })
    session = _session_with_post(201, body)
    adapter = _make_adapter(session)
    # qty=5 * price=1.00 == 5.00 == cap → allowed
    result = await adapter.place_resting_limit(
        "ARB-HL-3", "T", "C", "yes", 0.99, 5,
    )
    # 0.99 * 5 = 4.95 — definitively under cap; use it to keep price valid (<1).
    # (Price must be in (0, 1) exclusive per _validate_price.)
    assert session.post.called
    assert result.status == OrderStatus.SUBMITTED


@pytest.mark.asyncio
async def test_resting_limit_hardlock_unparseable_env_is_maximally_restrictive(monkeypatch):
    """Garbage env value → 0.0 cap → any positive notional rejected."""
    monkeypatch.setenv("PHASE4_MAX_ORDER_USD", "not-a-number")
    session = _session_with_post(201, "{}")
    adapter = _make_adapter(session)
    result = await adapter.place_resting_limit(
        "ARB-HL-4", "T", "C", "yes", 0.01, 1,
    )
    assert result.status == OrderStatus.FAILED
    assert "hard-lock" in result.error
    assert not session.post.called


@pytest.mark.asyncio
async def test_resting_limit_hardlock_noop_when_env_unset(monkeypatch):
    """Unset PHASE4_MAX_ORDER_USD → production behaviour unchanged (HTTP proceeds)."""
    monkeypatch.delenv("PHASE4_MAX_ORDER_USD", raising=False)
    body = json.dumps({
        "order": {
            "order_id": "K-FREE",
            "status": "resting",
            "client_order_id": "ARB-HL-5-YES-feed",
            "fill_count_fp": "0",
            "yes_price_dollars": "0.9000",
        },
    })
    session = _session_with_post(201, body)
    adapter = _make_adapter(session)
    # Large notional that WOULD trip a hard-lock if set; without env it proceeds.
    await adapter.place_resting_limit("ARB-HL-5", "T", "C", "yes", 0.90, 100)
    assert session.post.called


# ─── Refusal paths (NO HTTP call) ────────────────────────────────────────

@pytest.mark.asyncio
async def test_resting_limit_rejects_when_no_auth():
    session = _session_with_post(201, "{}")
    adapter = _make_adapter(session, authenticated=False)
    order = await adapter.place_resting_limit(
        "ARB-NA", "T", "C", "yes", 0.55, 10,
    )
    assert order.status == OrderStatus.FAILED
    assert "Kalshi auth not configured" in order.error
    assert not session.post.called


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_price", [0, 1, -0.01, 1.5])
async def test_resting_limit_rejects_invalid_price(bad_price):
    session = _session_with_post(201, "{}")
    adapter = _make_adapter(session)
    order = await adapter.place_resting_limit(
        "ARB-BP", "T", "C", "yes", bad_price, 10,
    )
    assert order.status == OrderStatus.FAILED
    assert "Invalid price" in order.error
    assert not session.post.called


@pytest.mark.asyncio
async def test_resting_limit_circuit_open_short_circuits():
    session = _session_with_post(201, "{}")
    adapter = _make_adapter(session, can_execute=False)
    order = await adapter.place_resting_limit(
        "ARB-CO", "T", "C", "yes", 0.55, 10,
    )
    assert order.status == OrderStatus.FAILED
    assert "circuit open" in order.error.lower()
    assert not session.post.called


# ─── Circuit-breaker integration (failure/success recording) ─────────────

@pytest.mark.asyncio
async def test_resting_limit_non_2xx_records_circuit_failure():
    session = _session_with_post(500, "internal server error")
    adapter = _make_adapter(session)
    order = await adapter.place_resting_limit(
        "ARB-500", "T", "C", "yes", 0.55, 10,
    )
    assert order.status == OrderStatus.FAILED
    assert "500" in order.error
    adapter.circuit.record_failure.assert_called()


@pytest.mark.asyncio
async def test_resting_limit_success_records_circuit_success():
    body = json.dumps({
        "order": {
            "order_id": "K-OK",
            "status": "resting",
            "client_order_id": "ARB-CBS-YES-feed",
            "fill_count_fp": "0",
            "yes_price_dollars": "0.5500",
        },
    })
    adapter = _make_adapter(_session_with_post(201, body))
    order = await adapter.place_resting_limit("ARB-CBS", "T", "C", "yes", 0.55, 10)
    assert order.status == OrderStatus.SUBMITTED
    adapter.circuit.record_success.assert_called()


@pytest.mark.asyncio
async def test_resting_limit_exception_records_circuit_failure():
    session = MagicMock()
    session.post.side_effect = RuntimeError("network melted")
    adapter = _make_adapter(session)
    order = await adapter.place_resting_limit(
        "ARB-EX", "T", "C", "yes", 0.55, 10,
    )
    assert order.status == OrderStatus.FAILED
    assert "network melted" in order.error
    adapter.circuit.record_failure.assert_called()


# ─── Rate-limiter integration (acquire-before-I/O invariant) ─────────────

@pytest.mark.asyncio
async def test_resting_limit_acquires_rate_token_before_http():
    """SAFE-04 invariant: rate_limiter.acquire() MUST be awaited BEFORE
    session.post() — mirrors ``test_place_fok_acquires_rate_token_before_http``.
    """
    call_log: list = []
    body = json.dumps({
        "order": {
            "order_id": "K-RL",
            "status": "resting",
            "client_order_id": "ARB-RL-YES-aaaa",
            "fill_count_fp": "0",
            "yes_price_dollars": "0.5500",
        },
    })
    session = _tracking_session_with_post(201, body, call_log)
    adapter = KalshiAdapter(
        config=_config(),
        session=session,
        auth=_auth(True),
        rate_limiter=_tracking_rate_limiter(call_log),
        circuit=_circuit(True),
    )
    await adapter.place_resting_limit("ARB-RL", "T", "C", "yes", 0.55, 10)

    acquire_idx = next(
        (i for i, (k, _) in enumerate(call_log) if k == "rate_limiter.acquire"), None,
    )
    post_idx = next(
        (i for i, (k, _) in enumerate(call_log) if k == "session.post"), None,
    )
    assert acquire_idx is not None, f"rate_limiter.acquire never called; log={call_log}"
    assert post_idx is not None, f"session.post never called; log={call_log}"
    assert acquire_idx < post_idx, (
        f"rate_limiter.acquire must come BEFORE session.post "
        f"(acquire_idx={acquire_idx} post_idx={post_idx} log={call_log})"
    )


@pytest.mark.asyncio
async def test_resting_limit_429_applies_retry_after():
    """SAFE-04: 429 triggers apply_retry_after + circuit.record_failure +
    FAILED order + NO retry. Mirrors place_fok 429 handling.
    """
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
    order = await adapter.place_resting_limit(
        "ARB-429", "T", "C", "yes", 0.55, 10,
    )
    assert rate_limiter.apply_retry_after.called
    args, kwargs = rate_limiter.apply_retry_after.call_args
    header_arg = args[0] if args else kwargs.get("retry_after")
    assert header_arg == "3", f"expected retry_after header '3', got {header_arg!r}"
    reason = kwargs.get("reason")
    assert reason == "kalshi_429", f"expected reason='kalshi_429', got {reason!r}"
    assert circuit.record_failure.called
    assert order.status == OrderStatus.FAILED
    assert "rate_limited" in (order.error or "")
    # NO retry on 429 for a resting-limit POST (same semantics as FOK — a
    # second submission could place a duplicate order).
    post_calls = [c for c in call_log if c[0] == "session.post"]
    assert len(post_calls) == 1, (
        f"expected exactly 1 session.post call (no retry on 429), "
        f"got {len(post_calls)}: {post_calls}"
    )


# ─── cancel_order still works on the returned resting Order ──────────────

@pytest.mark.asyncio
async def test_cancel_order_accepts_place_resting_limit_order_id():
    """The Order returned by place_resting_limit must be usable by
    cancel_order without modification — i.e. order.order_id is a real Kalshi
    order id, not an engine-synthesized placeholder. This is the contract
    04-05 relies on (place → sleep → kill-switch triggers cancel_order).
    """
    # 1) Submit a resting order; the adapter returns an Order with a real
    #    Kalshi order_id from the response body.
    submit_body = json.dumps({
        "order": {
            "order_id": "K-RESTING-FOR-CANCEL",
            "status": "resting",
            "client_order_id": "ARB-CAN-YES-ffee",
            "fill_count_fp": "0",
            "yes_price_dollars": "0.5000",
        },
    })
    submit_session = _session_with_post(201, submit_body)
    adapter = _make_adapter(submit_session)
    resting = await adapter.place_resting_limit(
        "ARB-CAN", "T", "C", "yes", 0.50, 4,
    )
    assert resting.status == OrderStatus.SUBMITTED
    assert resting.order_id == "K-RESTING-FOR-CANCEL"

    # 2) Swap in a DELETE-capable session, then cancel the same Order.
    cancel_session = MagicMock()
    resp = MagicMock()
    resp.status = 204
    resp.headers = {}
    cancel_session.delete.return_value.__aenter__ = AsyncMock(return_value=resp)
    cancel_session.delete.return_value.__aexit__ = AsyncMock(return_value=False)
    adapter.session = cancel_session
    assert await adapter.cancel_order(resting) is True
    # Verify DELETE was routed at /portfolio/orders/{order_id}
    called_args, _ = cancel_session.delete.call_args
    assert "K-RESTING-FOR-CANCEL" in called_args[0]


# ─── Interop: place_fok behavior unchanged (regression guard) ────────────

@pytest.mark.asyncio
async def test_place_fok_still_sets_time_in_force_after_scope_expansion():
    """Regression guard: adding place_resting_limit MUST NOT alter the
    place_fok body shape. This is the single assertion a 04-05 / Phase 5
    operator cares about — both methods co-exist and do NOT cross-contaminate.
    """
    body = json.dumps({
        "order": {
            "order_id": "K-FOK",
            "status": "executed",
            "fill_count_fp": "10.00",
            "yes_price_dollars": "0.5500",
        },
    })
    session = _session_with_post(200, body)
    adapter = _make_adapter(session)
    await adapter.place_fok("ARB-FOK", "TICKER", "CAN", "yes", 0.55, 10)
    posted = session.post.call_args.kwargs["json"]
    assert posted["time_in_force"] == "fill_or_kill", (
        "place_fok must STILL set time_in_force=fill_or_kill; "
        "place_resting_limit is a separate method"
    )
