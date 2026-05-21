"""
Cross-cutting integration tests for ForecastEx:
  - API platform validation list
  - main.py builders return None when disabled, return real objects when enabled
  - dashboards / templates / docker-compose alignment
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from unittest.mock import patch

import pytest

from arbiter.config.settings import (
    ArbiterConfig,
    AlertConfig,
    ForecastExConfig,
    KalshiConfig,
    PolymarketConfig,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _read(path: str) -> str:
    return (PROJECT_ROOT / path).read_text()


# ── API platform whitelist ────────────────────────────────────────────────


def test_api_deposit_handler_whitelists_forecastex():
    src = _read("arbiter/api.py")
    # New whitelist must include forecastex; legacy two-platform-only
    # message must be gone.
    assert "'forecastex'" in src
    assert "platform must be 'kalshi', 'polymarket', or 'forecastex'" in src


def test_api_emits_forecastex_low_threshold_field():
    src = _read("arbiter/api.py")
    assert "forecastex_low" in src
    # Settings PATCH must also accept forecastex_low.
    assert "alerts_patch[\"forecastex_low\"]" in src


# ── main.py wiring ────────────────────────────────────────────────────────


def test_main_imports_forecastex_adapter():
    src = _read("arbiter/main.py")
    assert "ForecastExAdapter" in src
    assert "from .execution.adapters import ForecastExAdapter" in src or \
        "ForecastExAdapter, KalshiAdapter" in src


def test_main_builders_return_none_when_disabled():
    from arbiter.main import build_forecastex_adapter, build_forecastex_collector
    from arbiter.utils.price_store import PriceStore

    cfg = ArbiterConfig()
    cfg.forecastex = None  # disabled — load_config() collapses it to None
    cfg.kalshi = KalshiConfig()
    cfg.polymarket = PolymarketConfig()
    cfg.alerts = AlertConfig()
    assert build_forecastex_collector(cfg, PriceStore()) is None
    assert build_forecastex_adapter(cfg, None) is None


def test_main_collector_built_when_enabled(monkeypatch):
    from arbiter.main import build_forecastex_collector
    from arbiter.utils.price_store import PriceStore

    cfg = ArbiterConfig()
    cfg.forecastex = ForecastExConfig()
    cfg.forecastex.enabled = True
    cfg.forecastex.account_id = "DU1234567"

    collector = build_forecastex_collector(cfg, PriceStore())
    assert collector is not None
    assert collector.client.account_id == "DU1234567"


def test_main_adapter_reuses_collector_client():
    from arbiter.main import build_forecastex_adapter, build_forecastex_collector
    from arbiter.utils.price_store import PriceStore

    cfg = ArbiterConfig()
    cfg.forecastex = ForecastExConfig()
    cfg.forecastex.enabled = True
    cfg.forecastex.account_id = "DU1234567"

    collector = build_forecastex_collector(cfg, PriceStore())
    adapter = build_forecastex_adapter(cfg, collector)
    assert adapter is not None
    assert adapter._client is collector.client


# ── Dashboard surface ─────────────────────────────────────────────────────


def test_ops_html_renders_three_platform_balance_card():
    src = _read("arbiter/web/ops.html")
    # Mock data shape, platform chip, balance accumulation.
    assert "balances.forecastex" in src or "M.balances.forecastex" in src
    assert "label: 'ForecastEx'" in src
    # The hero/total computation must include forecastex.
    assert re.search(
        r"M\.balances\.kalshi\.balance\s*\+\s*M\.balances\.polymarket\.balance\s*\+\s*_?(?:fc|forecastex)",
        src,
    )


def test_ops_html_balance_panel_lists_forecastex():
    src = _read("arbiter/web/ops.html")
    # Donut/balance legend entry for the third venue.
    assert "ForecastEx" in src


def test_static_dashboard_renders_forecastex_balance():
    src = _read("arbiter_ops_dashboard.html")
    assert "bal-forecastex" in src
    assert "ForecastEx Balance" in src


def test_dashboard_js_includes_forecastex_helpers():
    src = _read("arbiter/web/dashboard.js")
    assert "forecastex" in src.lower()
    assert "FCST" in src  # short code


# ── Templates & deploy config ─────────────────────────────────────────────


def test_env_templates_contain_forecastex_block():
    for path in (".env.template", ".env.production.template"):
        src = _read(path)
        assert "FORECASTEX_ENABLED" in src
        assert "IBKR_GATEWAY_URL" in src
        assert "IBKR_ACCOUNT_ID" in src
        assert "IBKR_PAPER_TRADING" in src


def test_docker_compose_does_not_override_forecastex_envs():
    """The previous fixes for AUTO_EXECUTE/AUTO_PROMOTE proved that listing
    a variable in `environment:` with a default silently overrides whatever
    .env.production sets. The ForecastEx vars must NOT appear in
    `environment:` — they must come through env_file only."""
    src = _read("docker-compose.yml")
    # No FORECASTEX_ENABLED assignment line should remain in compose.
    assert "FORECASTEX_ENABLED:" not in src
    assert "IBKR_GATEWAY_URL:" not in src
    assert "IBKR_ACCOUNT_ID:" not in src


def test_docker_compose_documents_env_override_trap():
    """A comment must remain so a future contributor doesn't reintroduce
    the same precedence bug."""
    src = _read("docker-compose.yml")
    assert "env_file" in src
    # The block we replaced must contain a warning about env override.
    assert "do NOT add" in src or "override" in src.lower()
