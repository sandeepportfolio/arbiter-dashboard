"""Tests for Task 16 — Polymarket US preflight split (5a credentials + 5b live balance).

Invariants verified:
  M3: 5b NEVER runs in CI (no PREFLIGHT_ALLOW_LIVE=1) — checked by test_5b_skipped_without_flag.
"""
from __future__ import annotations

import base64

import pytest
from aioresponses import aioresponses

from arbiter.live.preflight import (
    _check_05a_polymarket_us_credentials,
    _check_05b_polymarket_us_balance,
)

# A valid 32-byte seed encoded as base64 (32 zero bytes = 44 chars).
VALID_SECRET_B64 = base64.b64encode(bytes(32)).decode()  # 44-char base64, 32 zero bytes

# A short secret that decodes to <32 bytes.
SHORT_SECRET_B64 = base64.b64encode(b"tooshort").decode()  # 8 bytes

VALID_KEY_ID = "test-key-id-abc123"


# ─── 5a: credentials check ───────────────────────────────────────────────────


def test_5a_pass_with_valid_creds(monkeypatch):
    """5a passes when POLYMARKET_VARIANT=us, key_id set, secret is valid >=32-byte seed."""
    monkeypatch.setenv("POLYMARKET_VARIANT", "us")
    monkeypatch.setenv("POLYMARKET_US_API_KEY_ID", VALID_KEY_ID)
    monkeypatch.setenv("POLYMARKET_US_API_SECRET", VALID_SECRET_B64)
    item = _check_05a_polymarket_us_credentials()
    assert item.passed is True
    assert item.blocking is True


def test_5a_fail_missing_key_id(monkeypatch):
    """5a fails (blocking) when POLYMARKET_US_API_KEY_ID is unset."""
    monkeypatch.setenv("POLYMARKET_VARIANT", "us")
    monkeypatch.delenv("POLYMARKET_US_API_KEY_ID", raising=False)
    monkeypatch.setenv("POLYMARKET_US_API_SECRET", VALID_SECRET_B64)
    item = _check_05a_polymarket_us_credentials()
    assert item.passed is False
    assert item.blocking is True
    assert "POLYMARKET_US_API_KEY_ID" in item.detail


def test_5a_fail_bad_secret_length(monkeypatch):
    """5a fails (blocking) when secret decodes to <32 bytes."""
    monkeypatch.setenv("POLYMARKET_VARIANT", "us")
    monkeypatch.setenv("POLYMARKET_US_API_KEY_ID", VALID_KEY_ID)
    monkeypatch.setenv("POLYMARKET_US_API_SECRET", SHORT_SECRET_B64)
    item = _check_05a_polymarket_us_credentials()
    assert item.passed is False
    assert item.blocking is True
    assert "32" in item.detail


def test_5a_fail_missing_secret(monkeypatch):
    """5a fails (blocking) when POLYMARKET_US_API_SECRET is unset."""
    monkeypatch.setenv("POLYMARKET_VARIANT", "us")
    monkeypatch.setenv("POLYMARKET_US_API_KEY_ID", VALID_KEY_ID)
    monkeypatch.delenv("POLYMARKET_US_API_SECRET", raising=False)
    item = _check_05a_polymarket_us_credentials()
    assert item.passed is False
    assert item.blocking is True


def test_5a_legacy_variant_delegates(monkeypatch):
    """When POLYMARKET_VARIANT=legacy, 5a delegates to the legacy wallet check."""
    monkeypatch.setenv("POLYMARKET_VARIANT", "legacy")
    # Legacy check requires POLY_PRIVATE_KEY + POLY_FUNDER
    monkeypatch.setenv("POLY_PRIVATE_KEY", "0xdeadbeef")
    monkeypatch.setenv("POLY_FUNDER", "0xfunder")
    item = _check_05a_polymarket_us_credentials()
    assert item.passed is True
    assert "legacy" in item.label.lower()


def test_5a_disabled_variant_not_applicable(monkeypatch):
    """When POLYMARKET_VARIANT=disabled, 5a is not applicable (non-blocking pass)."""
    monkeypatch.setenv("POLYMARKET_VARIANT", "disabled")
    item = _check_05a_polymarket_us_credentials()
    assert item.passed is True
    assert item.blocking is False
    assert "not applicable" in item.detail.lower()


# ─── 5b: live balance check ──────────────────────────────────────────────────


async def test_5b_skipped_without_flag(monkeypatch):
    """M3 invariant: 5b NEVER blocks when PREFLIGHT_ALLOW_LIVE is not set.

    Credentials are set but the flag is absent — must return SKIPPED (passed=True,
    blocking=False), NOT a network call.
    """
    monkeypatch.setenv("POLYMARKET_VARIANT", "us")
    monkeypatch.setenv("POLYMARKET_US_API_KEY_ID", VALID_KEY_ID)
    monkeypatch.setenv("POLYMARKET_US_API_SECRET", VALID_SECRET_B64)
    monkeypatch.delenv("PREFLIGHT_ALLOW_LIVE", raising=False)

    item = await _check_05b_polymarket_us_balance()

    assert item.passed is True, "5b must pass (SKIPPED) without PREFLIGHT_ALLOW_LIVE"
    assert item.blocking is False, "5b must be non-blocking without PREFLIGHT_ALLOW_LIVE"
    assert "SKIPPED" in item.detail or "skip" in item.detail.lower()


async def test_5b_pass_with_sufficient_balance(monkeypatch):
    """5b passes when PREFLIGHT_ALLOW_LIVE=1 and balance response returns $100."""
    monkeypatch.setenv("POLYMARKET_VARIANT", "us")
    monkeypatch.setenv("POLYMARKET_US_API_KEY_ID", VALID_KEY_ID)
    monkeypatch.setenv("POLYMARKET_US_API_SECRET", VALID_SECRET_B64)
    monkeypatch.setenv("PREFLIGHT_ALLOW_LIVE", "1")
    monkeypatch.setenv("POLYMARKET_US_API_URL", "https://api.polymarket.us")

    with aioresponses() as m:
        m.get(
            "https://api.polymarket.us/v1/account/balances",
            payload={"currentBalance": 100.0},
            status=200,
        )
        item = await _check_05b_polymarket_us_balance()

    assert item.passed is True
    assert item.blocking is True
    assert "100.00" in item.detail


async def test_5b_fail_with_low_balance(monkeypatch):
    """5b fails (blocking) when balance is $5 (< $20 minimum)."""
    monkeypatch.setenv("POLYMARKET_VARIANT", "us")
    monkeypatch.setenv("POLYMARKET_US_API_KEY_ID", VALID_KEY_ID)
    monkeypatch.setenv("POLYMARKET_US_API_SECRET", VALID_SECRET_B64)
    monkeypatch.setenv("PREFLIGHT_ALLOW_LIVE", "1")
    monkeypatch.setenv("POLYMARKET_US_API_URL", "https://api.polymarket.us")

    with aioresponses() as m:
        m.get(
            "https://api.polymarket.us/v1/account/balances",
            payload={"currentBalance": 5.0},
            status=200,
        )
        item = await _check_05b_polymarket_us_balance()

    assert item.passed is False
    assert item.blocking is True
    assert "5.00" in item.detail


async def test_disabled_variant_skips_both(monkeypatch):
    """When POLYMARKET_VARIANT=disabled, both 5a and 5b are 'not applicable' (non-blocking)."""
    monkeypatch.setenv("POLYMARKET_VARIANT", "disabled")

    item_a = _check_05a_polymarket_us_credentials()
    item_b = await _check_05b_polymarket_us_balance()

    assert item_a.passed is True
    assert item_a.blocking is False
    assert "not applicable" in item_a.detail.lower()

    assert item_b.passed is True
    assert item_b.blocking is False
    assert "not applicable" in item_b.detail.lower()
