"""Unit tests for the dedicated arbitrage-alerts channel routing.

Verifies that:
    - When TELEGRAM_ALERTS_CHAT_ID is unset, AlertConfig falls back to
      TELEGRAM_CHAT_ID (legacy single-channel deployments).
    - When TELEGRAM_ALERTS_CHAT_ID is set, AlertConfig exposes it and
      BalanceMonitor's TelegramNotifier binds to the alerts channel,
      NOT the pairing DM.
    - The alerts channel id wins even when both env vars are set.
"""
from __future__ import annotations

from unittest.mock import patch

from arbiter.config.settings import AlertConfig, _resolve_alerts_chat_id
from arbiter.monitor.balance import BalanceMonitor


class _FakeCollector:
    async def fetch_balance(self):
        return None


def _clear_telegram_env(monkeypatch):
    for key in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "TELEGRAM_ALERTS_CHAT_ID"):
        monkeypatch.delenv(key, raising=False)


def test_resolve_alerts_chat_id_fallback(monkeypatch):
    _clear_telegram_env(monkeypatch)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "12345")
    assert _resolve_alerts_chat_id() == "12345"


def test_resolve_alerts_chat_id_prefers_dedicated(monkeypatch):
    _clear_telegram_env(monkeypatch)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "12345")
    monkeypatch.setenv("TELEGRAM_ALERTS_CHAT_ID", "-1009999999")
    assert _resolve_alerts_chat_id() == "-1009999999"


def test_resolve_alerts_chat_id_empty_string_falls_through(monkeypatch):
    _clear_telegram_env(monkeypatch)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "12345")
    monkeypatch.setenv("TELEGRAM_ALERTS_CHAT_ID", "   ")
    assert _resolve_alerts_chat_id() == "12345"


def test_alertconfig_populates_alerts_chat_id(monkeypatch):
    _clear_telegram_env(monkeypatch)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "pairing")
    monkeypatch.setenv("TELEGRAM_ALERTS_CHAT_ID", "-1001111111")
    cfg = AlertConfig()
    assert cfg.telegram_chat_id == "pairing"
    assert cfg.telegram_alerts_chat_id == "-1001111111"


def test_balance_monitor_routes_to_alerts_channel(monkeypatch):
    _clear_telegram_env(monkeypatch)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "pairing")
    monkeypatch.setenv("TELEGRAM_ALERTS_CHAT_ID", "-1002222222")
    cfg = AlertConfig()
    monitor = BalanceMonitor(cfg, {"kalshi": _FakeCollector()})
    # The notifier the balance monitor (and by extension safety supervisor,
    # heartbeat, execution engine) uses must be bound to the alerts channel.
    assert monitor.notifier.chat_id == "-1002222222"
    assert monitor.notifier.chat_id != cfg.telegram_chat_id


def test_balance_monitor_falls_back_when_alerts_unset(monkeypatch):
    _clear_telegram_env(monkeypatch)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "pairing")
    cfg = AlertConfig()
    monitor = BalanceMonitor(cfg, {"kalshi": _FakeCollector()})
    # Legacy single-channel mode: alerts route to the same chat as pairing.
    assert monitor.notifier.chat_id == "pairing"
