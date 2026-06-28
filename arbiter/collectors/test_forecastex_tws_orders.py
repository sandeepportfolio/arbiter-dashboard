"""TDD for ForecastEx TWS client *live-safety* behaviours (task: FX transport
readiness). These guard the switchover to live ForecastEx trading:

  * ``_assert_safe_routing`` — refuse paper/live ↔ account-prefix mismatches and
    require an explicit opt-in before routing real orders (the diagnosis's
    "do NOT bare-flip IBKR_USE_TWS" guard).
  * BUY-only — ForecastEx forbids selling; ``place_order`` must reject SELL.
  * FOK → IOC — ForecastEx supports only DAY/GTC/IOC; FOK must be normalised.
  * ``preview_order`` — a whatIf preview that verifies permission/margin WITHOUT
    executing, so the first live check is safe.
  * open-order resync on (re)connect — survive the nightly IBC restart.
  * IOC partial-fill drain — never report a real partial fill as zero.

All tests use the guarded-import + patched-``_ensure_connected`` pattern so they
run without a live IB Gateway. ``ib_async`` is installed, so ``Order`` is real.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from arbiter.collectors.forecastex_tws import ForecastExTWSClient


LIVE_ACCT = "U2595308"
PAPER_ACCT = "DU1234567"


# ── Routing safety guard ────────────────────────────────────────────────

def test_routing_paper_with_paper_account_ok():
    c = ForecastExTWSClient(account_id=PAPER_ACCT, paper_trading=True)
    c._assert_safe_routing()  # must not raise


def test_routing_paper_with_live_account_raises():
    # paper_trading=True but pointed at a live U-account → would route REAL
    # orders while the system thinks it is simulating. Must refuse.
    c = ForecastExTWSClient(account_id=LIVE_ACCT, paper_trading=True)
    with pytest.raises(RuntimeError, match="paper_trading=True"):
        c._assert_safe_routing()


def test_routing_live_without_optin_raises(monkeypatch):
    monkeypatch.delenv("IBKR_TWS_ALLOW_LIVE", raising=False)
    c = ForecastExTWSClient(account_id=LIVE_ACCT, paper_trading=False)
    with pytest.raises(RuntimeError, match="IBKR_TWS_ALLOW_LIVE"):
        c._assert_safe_routing()


def test_routing_live_with_optin_and_live_account_ok(monkeypatch):
    monkeypatch.setenv("IBKR_TWS_ALLOW_LIVE", "true")
    c = ForecastExTWSClient(account_id=LIVE_ACCT, paper_trading=False)
    c._assert_safe_routing()  # must not raise


def test_routing_live_with_optin_but_paper_account_raises(monkeypatch):
    monkeypatch.setenv("IBKR_TWS_ALLOW_LIVE", "true")
    c = ForecastExTWSClient(account_id=PAPER_ACCT, paper_trading=False)
    with pytest.raises(RuntimeError, match="paper"):
        c._assert_safe_routing()


# ── place_order helpers ─────────────────────────────────────────────────

def _fake_trade(order, contract, *, status="Filled", filled=1.0, avg=0.5, fills=None):
    return SimpleNamespace(
        order=order,
        orderStatus=SimpleNamespace(
            status=status, filled=filled, avgFillPrice=avg,
            orderId=getattr(order, "orderId", 0) or 77,
        ),
        contract=contract,
        fills=fills or [],
    )


def _fake_ib_capturing(captured, **trade_kw):
    def place_order(contract, order):
        captured["order"] = order
        captured["contract"] = contract
        return _fake_trade(order, contract, **trade_kw)

    return SimpleNamespace(isConnected=lambda: True, placeOrder=place_order)


def _patch_conn(client, fake_ib):
    client._ensure_connected = AsyncMock(return_value=fake_ib)
    client._contract_for_conid = AsyncMock(return_value=SimpleNamespace(conId=123))


# ── BUY-only ────────────────────────────────────────────────────────────

async def test_place_order_rejects_sell():
    c = ForecastExTWSClient(account_id=LIVE_ACCT, paper_trading=True)
    with pytest.raises(ValueError, match="BUY"):
        await c.place_order(conid="123", side="SELL", price=0.5, quantity=1)


# ── FOK → IOC normalisation ─────────────────────────────────────────────

async def test_place_order_normalizes_fok_to_ioc():
    c = ForecastExTWSClient(account_id=LIVE_ACCT, paper_trading=True)
    captured: dict = {}
    _patch_conn(c, _fake_ib_capturing(captured))
    await c.place_order(conid="123", side="BUY", price=0.5, quantity=1, tif="FOK")
    assert captured["order"].tif == "IOC"


async def test_place_order_keeps_ioc():
    c = ForecastExTWSClient(account_id=LIVE_ACCT, paper_trading=True)
    captured: dict = {}
    _patch_conn(c, _fake_ib_capturing(captured))
    await c.place_order(conid="123", side="BUY", price=0.5, quantity=1, tif="IOC")
    assert captured["order"].tif == "IOC"


# ── whatIf preview (no execution) ───────────────────────────────────────

async def test_preview_order_uses_whatif_not_placeorder():
    c = ForecastExTWSClient(account_id=LIVE_ACCT, paper_trading=True)
    state = SimpleNamespace(
        commission=0.01, initMarginChange="1.00", maintMarginChange="1.00",
        warningText="", equityWithLoanChange="0",
    )

    def _boom(*a, **k):  # placeOrder must never be called by a preview
        raise AssertionError("preview_order must not place an order")

    fake_ib = SimpleNamespace(
        isConnected=lambda: True,
        whatIfOrderAsync=AsyncMock(return_value=state),
        placeOrder=_boom,
    )
    _patch_conn(c, fake_ib)
    out = await c.preview_order(conid="123", side="BUY", price=0.5, quantity=1)
    fake_ib.whatIfOrderAsync.assert_awaited()
    assert isinstance(out, dict)
    assert "init_margin" in out and "commission" in out


# ── Open-order resync on (re)connect ────────────────────────────────────

async def test_resync_open_orders_called():
    c = ForecastExTWSClient(account_id=LIVE_ACCT, paper_trading=True)
    fake_ib = SimpleNamespace(reqAllOpenOrdersAsync=AsyncMock(return_value=[]))
    await c._resync_open_orders(fake_ib)
    fake_ib.reqAllOpenOrdersAsync.assert_awaited()


async def test_resync_open_orders_tolerates_missing_method():
    c = ForecastExTWSClient(account_id=LIVE_ACCT, paper_trading=True)
    # An IB stub without the method must not raise.
    await c._resync_open_orders(SimpleNamespace())


# ── IOC partial-fill drain ──────────────────────────────────────────────

def test_trade_dict_reports_fills_when_status_filled_zero():
    # Cancelled IOC whose orderStatus.filled rounded to 0 but an execution
    # actually printed — must report the real filled qty, never silent zero.
    fill = SimpleNamespace(
        execution=SimpleNamespace(shares=1.0, price=0.5, avgPrice=0.5)
    )
    trade = SimpleNamespace(
        order=SimpleNamespace(orderId=5, permId=0, action="BUY"),
        orderStatus=SimpleNamespace(
            status="Cancelled", filled=0.0, avgFillPrice=0.0, orderId=5
        ),
        contract=SimpleNamespace(conId=123),
        fills=[fill],
    )
    d = ForecastExTWSClient._trade_to_dict(trade, trade.contract, "BUY")
    assert d["filled_quantity"] == 1.0
    assert d["avg_price"] == 0.5
