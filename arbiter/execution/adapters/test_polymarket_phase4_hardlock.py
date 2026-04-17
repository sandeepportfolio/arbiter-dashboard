"""Tests for PHASE4_MAX_ORDER_USD hard-lock in PolymarketAdapter.place_fok (Plan 04-02 Task 1).

The hard-lock is an adapter-layer belt (D-02) above RiskManager + the $10 test-wallet
hardware cap. Tests cover five cases:

1. Unset env -> no-op (production behavior unchanged)
2. Notional over cap -> _failed_order returned, no HTTP call made
3. Notional under cap -> proceeds to _place_fok_reconciling
4. Notional exactly equals cap -> ALLOWED (strict > comparison per Pitfall 8 note)
5. Unparseable env -> maximally restrictive (0.0 cap) -> any positive notional rejected

Tests follow the root-conftest async dispatch style (no @pytest.mark.asyncio);
`arbiter/safety/test_supervisor.py` uses the same pattern.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from arbiter.execution.adapters.polymarket import PolymarketAdapter
from arbiter.execution.engine import OrderStatus


def _config(private_key: str = "0xdeadbeef"):
    cfg = SimpleNamespace()
    cfg.polymarket = SimpleNamespace(
        private_key=private_key,
        clob_url="https://clob.test",
        chain_id=137,
        signature_type=1,
        funder="0xfunder",
    )
    return cfg


def _circuit(can_execute: bool = True):
    c = MagicMock()
    c.can_execute = MagicMock(return_value=can_execute)
    c.record_success = MagicMock()
    c.record_failure = MagicMock()
    return c


def _rate_limiter():
    rl = MagicMock()
    rl.acquire = AsyncMock(return_value=None)
    return rl


def _make_adapter():
    """Build PolymarketAdapter with mocked config + circuit + client; stub _place_fok_reconciling
    so tests can assert the hard-lock runs BEFORE reconciler (assert_not_awaited vs awaited_once).
    """
    cfg = _config()
    client = MagicMock()
    adapter = PolymarketAdapter(
        config=cfg,
        clob_client_factory=lambda: client,
        rate_limiter=_rate_limiter(),
        circuit=_circuit(can_execute=True),
    )
    # Stub the downstream reconciler so we only exercise the hard-lock path.
    adapter._place_fok_reconciling = AsyncMock(
        return_value=MagicMock(status=OrderStatus.FILLED)
    )
    return adapter


async def test_hardlock_noop_when_env_unset(monkeypatch):
    """Unset env -> place_fok proceeds to _place_fok_reconciling unchanged."""
    monkeypatch.delenv("PHASE4_MAX_ORDER_USD", raising=False)
    adapter = _make_adapter()
    await adapter.place_fok("ARB-1", "mkt", "CAN", "yes", 0.5, 100)
    adapter._place_fok_reconciling.assert_awaited_once()


async def test_hardlock_rejects_when_notional_exceeds_cap(monkeypatch):
    """PHASE4_MAX_ORDER_USD=5, qty=10, price=0.60 -> notional=6.00 > 5 -> rejected."""
    monkeypatch.setenv("PHASE4_MAX_ORDER_USD", "5")
    adapter = _make_adapter()
    result = await adapter.place_fok("ARB-2", "mkt", "CAN", "yes", 0.60, 10)
    assert result.status == OrderStatus.FAILED
    assert "PHASE4_MAX_ORDER_USD hard-lock" in result.error
    assert "$6.00" in result.error
    assert "$5.00" in result.error
    adapter._place_fok_reconciling.assert_not_awaited()


async def test_hardlock_allows_when_notional_under_cap(monkeypatch):
    """PHASE4_MAX_ORDER_USD=5, qty=10, price=0.40 -> notional=4.00 < 5 -> allowed."""
    monkeypatch.setenv("PHASE4_MAX_ORDER_USD", "5")
    adapter = _make_adapter()
    await adapter.place_fok("ARB-3", "mkt", "CAN", "yes", 0.40, 10)
    adapter._place_fok_reconciling.assert_awaited_once()


async def test_hardlock_boundary_exact_equal_allowed(monkeypatch):
    """Strict > comparison: notional == cap is ALLOWED (operator intent).

    PHASE4_MAX_ORDER_USD=5, qty=5, price=1.00 -> notional=5.00 == 5 -> allowed.
    """
    monkeypatch.setenv("PHASE4_MAX_ORDER_USD", "5")
    adapter = _make_adapter()
    await adapter.place_fok("ARB-4", "mkt", "CAN", "yes", 1.0, 5)
    adapter._place_fok_reconciling.assert_awaited_once()


async def test_hardlock_unparseable_env_is_maximally_restrictive(monkeypatch):
    """Garbage env value -> float parse fails -> falls back to 0.0 cap ->
    any positive notional is rejected (safe failure mode; Pitfall 8 / T-04-02-08).
    """
    monkeypatch.setenv("PHASE4_MAX_ORDER_USD", "not-a-number")
    adapter = _make_adapter()
    result = await adapter.place_fok("ARB-5", "mkt", "CAN", "yes", 0.01, 1)
    assert result.status == OrderStatus.FAILED
    assert "hard-lock" in result.error
    adapter._place_fok_reconciling.assert_not_awaited()
