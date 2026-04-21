from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

_REPO_ROOT = str(Path(__file__).resolve().parent.parent.parent)
sys.path.insert(0, _REPO_ROOT)

from scripts.setup.validate_env import main  # noqa: E402


@pytest.fixture()
def base_env(tmp_path, monkeypatch):
    pem = tmp_path / "kalshi.pem"
    pem.write_text("-----BEGIN PRIVATE KEY-----\nTEST\n-----END PRIVATE KEY-----\n", encoding="utf-8")

    values = {
        "DRY_RUN": "false",
        "DATABASE_URL": "postgresql://arbiter:supersecretpass@localhost:5432/arbiter_live",
        "PG_PASSWORD": "supersecretpass",
        "KALSHI_BASE_URL": "https://api.elections.kalshi.com/trade-api/v2",
        "KALSHI_API_KEY_ID": "kalshi-prod-key-1234567890",
        "KALSHI_PRIVATE_KEY_PATH": str(pem),
        "PHASE5_MAX_ORDER_USD": "10",
        "MAX_POSITION_USD": "10",
        "AUTO_EXECUTE_ENABLED": "false",
        "TELEGRAM_BOT_TOKEN": "1234567890:" + "a" * 40,
        "TELEGRAM_CHAT_ID": "7059619695",
        "POLYMARKET_MIGRATION_ACK": "ACKNOWLEDGED",
        "OPERATOR_RUNBOOK_ACK": "ACKNOWLEDGED",
        "OPS_EMAIL": "sparx.sandeep@gmail.com",
        "OPS_PASSWORD": "saibaba123",
        "UI_SESSION_SECRET": "a" * 64,
    }

    for key in list(os.environ):
        if key.startswith("POLY") or key in values or key in {
            "OPS_EMAIL",
            "OPS_PASSWORD",
            "PHASE4_MAX_ORDER_USD",
            "AUTO_EXECUTE_ENABLED",
        }:
            monkeypatch.delenv(key, raising=False)

    for key, value in values.items():
        monkeypatch.setenv(key, value)

    return monkeypatch


def test_us_variant_passes_without_legacy_vars(base_env, capsys):
    base_env.setenv("POLYMARKET_VARIANT", "us")
    base_env.setenv("POLYMARKET_US_API_URL", "https://api.polymarket.us/v1")
    base_env.setenv("POLYMARKET_US_API_KEY_ID", "pm-us-key-12345678")
    base_env.setenv("POLYMARKET_US_API_SECRET", "a" * 44)

    result = main()

    assert result == 0
    output = capsys.readouterr().out
    assert "POLYMARKET_US_API_KEY_ID" in output
    assert "PHASE4_MAX_ORDER_USD" in output
    assert "FAILED" not in output


def test_legacy_variant_passes_without_us_vars(base_env, capsys):
    base_env.setenv("POLYMARKET_VARIANT", "legacy")
    base_env.setenv("POLYMARKET_CLOB_URL", "https://clob.polymarket.com")
    base_env.setenv("POLY_PRIVATE_KEY", "a" * 64)
    base_env.setenv("POLY_FUNDER", "0x" + "b" * 40)
    base_env.setenv("POLY_SIGNATURE_TYPE", "2")

    result = main()

    assert result == 0
    output = capsys.readouterr().out
    assert "POLY_PRIVATE_KEY" in output
    assert "FAILED" not in output


def test_us_variant_requires_us_creds(base_env):
    base_env.setenv("POLYMARKET_VARIANT", "us")
    base_env.delenv("POLYMARKET_US_API_KEY_ID", raising=False)
    base_env.delenv("POLYMARKET_US_API_SECRET", raising=False)
    base_env.setenv("POLYMARKET_US_API_URL", "https://api.polymarket.us/v1")

    assert main() == 1


def test_max_position_cannot_exceed_phase5(base_env):
    base_env.setenv("POLYMARKET_VARIANT", "us")
    base_env.setenv("POLYMARKET_US_API_URL", "https://api.polymarket.us/v1")
    base_env.setenv("POLYMARKET_US_API_KEY_ID", "pm-us-key-12345678")
    base_env.setenv("POLYMARKET_US_API_SECRET", "a" * 44)
    base_env.setenv("PHASE5_MAX_ORDER_USD", "10")
    base_env.setenv("MAX_POSITION_USD", "11")

    assert main() == 1
