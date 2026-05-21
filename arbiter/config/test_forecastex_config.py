"""
Tests for ForecastEx-specific config and fee math.

Covers:
  - forecastex_order_fee math (flat $0.005/contract, independent of price)
  - ForecastExConfig env-var wiring (enabled / paper / gateway URL / account)
  - validate_live_config check for enabled+missing-account-id
  - load_config's enabled→None collapse for two-platform-only deployments
"""
from __future__ import annotations

import pytest

from arbiter.config import settings as settings_mod
from arbiter.config.settings import (
    AlertConfig,
    ArbiterConfig,
    FORECASTEX_TAKER_FEE_PER_CONTRACT,
    ForecastExConfig,
    forecastex_fee,
    forecastex_order_fee,
    validate_live_config,
)


# ── Fee math ──────────────────────────────────────────────────────────────


def test_fee_constant_is_half_cent():
    assert FORECASTEX_TAKER_FEE_PER_CONTRACT == 0.005


def test_fee_is_flat_regardless_of_price():
    """Flat fee — same dollar amount whether buying a 1¢ or 99¢ contract."""
    a = forecastex_order_fee(price=0.01, quantity=100)
    b = forecastex_order_fee(price=0.99, quantity=100)
    assert a == b == pytest.approx(0.50)


def test_fee_scales_linearly_with_quantity():
    assert forecastex_order_fee(0.5, 1) == pytest.approx(0.005)
    assert forecastex_order_fee(0.5, 10) == pytest.approx(0.05)
    assert forecastex_order_fee(0.5, 1000) == pytest.approx(5.0)


def test_fee_zero_when_qty_or_price_invalid():
    assert forecastex_order_fee(price=0.5, quantity=0) == 0.0
    assert forecastex_order_fee(price=0.0, quantity=100) == 0.0
    assert forecastex_order_fee(price=-0.1, quantity=10) == 0.0


def test_per_contract_helper_returns_unit_fee():
    # forecastex_fee returns per-contract cost (matches polymarket_fee / kalshi_fee shape)
    assert forecastex_fee(price=0.5, quantity=100) == pytest.approx(0.005)


def test_custom_fee_per_contract_override():
    assert forecastex_order_fee(0.5, 100, fee_per_contract=0.01) == pytest.approx(1.0)


def test_fee_clamps_negative_per_contract_override():
    assert forecastex_order_fee(0.5, 100, fee_per_contract=-1.0) == 0.0


# ── ForecastExConfig env-var wiring ────────────────────────────────────────


def test_config_defaults_when_no_env(monkeypatch):
    for key in (
        "FORECASTEX_ENABLED",
        "IBKR_GATEWAY_URL",
        "IBKR_ACCOUNT_ID",
        "IBKR_PAPER_TRADING",
        "IBKR_VERIFY_SSL",
    ):
        monkeypatch.delenv(key, raising=False)
    cfg = ForecastExConfig()
    assert cfg.enabled is False
    assert cfg.gateway_url == "https://localhost:5000/v1/api"
    assert cfg.account_id == ""
    assert cfg.paper_trading is True
    assert cfg.verify_ssl is False


def test_config_reads_env(monkeypatch):
    monkeypatch.setenv("FORECASTEX_ENABLED", "true")
    monkeypatch.setenv("IBKR_GATEWAY_URL", "https://gateway.local:5005/v1/api")
    monkeypatch.setenv("IBKR_ACCOUNT_ID", "U7654321")
    monkeypatch.setenv("IBKR_PAPER_TRADING", "false")
    monkeypatch.setenv("IBKR_VERIFY_SSL", "true")
    cfg = ForecastExConfig()
    assert cfg.enabled is True
    assert cfg.gateway_url == "https://gateway.local:5005/v1/api"
    assert cfg.account_id == "U7654321"
    assert cfg.paper_trading is False
    assert cfg.verify_ssl is True


# ── validate_live_config integration ───────────────────────────────────────


def test_validate_flags_enabled_without_account_id():
    cfg = ArbiterConfig()
    cfg.forecastex = ForecastExConfig()
    cfg.forecastex.enabled = True
    cfg.forecastex.account_id = ""
    errors = validate_live_config(cfg)
    msg = " ".join(errors)
    assert "FORECASTEX_ENABLED=true" in msg and "IBKR_ACCOUNT_ID" in msg


def test_validate_accepts_enabled_with_account_id():
    cfg = ArbiterConfig()
    cfg.forecastex = ForecastExConfig()
    cfg.forecastex.enabled = True
    cfg.forecastex.account_id = "DU1234567"
    errors = validate_live_config(cfg)
    assert not any("FORECASTEX" in e for e in errors)


def test_validate_skips_when_disabled():
    cfg = ArbiterConfig()
    cfg.forecastex = None
    errors = validate_live_config(cfg)
    assert not any("FORECASTEX" in e for e in errors)


# ── AlertConfig wires forecastex_low ───────────────────────────────────────


def test_alert_config_has_forecastex_threshold_default():
    cfg = AlertConfig()
    assert cfg.forecastex_low == 50.0


# ── load_config disabled → None collapse ───────────────────────────────────


def test_load_config_collapses_disabled_to_none(monkeypatch):
    """Disabled (default) means cfg.forecastex == None so all of main.py's
    `if cfg.forecastex is not None` guards short-circuit cleanly."""
    monkeypatch.delenv("FORECASTEX_ENABLED", raising=False)
    cfg = settings_mod.load_config()
    assert cfg.forecastex is None


def test_load_config_keeps_enabled_config(monkeypatch):
    monkeypatch.setenv("FORECASTEX_ENABLED", "true")
    monkeypatch.setenv("IBKR_ACCOUNT_ID", "DU1234567")
    cfg = settings_mod.load_config()
    assert cfg.forecastex is not None
    assert cfg.forecastex.enabled is True
    assert cfg.forecastex.account_id == "DU1234567"
