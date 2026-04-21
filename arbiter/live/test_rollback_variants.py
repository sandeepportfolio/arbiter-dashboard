"""Rollback smoke tests — Task 19.5.

Tests that ``POLYMARKET_VARIANT`` correctly gates which adapter (or None) is
wired up, and that no global state leaks between config reloads.

Tests:
    test_variant_disabled_skips_polymarket_wiring
    test_variant_legacy_uses_legacy_adapter
    test_variant_us_uses_us_adapter
    test_switch_variant_no_orphan_state
"""
from __future__ import annotations

import importlib
import sys
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _clear_settings_cache() -> None:
    """Force settings.py to re-evaluate env vars on next import.

    ``load_config()`` reads env vars at call time (not module import time),
    so we only need to invalidate cached dataclass defaults that are
    computed via ``field(default_factory=…)``.  Since every factory calls
    ``os.getenv`` directly, simply calling ``load_config()`` after patching
    env vars is sufficient — no module reload needed.
    """
    pass  # no-op: all factories are lazy


# ---------------------------------------------------------------------------
# Test 1 — POLYMARKET_VARIANT=disabled
# ---------------------------------------------------------------------------


def test_variant_disabled_skips_polymarket_wiring(monkeypatch):
    """With POLYMARKET_VARIANT=disabled:
      - load_config() returns polymarket=None
      - build_polymarket_component() returns None
      - No HTTP client for any polymarket host is created

    The check is structural (no HTTP calls issued) — we verify that
    build_polymarket_component returns None and that calling it does not
    import or instantiate any aiohttp sessions pointing to polymarket hosts.
    """
    monkeypatch.setenv("POLYMARKET_VARIANT", "disabled")
    # Remove any lingering Polymarket creds so the legacy path also gets None
    monkeypatch.delenv("POLY_PRIVATE_KEY", raising=False)
    monkeypatch.delenv("POLY_FUNDER", raising=False)
    monkeypatch.delenv("POLYMARKET_US_API_KEY_ID", raising=False)
    monkeypatch.delenv("POLYMARKET_US_API_SECRET", raising=False)

    from arbiter.config.settings import load_config
    from arbiter.main import build_polymarket_component

    cfg = load_config()
    assert cfg.polymarket is None, (
        "load_config() must return polymarket=None for POLYMARKET_VARIANT=disabled"
    )

    component = build_polymarket_component(cfg)
    assert component is None, (
        "build_polymarket_component() must return None for disabled variant"
    )

    # Assert no aiohttp.ClientSession was created pointing to polymarket hosts.
    # We do this by patching ClientSession and asserting it was never called.
    created_sessions: list = []

    class _TrackingSession:
        def __init__(self, *args, **kwargs):
            created_sessions.append((args, kwargs))

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

    with patch("aiohttp.ClientSession", _TrackingSession):
        component2 = build_polymarket_component(cfg)

    assert component2 is None
    # No HTTP sessions should have been opened for a disabled variant
    assert len(created_sessions) == 0, (
        "No aiohttp sessions must be created for POLYMARKET_VARIANT=disabled"
    )


# ---------------------------------------------------------------------------
# Test 2 — POLYMARKET_VARIANT=legacy
# ---------------------------------------------------------------------------


def test_variant_legacy_uses_legacy_adapter(monkeypatch):
    """With POLYMARKET_VARIANT=legacy, build_polymarket_component() returns a
    PolymarketAdapter, NOT a PolymarketUSAdapter.
    """
    monkeypatch.setenv("POLYMARKET_VARIANT", "legacy")
    monkeypatch.setenv("POLY_PRIVATE_KEY", "0x" + "a" * 64)
    monkeypatch.setenv("POLY_FUNDER", "0x" + "b" * 40)
    monkeypatch.delenv("POLYMARKET_US_API_KEY_ID", raising=False)
    monkeypatch.delenv("POLYMARKET_US_API_SECRET", raising=False)

    from arbiter.config.settings import load_config, PolymarketConfig
    from arbiter.main import build_polymarket_component
    from arbiter.execution.adapters.polymarket import PolymarketAdapter
    from arbiter.execution.adapters.polymarket_us import PolymarketUSAdapter

    cfg = load_config()
    assert isinstance(cfg.polymarket, PolymarketConfig), (
        "load_config() must return PolymarketConfig for POLYMARKET_VARIANT=legacy"
    )

    adapter = build_polymarket_component(cfg)
    assert adapter is not None, "build_polymarket_component() must not return None for legacy"
    assert isinstance(adapter, PolymarketAdapter), (
        f"Expected PolymarketAdapter, got {type(adapter).__name__}"
    )
    assert not isinstance(adapter, PolymarketUSAdapter), (
        "Must NOT be PolymarketUSAdapter for legacy variant"
    )


# ---------------------------------------------------------------------------
# Test 3 — POLYMARKET_VARIANT=us
# ---------------------------------------------------------------------------


def test_variant_us_uses_us_adapter(monkeypatch):
    """With POLYMARKET_VARIANT=us, build_polymarket_component() returns a
    PolymarketUSAdapter, NOT the legacy PolymarketAdapter.
    """
    monkeypatch.setenv("POLYMARKET_VARIANT", "us")
    # Valid 32-byte (44-char base64) secret
    import base64
    valid_secret = base64.b64encode(bytes(32)).decode()
    monkeypatch.setenv("POLYMARKET_US_API_KEY_ID", "test-key-id")
    monkeypatch.setenv("POLYMARKET_US_API_SECRET", valid_secret)
    monkeypatch.delenv("POLY_PRIVATE_KEY", raising=False)
    monkeypatch.delenv("POLY_FUNDER", raising=False)

    from arbiter.config.settings import load_config, PolymarketUSConfig
    from arbiter.main import build_polymarket_component
    from arbiter.execution.adapters.polymarket import PolymarketAdapter
    from arbiter.execution.adapters.polymarket_us import PolymarketUSAdapter

    cfg = load_config()
    assert isinstance(cfg.polymarket, PolymarketUSConfig), (
        "load_config() must return PolymarketUSConfig for POLYMARKET_VARIANT=us"
    )

    adapter = build_polymarket_component(cfg)
    assert adapter is not None, "build_polymarket_component() must not return None for us variant"
    assert isinstance(adapter, PolymarketUSAdapter), (
        f"Expected PolymarketUSAdapter, got {type(adapter).__name__}"
    )
    assert not isinstance(adapter, PolymarketAdapter), (
        "Must NOT be legacy PolymarketAdapter for us variant"
    )


# ---------------------------------------------------------------------------
# Test 4 — No orphan state across config switches
# ---------------------------------------------------------------------------


def test_switch_variant_no_orphan_state(monkeypatch):
    """Loading config with 'us', then 'disabled', then 'us' again produces no
    global state leakage.

    This verifies that:
    - Each call to load_config() + build_polymarket_component() is independent.
    - The second 'us' call produces a fresh PolymarketUSAdapter (not a stale one).
    - The 'disabled' pass truly returns None with no side effects.
    """
    import base64
    from arbiter.config.settings import load_config, PolymarketUSConfig
    from arbiter.main import build_polymarket_component
    from arbiter.execution.adapters.polymarket_us import PolymarketUSAdapter

    valid_secret = base64.b64encode(bytes(32)).decode()

    # ── Round 1: us ─────────────────────────────────────────────────────────
    monkeypatch.setenv("POLYMARKET_VARIANT", "us")
    monkeypatch.setenv("POLYMARKET_US_API_KEY_ID", "round1-key-id")
    monkeypatch.setenv("POLYMARKET_US_API_SECRET", valid_secret)

    cfg1 = load_config()
    adapter1 = build_polymarket_component(cfg1)
    assert isinstance(adapter1, PolymarketUSAdapter)

    # ── Round 2: disabled ────────────────────────────────────────────────────
    monkeypatch.setenv("POLYMARKET_VARIANT", "disabled")
    monkeypatch.delenv("POLYMARKET_US_API_KEY_ID", raising=False)
    monkeypatch.delenv("POLYMARKET_US_API_SECRET", raising=False)

    cfg2 = load_config()
    assert cfg2.polymarket is None
    adapter2 = build_polymarket_component(cfg2)
    assert adapter2 is None

    # ── Round 3: us again ────────────────────────────────────────────────────
    monkeypatch.setenv("POLYMARKET_VARIANT", "us")
    monkeypatch.setenv("POLYMARKET_US_API_KEY_ID", "round3-key-id")
    monkeypatch.setenv("POLYMARKET_US_API_SECRET", valid_secret)

    cfg3 = load_config()
    adapter3 = build_polymarket_component(cfg3)
    assert isinstance(adapter3, PolymarketUSAdapter)

    # All three adapters are independent objects (no global state sharing)
    assert adapter1 is not adapter3, (
        "Each build_polymarket_component() call must return a fresh object"
    )

    # Disabled round left no orphan adapter in the module namespace
    # (we just verify cfg2.polymarket is still None after round 3 env changes —
    # this confirms the config objects are independent)
    assert cfg2.polymarket is None
