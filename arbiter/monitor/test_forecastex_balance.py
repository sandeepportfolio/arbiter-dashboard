"""
Tests for BalanceMonitor's ForecastEx integration:
  - threshold defaulting to AlertConfig.forecastex_low
  - polling via collector.fetch_balance() (platform-agnostic loop)
  - low-balance alert routed through the Telegram notifier
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from arbiter.config.settings import AlertConfig
from arbiter.monitor.balance import BalanceMonitor


def _make_collector(balance: float):
    c = MagicMock()
    c.fetch_balance = AsyncMock(return_value=balance)
    c.balance_source = "forecastex:ibkr-gateway"
    return c


@pytest.fixture
def alert_config():
    cfg = AlertConfig(
        telegram_bot_token="",
        telegram_chat_id="",
        telegram_alerts_chat_id="",
        kalshi_low=50.0,
        polymarket_low=25.0,
        forecastex_low=75.0,
        cooldown=300.0,
    )
    return cfg


async def test_monitor_uses_forecastex_threshold(alert_config):
    monitor = BalanceMonitor(alert_config, {"forecastex": _make_collector(100.0)})
    assert monitor._thresholds["forecastex"] == 75.0


async def test_monitor_polls_forecastex_balance(alert_config):
    coll = _make_collector(200.0)
    monitor = BalanceMonitor(alert_config, {"forecastex": coll})
    snaps = await monitor.check_balances()
    assert "forecastex" in snaps
    assert snaps["forecastex"].balance == 200.0
    assert snaps["forecastex"].source == "forecastex:ibkr-gateway"
    coll.fetch_balance.assert_awaited()


async def test_monitor_flags_low_forecastex_balance(alert_config):
    coll = _make_collector(10.0)  # under 75
    monitor = BalanceMonitor(alert_config, {"forecastex": coll})
    monitor.notifier.send = AsyncMock(return_value=True)
    snaps = await monitor.check_balances()
    assert snaps["forecastex"].is_low is True
    monitor.notifier.send.assert_awaited()
    msg = monitor.notifier.send.await_args.args[0]
    assert "FORECASTEX" in msg


async def test_monitor_default_threshold_when_missing(alert_config):
    """If the legacy AlertConfig didn't have forecastex_low, the monitor
    must still gracefully default rather than KeyError."""
    cfg = AlertConfig(
        telegram_bot_token="",
        telegram_chat_id="",
        telegram_alerts_chat_id="",
        kalshi_low=50.0,
        polymarket_low=25.0,
        cooldown=300.0,
    )
    # Simulate older release that didn't have the attribute.
    delattr(cfg, "forecastex_low")
    monitor = BalanceMonitor(cfg, {"forecastex": _make_collector(100.0)})
    assert monitor._thresholds["forecastex"] == 50.0
