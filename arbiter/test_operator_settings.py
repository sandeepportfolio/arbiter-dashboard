"""Operator-settings regressions for continuous market discovery."""
from __future__ import annotations

from arbiter.operator_settings import default_market_discovery_settings, load_market_discovery_settings


class _Store:
    def __init__(self, payload):
        self.payload = payload

    def load(self):
        return self.payload


def test_auto_discovery_env_default_is_enabled(monkeypatch):
    monkeypatch.delenv("AUTO_DISCOVERY_ENABLED", raising=False)

    settings = default_market_discovery_settings()

    assert settings["auto_discovery_enabled"] is True


def test_auto_discovery_env_parses_false(monkeypatch):
    monkeypatch.setenv("AUTO_DISCOVERY_ENABLED", "false")

    settings = default_market_discovery_settings()

    assert settings["auto_discovery_enabled"] is False


def test_operator_settings_file_overrides_auto_discovery_env(monkeypatch):
    monkeypatch.setenv("AUTO_DISCOVERY_ENABLED", "true")
    store = _Store({
        "settings": {
            "mapping": {
                "auto_discovery_enabled": False,
                "auto_discovery_interval_seconds": 120,
                "auto_discovery_budget_rps": 4.5,
                "auto_discovery_min_score": 0.18,
                "auto_discovery_max_candidates": 900,
            },
        },
    })

    settings = load_market_discovery_settings(store)

    assert settings["auto_discovery_enabled"] is False
    assert settings["auto_discovery_interval_seconds"] == 120.0
    assert settings["auto_discovery_budget_rps"] == 4.5
    assert settings["auto_discovery_min_score"] == 0.18
    assert settings["auto_discovery_max_candidates"] == 900
