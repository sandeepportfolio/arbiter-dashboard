"""Tests for arbiter.execution.adapters.polymarket.PolymarketAdapter
(EXEC-01, EXEC-03, EXEC-04, Pitfalls 1+2).

The ClobClient is mocked as a MagicMock with synchronous methods; the adapter
wraps each SDK call in `loop.run_in_executor(None, lambda: client.method(...))`,
which invokes the synchronous MagicMock correctly.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from arbiter.execution.adapters import PlatformAdapter
from arbiter.execution.adapters.polymarket import PolymarketAdapter
from arbiter.execution.engine import Order, OrderStatus


# --- Fixtures --------------------------------------------------------------

def _config(private_key: str = "0xdeadbeef"):
    cfg = SimpleNamespace()
    cfg.polymarket = SimpleNamespace(
        private_key=private_key,
        clob_url="https://clob.test",
        chain_id=137,
        signature_type=1,
        funder="0xfunder",
    )
    return cfg


def _circuit(can_execute: bool = True):
    c = MagicMock()
    c.can_execute = MagicMock(return_value=can_execute)
    c.record_success = MagicMock()
    c.record_failure = MagicMock()
    return c


def _rate_limiter():
    rl = MagicMock()
    rl.acquire = AsyncMock(return_value=None)
    return rl


def _make_adapter(client=None, *, private_key="0xdeadbeef", can_execute=True):
    cfg = _config(private_key=private_key)
    # Capture client by closure so the factory returns the same instance every call
    factory = (lambda: client) if client is not None else (lambda: None)
    return PolymarketAdapter(
        config=cfg,
        clob_client_factory=factory,
        rate_limiter=_rate_limiter(),
        circuit=_circuit(can_execute),
    )


def _good_post_response(order_id="P-1"):
    return {
        "success": True,
        "orderID": order_id,
        "status": "matched",
        "size_matched": 10,
    }


# --- Protocol conformance --------------------------------------------------

def test_polymarket_adapter_satisfies_protocol():
    adapter = _make_adapter(client=MagicMock())
    assert isinstance(adapter, PlatformAdapter)


# --- Two-phase FOK ---------------------------------------------------------

@pytest.mark.asyncio
async def test_place_fok_uses_two_phase_create_then_post():
    client = MagicMock()
    client.get_orders = MagicMock(return_value=[])
    client.create_order = MagicMock(return_value="SIGNED")
    client.post_order = MagicMock(return_value=_good_post_response("P-2"))
    adapter = _make_adapter(client=client)
    order = await adapter.place_fok(
        "ARB-1", "TOKEN-A", "DEM_HOUSE", "yes", 0.55, 10,
    )
    assert client.create_order.called
    assert client.post_order.called
    assert order.status == OrderStatus.FILLED
    assert order.order_id == "P-2"


@pytest.mark.asyncio
async def test_place_fok_post_order_called_with_fok_order_type():
    from py_clob_client.clob_types import OrderType
    client = MagicMock()
    client.get_orders = MagicMock(return_value=[])
    client.create_order = MagicMock(return_value="SIGNED")
    client.post_order = MagicMock(return_value=_good_post_response())
    adapter = _make_adapter(client=client)
    await adapter.place_fok("ARB-1", "TOKEN", "C", "yes", 0.55, 10)
    args, kwargs = client.post_order.call_args
    # Second positional arg must be OrderType.FOK
    assert args[1] is OrderType.FOK or args[1] == OrderType.FOK


@pytest.mark.asyncio
async def test_place_fok_create_and_post_NOT_used():
    """Legacy one-shot combined call MUST NOT be used."""
    client = MagicMock()
    client.get_orders = MagicMock(return_value=[])
    client.create_order = MagicMock(return_value="SIGNED")
    client.post_order = MagicMock(return_value=_good_post_response())
    client.create_and_post_order = MagicMock()
    adapter = _make_adapter(client=client)
    await adapter.place_fok("ARB-1", "TOKEN", "C", "yes", 0.55, 10)
    assert not client.create_and_post_order.called, (
        "Legacy one-shot combined call must not be used"
    )


# --- Refusal / failure paths -----------------------------------------------

@pytest.mark.asyncio
async def test_place_fok_returns_failed_when_no_wallet():
    client = MagicMock()
    adapter = _make_adapter(client=client, private_key="")
    order = await adapter.place_fok("ARB-X", "T", "C", "yes", 0.55, 10)
    assert order.status == OrderStatus.FAILED
    assert "wallet not configured" in order.error.lower()
    assert not client.create_order.called


@pytest.mark.asyncio
async def test_place_fok_returns_failed_when_factory_returns_none():
    adapter = _make_adapter(client=None)
    order = await adapter.place_fok("ARB-X", "T", "C", "yes", 0.55, 10)
    assert order.status == OrderStatus.FAILED
    assert "Unable to initialize Polymarket client" in order.error


@pytest.mark.asyncio
async def test_place_fok_circuit_open_short_circuits():
    client = MagicMock()
    adapter = _make_adapter(client=client, can_execute=False)
    order = await adapter.place_fok("ARB-X", "T", "C", "yes", 0.55, 10)
    assert order.status == OrderStatus.FAILED
    assert "circuit open" in order.error.lower()
    assert not client.create_order.called


# --- Reconcile-before-retry (Pitfall 2) ------------------------------------

@pytest.mark.asyncio
async def test_place_fok_reconcile_finds_existing_order_skips_resubmit():
    client = MagicMock()
    # Pre-check returns an existing matching order — adapter must NOT submit
    client.get_orders = MagicMock(return_value=[
        {"id": "P-EXISTING", "price": 0.55, "size": 10.0, "side": "BUY"},
    ])
    client.create_order = MagicMock()
    client.post_order = MagicMock()
    adapter = _make_adapter(client=client)
    order = await adapter.place_fok(
        "ARB-RECONCILE", "TOKEN", "C", "yes", 0.55, 10,
    )
    assert order.order_id == "P-EXISTING"
    assert not client.create_order.called, (
        "Reconcile path must not call create_order"
    )
    assert not client.post_order.called, (
        "Reconcile path must not call post_order"
    )


@pytest.mark.asyncio
async def test_place_fok_timeout_then_reconcile_finds_order():
    """First attempt times out; second attempt's pre-check finds the order.
    NO duplicate POST — this is the critical Pitfall 2 mitigation test."""
    client = MagicMock()

    pre_check_calls: list = []

    def _get_orders(market):
        pre_check_calls.append(market)
        if len(pre_check_calls) == 1:
            return []
        return [{"id": "P-LATE", "price": 0.55, "size": 10.0, "side": "BUY"}]

    client.get_orders = MagicMock(side_effect=_get_orders)
    client.create_order = MagicMock(side_effect=asyncio.TimeoutError("timeout"))
    client.post_order = MagicMock()  # never called

    adapter = _make_adapter(client=client)
    order = await adapter.place_fok("ARB-LATE", "TOKEN", "C", "yes", 0.55, 10)
    assert order.order_id == "P-LATE"
    assert client.get_orders.call_count == 2
    # post_order MUST NOT be called — that's the safety invariant
    assert not client.post_order.called


@pytest.mark.asyncio
async def test_place_fok_max_attempts_exhausted():
    client = MagicMock()
    client.get_orders = MagicMock(return_value=[])
    client.create_order = MagicMock(
        side_effect=asyncio.TimeoutError("never recovers"),
    )
    client.post_order = MagicMock()
    adapter = _make_adapter(client=client)
    order = await adapter.place_fok("ARB-DEAD", "TOKEN", "C", "yes", 0.55, 10)
    assert order.status == OrderStatus.FAILED
    assert "max attempts exhausted" in order.error.lower()


@pytest.mark.asyncio
async def test_place_fok_non_timeout_exception_bails_immediately():
    client = MagicMock()
    client.get_orders = MagicMock(return_value=[])
    client.create_order = MagicMock(side_effect=ValueError("bad signature"))
    client.post_order = MagicMock()
    adapter = _make_adapter(client=client)
    order = await adapter.place_fok("ARB-BAD", "TOKEN", "C", "yes", 0.55, 10)
    assert order.status == OrderStatus.FAILED
    assert "Polymarket order exception" in order.error
    assert "bad signature" in order.error
    # Bail fast: only one attempt's worth of get_orders
    assert client.get_orders.call_count == 1


# --- check_depth + Pitfall 1 stale-book guard ------------------------------

@pytest.mark.asyncio
async def test_check_depth_sufficient():
    client = MagicMock()
    client.get_order_book = MagicMock(return_value={
        "asks": [[0.55, 5], [0.56, 10], [0.57, 20]],
        "bids": [[0.54, 5]],
    })
    client.get_price = MagicMock(return_value=0.55)
    adapter = _make_adapter(client=client)
    sufficient, best = await adapter.check_depth("TOKEN", "yes", required_qty=10)
    assert sufficient is True
    assert abs(best - 0.55) < 1e-6


@pytest.mark.asyncio
async def test_check_depth_stale_book_refuses_when_tick_above_ask():
    client = MagicMock()
    client.get_order_book = MagicMock(return_value={
        "asks": [[0.55, 100]],
        "bids": [[0.54, 100]],
    })
    # Tick is 0.62 — way above the cached ask (>1¢ outside spread) → stale
    client.get_price = MagicMock(return_value=0.62)
    adapter = _make_adapter(client=client)
    sufficient, best = await adapter.check_depth("TOKEN", "yes", required_qty=10)
    assert sufficient is False
    assert best == 0.0


@pytest.mark.asyncio
async def test_check_depth_stale_book_refuses_when_tick_below_bid():
    client = MagicMock()
    client.get_order_book = MagicMock(return_value={
        "asks": [[0.55, 100]],
        "bids": [[0.54, 100]],
    })
    # Tick is 0.50 — way below cached bid (>1¢ outside spread)
    client.get_price = MagicMock(return_value=0.50)
    adapter = _make_adapter(client=client)
    sufficient, best = await adapter.check_depth("TOKEN", "yes", required_qty=10)
    assert sufficient is False
    assert best == 0.0


@pytest.mark.asyncio
async def test_check_depth_empty_book_refuses():
    client = MagicMock()
    client.get_order_book = MagicMock(return_value={"asks": [], "bids": []})
    client.get_price = MagicMock(return_value=0.55)
    adapter = _make_adapter(client=client)
    sufficient, best = await adapter.check_depth("TOKEN", "yes", required_qty=1)
    assert sufficient is False
    assert best == 0.0


@pytest.mark.asyncio
async def test_check_depth_exception_returns_false():
    client = MagicMock()
    client.get_order_book = MagicMock(side_effect=RuntimeError("network"))
    client.get_price = MagicMock(return_value=0.55)
    adapter = _make_adapter(client=client)
    sufficient, best = await adapter.check_depth("TOKEN", "yes", required_qty=10)
    assert sufficient is False
    assert best == 0.0


# --- best_executable_price ------------------------------------------------

@pytest.mark.asyncio
async def test_best_executable_price_walks_levels():
    client = MagicMock()
    client.get_order_book = MagicMock(return_value={
        "asks": [[0.55, 3], [0.56, 4], [0.57, 5]],
        "bids": [[0.54, 5]],
    })
    client.get_price = MagicMock(return_value=0.55)
    adapter = _make_adapter(client=client)
    fillable, price = await adapter.best_executable_price(
        "TOKEN", "yes", required_qty=10,
    )
    assert fillable is True
    assert abs(price - 0.57) < 1e-6


@pytest.mark.asyncio
async def test_best_executable_price_returns_top_when_top_level_sufficient():
    client = MagicMock()
    client.get_order_book = MagicMock(return_value={
        "asks": [[0.55, 50]],
        "bids": [[0.54, 5]],
    })
    client.get_price = MagicMock(return_value=0.55)
    adapter = _make_adapter(client=client)
    fillable, price = await adapter.best_executable_price(
        "TOKEN", "yes", required_qty=10,
    )
    assert fillable is True
    assert abs(price - 0.55) < 1e-6


@pytest.mark.asyncio
async def test_best_executable_price_insufficient_returns_false():
    client = MagicMock()
    client.get_order_book = MagicMock(return_value={
        "asks": [[0.55, 3], [0.56, 2]],
        "bids": [[0.54, 5]],
    })
    client.get_price = MagicMock(return_value=0.55)
    adapter = _make_adapter(client=client)
    fillable, price = await adapter.best_executable_price(
        "TOKEN", "yes", required_qty=10,
    )
    assert fillable is False


@pytest.mark.asyncio
async def test_best_executable_price_empty_book_returns_false():
    client = MagicMock()
    client.get_order_book = MagicMock(return_value={"asks": [], "bids": []})
    client.get_price = MagicMock(return_value=0.55)
    adapter = _make_adapter(client=client)
    fillable, price = await adapter.best_executable_price(
        "TOKEN", "yes", required_qty=1,
    )
    assert fillable is False
    assert price == 0.0


# --- place_unwind_sell ----------------------------------------------------

@pytest.mark.asyncio
async def test_place_unwind_sell_uses_sell_side_and_ioc_at_panic_price():
    from py_clob_client.clob_types import OrderType
    client = MagicMock()
    client.create_order = MagicMock(return_value="SIGNED")
    client.post_order = MagicMock(return_value={
        "success": True,
        "orderID": "P-UNWIND",
        "status": "matched",
        "size_matched": 10,
    })
    adapter = _make_adapter(client=client)
    order = await adapter.place_unwind_sell(
        "ARB-1-UNWIND", "TOKEN", "C", "yes", qty=10,
    )
    assert order.status == OrderStatus.FILLED
    assert order.fill_qty == 10.0
    # Verify the OrderArgs used: SELL side, panic price 0.01
    args = client.create_order.call_args.args[0]
    assert args.side == "SELL"
    assert abs(args.price - 0.01) < 1e-9
    assert args.size == 10.0
    # FAK type (Fill-And-Kill, IOC-equivalent on the CLOB) so partial
    # fills are accepted instead of the whole order being rejected.
    post_kwargs = client.post_order.call_args
    assert OrderType.FAK == post_kwargs.args[1]


@pytest.mark.asyncio
async def test_place_unwind_sell_partial_fill_marked_partial():
    client = MagicMock()
    client.create_order = MagicMock(return_value="SIGNED")
    client.post_order = MagicMock(return_value={
        "success": True,
        "orderID": "P-UNWIND-2",
        "status": "matched",
        "size_matched": 7,  # only 7 of 10 filled
    })
    adapter = _make_adapter(client=client)
    order = await adapter.place_unwind_sell(
        "ARB-2-UNWIND", "TOKEN", "C", "yes", qty=10,
    )
    assert order.status == OrderStatus.PARTIAL
    assert order.fill_qty == 7.0


@pytest.mark.asyncio
async def test_place_unwind_sell_failed_response_returns_failed():
    client = MagicMock()
    client.create_order = MagicMock(return_value="SIGNED")
    client.post_order = MagicMock(return_value={
        "success": False,
        "errorMsg": "no liquidity",
    })
    adapter = _make_adapter(client=client)
    order = await adapter.place_unwind_sell(
        "ARB-3", "TOKEN", "C", "yes", qty=10,
    )
    assert order.status == OrderStatus.FAILED
    assert "no liquidity" in (order.error or "")


@pytest.mark.asyncio
async def test_place_unwind_sell_circuit_open_returns_failed_without_call():
    client = MagicMock()
    client.create_order = MagicMock()
    client.post_order = MagicMock()
    adapter = _make_adapter(client=client, can_execute=False)
    order = await adapter.place_unwind_sell(
        "ARB-CO", "TOKEN", "C", "yes", qty=10,
    )
    assert order.status == OrderStatus.FAILED
    assert "circuit open" in (order.error or "").lower()
    assert not client.create_order.called
    assert not client.post_order.called


# --- cancel_order ----------------------------------------------------------

@pytest.mark.asyncio
async def test_cancel_returns_true_on_first_method():
    client = MagicMock(spec=["cancel"])
    client.cancel = MagicMock(return_value={"success": True})
    adapter = _make_adapter(client=client)
    order = Order(
        order_id="P-X", platform="polymarket",
        market_id="T", canonical_id="C",
        side="yes", price=0.5, quantity=1,
        status=OrderStatus.SUBMITTED,
    )
    assert await adapter.cancel_order(order) is True
    assert client.cancel.called


@pytest.mark.asyncio
async def test_cancel_returns_false_on_no_client():
    adapter = _make_adapter(client=None)
    order = Order(
        order_id="P-X", platform="polymarket",
        market_id="T", canonical_id="C",
        side="yes", price=0.5, quantity=1,
        status=OrderStatus.SUBMITTED,
    )
    assert await adapter.cancel_order(order) is False


# --- list_open_orders_by_client_id -----------------------------------------

@pytest.mark.asyncio
async def test_list_open_orders_returns_empty_with_warning():
    client = MagicMock()
    adapter = _make_adapter(client=client)
    result = await adapter.list_open_orders_by_client_id("ARB-000001")
    assert result == []
    # Second call should also return [] but not warn again — sanity
    result2 = await adapter.list_open_orders_by_client_id("ARB-000002")
    assert result2 == []


# --- Polymarket adapter does NOT use @transient_retry on order POST --------

def test_polymarket_does_not_decorate_place_fok_with_transient_retry():
    """Critical safety invariant — transient_retry on a non-idempotent POST
    creates duplicate orders. Verify by inspecting attributes that tenacity
    sets on decorated functions (`.retry` / `.statistics`)."""
    method = PolymarketAdapter.place_fok
    assert not hasattr(method, "retry"), (
        "PolymarketAdapter.place_fok must NOT be wrapped with tenacity @retry "
        "— Pitfall 2"
    )
    assert not hasattr(method, "statistics"), (
        "PolymarketAdapter.place_fok appears tenacity-decorated"
    )


# --- CR-02 parity: external_client_order_id is None on Polymarket -----------

@pytest.mark.asyncio
async def test_place_fok_returns_external_client_order_id_none():
    """CR-02 parity: Polymarket has no client_order_id concept; the field is
    None on the returned Order on success.
    """
    client = MagicMock()
    client.get_orders = MagicMock(return_value=[])
    client.create_order = MagicMock(return_value="SIGNED")
    client.post_order = MagicMock(return_value=_good_post_response("P-42"))
    adapter = _make_adapter(client=client)
    order = await adapter.place_fok("ARB-1", "TOKEN-A", "DEM_HOUSE", "yes", 0.55, 10)
    assert order.status == OrderStatus.FILLED
    assert order.external_client_order_id is None


# Alias for VALIDATION.md row 02.1-01-08 naming
test_place_fok_leaves_external_client_order_id_none = test_place_fok_returns_external_client_order_id_none


# --- SAFE-04: rate-limiter acquire-before-SDK and 429 handling -------------


@pytest.mark.asyncio
async def test_place_fok_acquires_rate_token_before_sdk():
    """SAFE-04: rate_limiter.acquire() MUST be awaited BEFORE any SDK call
    (create_order / post_order) inside the reconcile loop.
    """
    call_log: list = []

    async def _acquire():
        call_log.append("rate_limiter.acquire")

    client = MagicMock()
    client.get_orders = MagicMock(
        side_effect=lambda **kw: (call_log.append("client.get_orders"), [])[-1],
    )
    client.create_order = MagicMock(
        side_effect=lambda *a, **kw: (call_log.append("client.create_order"), "SIGNED")[-1],
    )
    client.post_order = MagicMock(
        side_effect=lambda *a, **kw: (
            call_log.append("client.post_order"), _good_post_response("P-RL-1"),
        )[-1],
    )

    adapter = PolymarketAdapter(
        config=_config(),
        clob_client_factory=lambda: client,
        rate_limiter=SimpleNamespace(
            acquire=AsyncMock(side_effect=_acquire),
            apply_retry_after=MagicMock(return_value=2.0),
        ),
        circuit=_circuit(True),
    )
    order = await adapter.place_fok(
        "ARB-RL-1", "TOKEN", "C", "yes", 0.55, 10,
    )
    # SDK must have been called
    assert "client.create_order" in call_log
    assert "client.post_order" in call_log
    # acquire must precede create_order and post_order
    acquire_idx = call_log.index("rate_limiter.acquire")
    create_idx = call_log.index("client.create_order")
    post_idx = call_log.index("client.post_order")
    assert acquire_idx < create_idx, (
        f"rate_limiter.acquire must come before client.create_order "
        f"(acquire={acquire_idx} create={create_idx} log={call_log})"
    )
    assert acquire_idx < post_idx, (
        f"rate_limiter.acquire must come before client.post_order "
        f"(acquire={acquire_idx} post={post_idx} log={call_log})"
    )
    assert order.status == OrderStatus.FILLED


@pytest.mark.asyncio
async def test_429_via_sdk_exception_applies_retry_after():
    """SAFE-04: When the py-clob-client SDK surfaces a 429 as an exception,
    the adapter calls apply_retry_after, records circuit failure, and returns
    a FAILED order whose error contains 'rate_limited'. NO retry for FOK.
    """
    client = MagicMock()
    client.get_orders = MagicMock(return_value=[])
    # The SDK raises an exception whose message contains "429" — this is how
    # py-clob-client signals HTTP 429 back to callers.
    client.create_order = MagicMock(
        side_effect=Exception("HTTP 429 Too Many Requests: rate limit exceeded"),
    )
    client.post_order = MagicMock()

    rate_limiter = SimpleNamespace(
        acquire=AsyncMock(),
        apply_retry_after=MagicMock(return_value=2.0),
    )
    circuit = _circuit(True)
    adapter = PolymarketAdapter(
        config=_config(),
        clob_client_factory=lambda: client,
        rate_limiter=rate_limiter,
        circuit=circuit,
    )
    order = await adapter.place_fok("ARB-RL-429", "TOKEN", "C", "yes", 0.55, 10)

    # apply_retry_after must have been called with reason="polymarket_429"
    assert rate_limiter.apply_retry_after.called, (
        "apply_retry_after not invoked when SDK signaled 429"
    )
    _args, kwargs = rate_limiter.apply_retry_after.call_args
    assert kwargs.get("reason") == "polymarket_429", (
        f"expected reason='polymarket_429', got {kwargs.get('reason')!r}"
    )

    # Circuit failure recorded
    assert circuit.record_failure.called

    # FAILED order with 'rate_limited' in the error
    assert order.status == OrderStatus.FAILED
    assert "rate_limited" in (order.error or "").lower(), (
        f"expected 'rate_limited' in order.error, got {order.error!r}"
    )

    # post_order NOT called — create_order raised 429, we should bail not retry
    assert not client.post_order.called, (
        "post_order must not be called after a 429 on create_order"
    )


# --- SAFE-05: cancel_all full implementation via client.cancel_all() -------


@pytest.mark.asyncio
async def test_cancel_all_invokes_sdk_and_returns_canceled():
    """SAFE-05: PolymarketAdapter.cancel_all invokes client.cancel_all() via
    run_in_executor after acquiring a rate-limit token, and returns the
    'canceled' list from the SDK response.
    """
    client = MagicMock()
    client.cancel_all = MagicMock(
        return_value={"canceled": ["a", "b", "c"], "not_canceled": []},
    )

    call_log: list = []

    async def _acquire():
        call_log.append("rate_limiter.acquire")

    rate_limiter = SimpleNamespace(
        acquire=AsyncMock(side_effect=_acquire),
        apply_retry_after=MagicMock(return_value=2.0),
    )
    adapter = PolymarketAdapter(
        config=_config(),
        clob_client_factory=lambda: client,
        rate_limiter=rate_limiter,
        circuit=_circuit(True),
    )

    result = await adapter.cancel_all()

    assert result == ["a", "b", "c"], (
        f"expected ['a','b','c'] from SDK 'canceled' field, got {result!r}"
    )
    assert client.cancel_all.called, "SDK cancel_all not invoked"
    assert rate_limiter.acquire.await_count >= 1, (
        f"rate_limiter.acquire must fire before SDK call; "
        f"await_count={rate_limiter.acquire.await_count}"
    )


@pytest.mark.asyncio
async def test_cancel_all_returns_empty_list_when_client_missing():
    """No ClobClient → cancel_all returns []; does NOT raise."""
    adapter = _make_adapter(client=None)
    result = await adapter.cancel_all()
    assert result == []


@pytest.mark.asyncio
async def test_cancel_all_swallows_sdk_exception():
    """SDK raises mid-call → cancel_all logs and returns [] (never raises)."""
    client = MagicMock()
    client.cancel_all = MagicMock(side_effect=RuntimeError("network down"))
    adapter = _make_adapter(client=client)
    result = await adapter.cancel_all()
    assert result == []
