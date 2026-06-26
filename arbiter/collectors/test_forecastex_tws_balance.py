"""Unit tests for the ForecastEx TWS client's ``account_balance`` parser.

These tests do NOT require ``ib_async`` to be installed: the module imports
cleanly (guarded import) and we patch ``_ensure_connected`` to return a fake
``IB`` whose ``accountSummaryAsync`` yields ib_async-style ``AccountValue``
rows (objects with ``account`` / ``tag`` / ``value`` / ``currency``).

The parser must mirror the REST client's preference order
(AvailableFunds → TotalCashValue → CashBalance) and filter to the configured
account in USD/BASE.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from arbiter.collectors.forecastex_tws import ForecastExTWSClient


ACCOUNT = "U2595308"


def _row(tag, value, account=ACCOUNT, currency="USD"):
    return SimpleNamespace(account=account, tag=tag, value=value, currency=currency)


def _client_with_summary(rows):
    client = ForecastExTWSClient(account_id=ACCOUNT)
    fake_ib = SimpleNamespace(accountSummaryAsync=AsyncMock(return_value=rows))
    client._ensure_connected = AsyncMock(return_value=fake_ib)
    return client


async def test_tws_balance_prefers_available_funds():
    client = _client_with_summary([
        _row("AvailableFunds", "123.45"),
        _row("TotalCashValue", "999.99"),
        _row("CashBalance", "888.00"),
    ])
    assert await client.account_balance() == 123.45


async def test_tws_balance_falls_back_to_total_cash():
    client = _client_with_summary([
        _row("TotalCashValue", "77.0"),
        _row("CashBalance", "55.0"),
    ])
    assert await client.account_balance() == 77.0


async def test_tws_balance_falls_back_to_cash_balance():
    client = _client_with_summary([_row("CashBalance", "55.5")])
    assert await client.account_balance() == 55.5


async def test_tws_balance_returns_zero_when_no_cash_tags():
    client = _client_with_summary([_row("NetLiquidation", "1000.0")])
    assert await client.account_balance() == 0.0


async def test_tws_balance_filters_to_configured_account():
    # A row for a *different* account must be ignored; the matching-account
    # CashBalance is the only usable value here.
    client = _client_with_summary([
        _row("AvailableFunds", "5000.0", account="U9999999"),
        _row("CashBalance", "42.0", account=ACCOUNT),
    ])
    assert await client.account_balance() == 42.0


async def test_tws_balance_filters_non_usd_currency():
    # EUR AvailableFunds must be skipped; USD CashBalance is taken instead.
    client = _client_with_summary([
        _row("AvailableFunds", "5000.0", currency="EUR"),
        _row("CashBalance", "42.0", currency="USD"),
    ])
    assert await client.account_balance() == 42.0


async def test_tws_balance_accepts_base_currency():
    client = _client_with_summary([_row("AvailableFunds", "200.0", currency="BASE")])
    assert await client.account_balance() == 200.0


async def test_tws_balance_requires_account_id():
    client = ForecastExTWSClient(account_id="")
    with pytest.raises(RuntimeError, match="IBKR_ACCOUNT_ID is not configured"):
        await client.account_balance()
