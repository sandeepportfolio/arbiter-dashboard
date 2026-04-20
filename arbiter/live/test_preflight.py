"""Non-live unit tests for the 15 preflight checks.

Each ``_check_*`` function is tested in isolation via monkeypatched env vars
and (for checks that read 04-VALIDATION.md / 04-REVIEW.md) tmp_path-backed
markdown fixtures. Integration tests at the bottom exercise ``run_preflight``
to confirm the orchestrator splices sync + async check results correctly.

Follows root-conftest async dispatch for async cases, sync def for sync.
"""
from __future__ import annotations

import os
import pathlib

import pytest

from arbiter.live import preflight
from arbiter.live.preflight import (
    PreflightReport,
    _check_01_phase4_gate_passed,
    _check_02_phase4_scenarios_observed,
    _check_03_phase4_review,
    _check_04_kalshi_production_creds,
    _check_05_polymarket_funded,
    _check_06_kalshi_funded,
    _check_07_database_url_live,
    _check_08_phase5_max_order_usd,
    _check_09_phase4_polarity,
    _check_10_telegram_configured,
    _check_13_polymarket_migration,
    _check_14_identical_mapping_present,
    _check_15_operator_runbook_ack,
    run_preflight,
)


# ─── Helpers ─────────────────────────────────────────────────────────────────


def _write_validation_md(tmp_path: pathlib.Path, **frontmatter) -> pathlib.Path:
    """Write a minimal 04-VALIDATION.md into tmp_path's mimicked .planning tree."""
    directory = tmp_path / ".planning" / "phases" / "04-sandbox-validation"
    directory.mkdir(parents=True, exist_ok=True)
    lines = ["---"]
    for k, v in frontmatter.items():
        lines.append(f"{k}: {v}")
    lines.append("---")
    lines.append("")
    lines.append("# Phase 4 validation")
    path = directory / "04-VALIDATION.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def _chdir(tmp_path: pathlib.Path, monkeypatch):
    monkeypatch.chdir(tmp_path)


# ─── Check 1: phase4_gate ────────────────────────────────────────────────────


def test_check_01_gate_pass_when_status_pass(tmp_path, monkeypatch):
    _write_validation_md(tmp_path, phase_gate_status="PASS")
    _chdir(tmp_path, monkeypatch)
    item = _check_01_phase4_gate_passed()
    assert item.passed is True
    assert item.blocking is True


def test_check_01_gate_fail_when_status_pending(tmp_path, monkeypatch):
    _write_validation_md(tmp_path, phase_gate_status="PENDING")
    _chdir(tmp_path, monkeypatch)
    item = _check_01_phase4_gate_passed()
    assert item.passed is False
    assert item.blocking is True


def test_check_01_gate_fail_when_file_missing(tmp_path, monkeypatch):
    _chdir(tmp_path, monkeypatch)
    item = _check_01_phase4_gate_passed()
    assert item.passed is False
    assert "missing" in item.detail.lower() or "unreadable" in item.detail.lower()


# ─── Check 2: phase4_scenarios ───────────────────────────────────────────────


def test_check_02_scenarios_pass_when_9_observed_0_missing(tmp_path, monkeypatch):
    _write_validation_md(
        tmp_path, total_scenarios_observed=9, scenarios_missing=0,
    )
    _chdir(tmp_path, monkeypatch)
    item = _check_02_phase4_scenarios_observed()
    assert item.passed is True


def test_check_02_scenarios_fail_when_missing(tmp_path, monkeypatch):
    _write_validation_md(
        tmp_path, total_scenarios_observed=8, scenarios_missing=1,
    )
    _chdir(tmp_path, monkeypatch)
    item = _check_02_phase4_scenarios_observed()
    assert item.passed is False


# ─── Check 3: phase4_review ──────────────────────────────────────────────────


def test_check_03_review_pass_when_file_absent(tmp_path, monkeypatch):
    _chdir(tmp_path, monkeypatch)
    item = _check_03_phase4_review()
    assert item.passed is True  # Treated as no items (manual attest)


def test_check_03_review_fail_when_open_blocking(tmp_path, monkeypatch):
    directory = tmp_path / ".planning" / "phases" / "04-sandbox-validation"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "04-REVIEW.md").write_text(
        "# Review\n\nstatus: blocking\n", encoding="utf-8"
    )
    _chdir(tmp_path, monkeypatch)
    item = _check_03_phase4_review()
    assert item.passed is False


# ─── Check 4: kalshi creds ───────────────────────────────────────────────────


def test_check_04_pass_with_valid_prod_creds(tmp_path, monkeypatch):
    key_path = tmp_path / "keys" / "kalshi_private.pem"
    key_path.parent.mkdir(parents=True, exist_ok=True)
    key_path.write_text("dummy-rsa-key", encoding="utf-8")
    monkeypatch.setenv("KALSHI_API_KEY_ID", "abc-123")
    monkeypatch.setenv("KALSHI_PRIVATE_KEY_PATH", str(key_path))
    item = _check_04_kalshi_production_creds()
    assert item.passed is True


def test_check_04_fail_when_key_path_is_demo(tmp_path, monkeypatch):
    key_path = tmp_path / "keys" / "kalshi_demo_private.pem"
    key_path.parent.mkdir(parents=True, exist_ok=True)
    key_path.write_text("dummy", encoding="utf-8")
    monkeypatch.setenv("KALSHI_API_KEY_ID", "abc-123")
    monkeypatch.setenv("KALSHI_PRIVATE_KEY_PATH", str(key_path))
    item = _check_04_kalshi_production_creds()
    assert item.passed is False
    assert "demo" in item.detail


def test_check_04_fail_when_key_path_missing(monkeypatch):
    monkeypatch.setenv("KALSHI_API_KEY_ID", "abc-123")
    monkeypatch.setenv("KALSHI_PRIVATE_KEY_PATH", "/tmp/does-not-exist.pem")
    item = _check_04_kalshi_production_creds()
    assert item.passed is False


# ─── Check 5: polymarket funded ──────────────────────────────────────────────


def test_check_05_pass_with_wallet_creds(monkeypatch):
    monkeypatch.setenv("POLY_PRIVATE_KEY", "0xdeadbeef")
    monkeypatch.setenv("POLY_FUNDER", "0xfunder")
    item = _check_05_polymarket_funded()
    assert item.passed is True


def test_check_05_fail_when_pk_missing(monkeypatch):
    monkeypatch.delenv("POLY_PRIVATE_KEY", raising=False)
    monkeypatch.setenv("POLY_FUNDER", "0xfunder")
    item = _check_05_polymarket_funded()
    assert item.passed is False


# ─── Check 6: kalshi funded (proxy) ──────────────────────────────────────────


def test_check_06_pass_when_api_key_set(monkeypatch):
    monkeypatch.setenv("KALSHI_API_KEY_ID", "abc-123")
    item = _check_06_kalshi_funded()
    assert item.passed is True


def test_check_06_fail_when_api_key_missing(monkeypatch):
    monkeypatch.delenv("KALSHI_API_KEY_ID", raising=False)
    item = _check_06_kalshi_funded()
    assert item.passed is False


# ─── Check 7: database_url ───────────────────────────────────────────────────


def test_check_07_pass_on_arbiter_live(monkeypatch):
    monkeypatch.setenv(
        "DATABASE_URL", "postgresql://u:p@localhost:5432/arbiter_live"
    )
    item = _check_07_database_url_live()
    assert item.passed is True


def test_check_07_fail_on_arbiter_sandbox(monkeypatch):
    monkeypatch.setenv(
        "DATABASE_URL", "postgresql://u:p@localhost:5432/arbiter_sandbox"
    )
    item = _check_07_database_url_live()
    assert item.passed is False


def test_check_07_fail_when_unset(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    item = _check_07_database_url_live()
    assert item.passed is False


# ─── Check 8: PHASE5_MAX_ORDER_USD ──────────────────────────────────────────


def test_check_08_pass_when_set_to_10(monkeypatch):
    monkeypatch.setenv("PHASE5_MAX_ORDER_USD", "10")
    item = _check_08_phase5_max_order_usd()
    assert item.passed is True


def test_check_08_fail_when_unset(monkeypatch):
    monkeypatch.delenv("PHASE5_MAX_ORDER_USD", raising=False)
    item = _check_08_phase5_max_order_usd()
    assert item.passed is False


def test_check_08_fail_when_above_10(monkeypatch):
    monkeypatch.setenv("PHASE5_MAX_ORDER_USD", "25")
    item = _check_08_phase5_max_order_usd()
    assert item.passed is False


def test_check_08_fail_when_unparseable(monkeypatch):
    monkeypatch.setenv("PHASE5_MAX_ORDER_USD", "banana")
    item = _check_08_phase5_max_order_usd()
    assert item.passed is False


# ─── Check 9: phase4 polarity (W-2) ──────────────────────────────────────────


def test_check_09_both_unset_is_pass_not_blocking(monkeypatch):
    monkeypatch.delenv("PHASE4_MAX_ORDER_USD", raising=False)
    monkeypatch.delenv("PHASE5_MAX_ORDER_USD", raising=False)
    item = _check_09_phase4_polarity()
    assert item.passed is True
    assert item.blocking is False


def test_check_09_phase5_set_phase4_unset_is_pass(monkeypatch):
    monkeypatch.delenv("PHASE4_MAX_ORDER_USD", raising=False)
    monkeypatch.setenv("PHASE5_MAX_ORDER_USD", "10")
    item = _check_09_phase4_polarity()
    assert item.passed is True
    assert item.blocking is False


def test_check_09_phase4_ge_phase5_is_pass(monkeypatch):
    monkeypatch.setenv("PHASE4_MAX_ORDER_USD", "20")
    monkeypatch.setenv("PHASE5_MAX_ORDER_USD", "10")
    item = _check_09_phase4_polarity()
    assert item.passed is True
    assert item.blocking is False


def test_check_09_phase4_lt_phase5_is_fail_and_blocking(monkeypatch):
    """The unsafe inversion — PHASE4's tighter cap would reject below PHASE5 belt."""
    monkeypatch.setenv("PHASE4_MAX_ORDER_USD", "5")
    monkeypatch.setenv("PHASE5_MAX_ORDER_USD", "10")
    item = _check_09_phase4_polarity()
    assert item.passed is False
    assert item.blocking is True
    assert "INVERSION" in item.detail.upper()


# ─── Check 10: telegram ──────────────────────────────────────────────────────


def test_check_10_pass_when_both_set(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "bot-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "chat-id")
    item = _check_10_telegram_configured()
    assert item.passed is True


def test_check_10_fail_when_token_missing(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "chat-id")
    item = _check_10_telegram_configured()
    assert item.passed is False


# ─── Check 13: polymarket migration ──────────────────────────────────────────


def test_check_13_fail_without_ack(monkeypatch):
    monkeypatch.delenv("POLYMARKET_MIGRATION_ACK", raising=False)
    item = _check_13_polymarket_migration()
    # py_clob_client is installed in this env; without ack the check fails.
    assert item.passed is False


def test_check_13_pass_with_ack(monkeypatch):
    monkeypatch.setenv("POLYMARKET_MIGRATION_ACK", "ACKNOWLEDGED")
    item = _check_13_polymarket_migration()
    assert item.passed is True


# ─── Check 14: identical mapping ─────────────────────────────────────────────


def test_check_14_fail_when_no_identical_mapping(monkeypatch):
    """Patch iter_confirmed_market_mappings to return zero identical mappings."""
    import types
    from arbiter.config import settings

    def _empty_iter(*a, **kw):
        return iter([])

    monkeypatch.setattr(settings, "iter_confirmed_market_mappings", _empty_iter)
    # Also re-import inside the preflight module if it binds at import time.
    # (Our check imports lazily inside the function, so monkeypatching the
    # module-level symbol is sufficient.)
    item = _check_14_identical_mapping_present()
    assert item.passed is False


def test_check_14_pass_when_at_least_one_identical(monkeypatch):
    from arbiter.config import settings
    from types import SimpleNamespace

    def _iter(*a, **kw):
        yield "CAN-X", SimpleNamespace(resolution_match_status="identical")

    monkeypatch.setattr(settings, "iter_confirmed_market_mappings", _iter)
    item = _check_14_identical_mapping_present()
    assert item.passed is True


# ─── Check 15: operator runbook ack ──────────────────────────────────────────


def test_check_15_fail_when_unset(monkeypatch):
    monkeypatch.delenv("OPERATOR_RUNBOOK_ACK", raising=False)
    item = _check_15_operator_runbook_ack()
    assert item.passed is False


def test_check_15_pass_when_acknowledged(monkeypatch):
    monkeypatch.setenv("OPERATOR_RUNBOOK_ACK", "ACKNOWLEDGED")
    item = _check_15_operator_runbook_ack()
    assert item.passed is True


def test_check_15_fail_when_wrong_value(monkeypatch):
    monkeypatch.setenv("OPERATOR_RUNBOOK_ACK", "whatever")
    item = _check_15_operator_runbook_ack()
    assert item.passed is False


# ─── Integration: run_preflight ──────────────────────────────────────────────


async def test_run_preflight_with_clean_env_still_returns_15_items(monkeypatch, tmp_path):
    """Orchestrator returns PreflightReport with 15 items even when env is empty."""
    # Chdir to a temp dir so 04-VALIDATION.md is absent (forces check 1 + 2 fail).
    monkeypatch.chdir(tmp_path)
    # Clear env so dashboard checks fall to unreachable branch quickly.
    for var in (
        "DATABASE_URL", "PHASE5_MAX_ORDER_USD", "PHASE4_MAX_ORDER_USD",
        "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "OPERATOR_RUNBOOK_ACK",
        "POLYMARKET_MIGRATION_ACK", "KALSHI_API_KEY_ID",
    ):
        monkeypatch.delenv(var, raising=False)
    report = await run_preflight(dashboard_url="http://127.0.0.1:1")
    assert isinstance(report, PreflightReport)
    assert len(report.items) == 15, (
        f"expected 15 check rows, got {len(report.items)}"
    )
    # With nothing configured, overall must NOT pass.
    assert report.passed is False


async def test_run_preflight_table_renders_without_error(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    report = await run_preflight(dashboard_url="http://127.0.0.1:1")
    table = report.to_table()
    assert "Check" in table
    assert "Status" in table
    assert "OVERALL" in table


async def test_run_preflight_blocking_failure_on_sandbox_db(monkeypatch, tmp_path):
    """Setting DATABASE_URL to sandbox must produce a blocking failure in check 7."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(
        "DATABASE_URL", "postgresql://u:p@localhost/arbiter_sandbox",
    )
    report = await run_preflight(dashboard_url="http://127.0.0.1:1")
    # Find check 7 in the report.
    check7 = next((i for i in report.items if i.key == "database_url"), None)
    assert check7 is not None
    assert check7.passed is False
    assert check7.blocking is True
    assert check7 in report.blocking_failures
