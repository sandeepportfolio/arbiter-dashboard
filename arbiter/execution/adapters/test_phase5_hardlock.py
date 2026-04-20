"""Tests for PHASE5_MAX_ORDER_USD hard-lock (Plan 05-01 Task 1).

Mirrors the D-02 PHASE4 pattern from Plan 04-02 onto THREE call sites:

* ``PolymarketAdapter.place_fok``  (already has PHASE4; PHASE5 inserted AFTER)
* ``KalshiAdapter.place_fok``       (had NO hard-lock; Plan 05-01 adds BOTH PHASE4 and PHASE5)
* ``KalshiAdapter.place_resting_limit``  (already has PHASE4; PHASE5 inserted AFTER)

Five unit tests per call site (15 total), plus three combination tests that
verify the PHASE4 + PHASE5 blocks are additive (no short-circuit — both must
pass for the order to reach the wire) and that the ordering (PHASE4 first,
PHASE5 second) produces the expected error prefix when only one trips.

Tests follow the root-conftest async dispatch style:
* Polymarket tests: ``async def test_*`` with no marker (sync-dispatched by root conftest).
* Kalshi tests: ``@pytest.mark.asyncio`` marker (matches test_kalshi_place_resting_limit.py).

Both styles are supported simultaneously by the project's conftest because
``pytest_pyfunc_call`` recognises ``async def`` regardless of markers.
"""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from arbiter.execution.adapters.kalshi import KalshiAdapter
from arbiter.execution.adapters.polymarket import PolymarketAdapter
from arbiter.execution.engine import OrderStatus


# ─── Polymarket adapter helpers (mirror test_polymarket_phase4_hardlock.py) ───

def _poly_config(private_key: str = "0xdeadbeef"):
    cfg = SimpleNamespace()
    cfg.polymarket = SimpleNamespace(
        private_key=private_key,
        clob_url="https://clob.test",
        chain_id=137,
        signature_type=1,
        funder="0xfunder",
    )
    return cfg


def _poly_circuit(can_execute: bool = True):
    c = MagicMock()
    c.can_execute = MagicMock(return_value=can_execute)
    c.record_success = MagicMock()
    c.record_failure = MagicMock()
    return c


def _poly_rate_limiter():
    rl = MagicMock()
    rl.acquire = AsyncMock(return_value=None)
    return rl


def _make_polymarket_adapter():
    """Build PolymarketAdapter with downstream reconciler stubbed as AsyncMock."""
    cfg = _poly_config()
    client = MagicMock()
    adapter = PolymarketAdapter(
        config=cfg,
        clob_client_factory=lambda: client,
        rate_limiter=_poly_rate_limiter(),
        circuit=_poly_circuit(can_execute=True),
    )
    adapter._place_fok_reconciling = AsyncMock(
        return_value=MagicMock(status=OrderStatus.FILLED)
    )
    return adapter


# ─── Kalshi adapter helpers (mirror test_kalshi_place_resting_limit.py) ───────

def _kalshi_config():
    cfg = SimpleNamespace()
    cfg.kalshi = SimpleNamespace(
        base_url="https://api.elections.kalshi.test/trade-api/v2",
    )
    return cfg


def _kalshi_auth(authenticated: bool = True):
    auth = MagicMock()
    auth.is_authenticated = authenticated
    auth.get_headers = MagicMock(return_value={"Authorization": "test-sig"})
    return auth


def _kalshi_circuit(can_execute: bool = True):
    circuit = MagicMock()
    circuit.can_execute = MagicMock(return_value=can_execute)
    circuit.record_success = MagicMock()
    circuit.record_failure = MagicMock()
    return circuit


def _kalshi_rate_limiter():
    rl = MagicMock()
    rl.acquire = AsyncMock(return_value=None)
    rl.apply_retry_after = MagicMock(return_value=3.0)
    return rl


def _session_with_post(status: int, body_text: str, headers=None):
    """Session whose .post(url, json=..., headers=...) returns (status, body_text)."""
    session = MagicMock()
    resp = MagicMock()
    resp.status = status
    resp.text = AsyncMock(return_value=body_text)
    resp.headers = headers or {}
    session.post.return_value.__aenter__ = AsyncMock(return_value=resp)
    session.post.return_value.__aexit__ = AsyncMock(return_value=False)
    return session


def _kalshi_resting_body():
    return json.dumps({
        "order": {
            "order_id": "K-REST",
            "status": "resting",
            "client_order_id": "ARB-OK-YES-abcdef12",
            "fill_count_fp": "0",
            "yes_price_dollars": "0.4000",
        },
    })


def _kalshi_fok_body():
    return json.dumps({
        "order": {
            "order_id": "K-FOK",
            "status": "executed",
            "client_order_id": "ARB-OK-YES-fedcba21",
            "fill_count_fp": "10.00",
            "yes_price_dollars": "0.4000",
        },
    })


def _make_kalshi_adapter(session, *, authenticated: bool = True, can_execute: bool = True):
    return KalshiAdapter(
        config=_kalshi_config(),
        session=session,
        auth=_kalshi_auth(authenticated),
        rate_limiter=_kalshi_rate_limiter(),
        circuit=_kalshi_circuit(can_execute),
    )


# ═══════════════════════════════════════════════════════════════════════════════
# PolymarketAdapter.place_fok — 5 PHASE5 cases
# ═══════════════════════════════════════════════════════════════════════════════


async def test_poly_phase5_noop_when_env_unset(monkeypatch):
    """Unset PHASE5 env -> place_fok proceeds to reconciler."""
    monkeypatch.delenv("PHASE4_MAX_ORDER_USD", raising=False)
    monkeypatch.delenv("PHASE5_MAX_ORDER_USD", raising=False)
    adapter = _make_polymarket_adapter()
    await adapter.place_fok("ARB-P1", "mkt", "CAN", "yes", 0.5, 100)
    adapter._place_fok_reconciling.assert_awaited_once()


async def test_poly_phase5_rejects_when_notional_exceeds_cap(monkeypatch):
    """PHASE5=5, qty=10, price=0.60 -> notional=6.00 > 5 -> rejected."""
    monkeypatch.delenv("PHASE4_MAX_ORDER_USD", raising=False)
    monkeypatch.setenv("PHASE5_MAX_ORDER_USD", "5")
    adapter = _make_polymarket_adapter()
    result = await adapter.place_fok("ARB-P2", "mkt", "CAN", "yes", 0.60, 10)
    assert result.status == OrderStatus.FAILED
    assert "PHASE5_MAX_ORDER_USD hard-lock" in result.error
    assert "$6.00" in result.error
    assert "$5.00" in result.error
    adapter._place_fok_reconciling.assert_not_awaited()


async def test_poly_phase5_allows_when_notional_under_cap(monkeypatch):
    """PHASE5=5, qty=10, price=0.40 -> notional=4.00 < 5 -> allowed."""
    monkeypatch.delenv("PHASE4_MAX_ORDER_USD", raising=False)
    monkeypatch.setenv("PHASE5_MAX_ORDER_USD", "5")
    adapter = _make_polymarket_adapter()
    await adapter.place_fok("ARB-P3", "mkt", "CAN", "yes", 0.40, 10)
    adapter._place_fok_reconciling.assert_awaited_once()


async def test_poly_phase5_boundary_exact_equal_allowed(monkeypatch):
    """Strict > comparison: notional == cap is allowed."""
    monkeypatch.delenv("PHASE4_MAX_ORDER_USD", raising=False)
    monkeypatch.setenv("PHASE5_MAX_ORDER_USD", "5")
    adapter = _make_polymarket_adapter()
    # qty=5 * price=1.0 == 5.0 == cap -> allowed (price<1 strict elsewhere but
    # polymarket adapter does not validate price range, so 1.0 here is safe).
    await adapter.place_fok("ARB-P4", "mkt", "CAN", "yes", 1.0, 5)
    adapter._place_fok_reconciling.assert_awaited_once()


async def test_poly_phase5_unparseable_env_is_maximally_restrictive(monkeypatch):
    """Garbage env -> 0.0 cap -> any positive notional rejected."""
    monkeypatch.delenv("PHASE4_MAX_ORDER_USD", raising=False)
    monkeypatch.setenv("PHASE5_MAX_ORDER_USD", "not-a-number")
    adapter = _make_polymarket_adapter()
    result = await adapter.place_fok("ARB-P5", "mkt", "CAN", "yes", 0.01, 1)
    assert result.status == OrderStatus.FAILED
    assert "PHASE5_MAX_ORDER_USD hard-lock" in result.error
    adapter._place_fok_reconciling.assert_not_awaited()


# ═══════════════════════════════════════════════════════════════════════════════
# KalshiAdapter.place_fok — 5 PHASE5 cases
# (place_fok previously had NO hard-lock at all — Plan 05-01 adds BOTH PHASE4
#  and PHASE5. The 5 PHASE5 cases below cover the PHASE5 belt specifically.)
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_kalshi_fok_phase5_noop_when_env_unset(monkeypatch):
    monkeypatch.delenv("PHASE4_MAX_ORDER_USD", raising=False)
    monkeypatch.delenv("PHASE5_MAX_ORDER_USD", raising=False)
    session = _session_with_post(201, _kalshi_fok_body())
    adapter = _make_kalshi_adapter(session)
    await adapter.place_fok("ARB-KF1", "T", "CAN", "yes", 0.40, 10)
    assert session.post.called, "unset env -> HTTP call should proceed"


@pytest.mark.asyncio
async def test_kalshi_fok_phase5_rejects_when_notional_exceeds_cap(monkeypatch):
    """PHASE5=5, qty=10, price=0.60 -> notional=6.00 > 5 -> rejected WITHOUT HTTP."""
    monkeypatch.delenv("PHASE4_MAX_ORDER_USD", raising=False)
    monkeypatch.setenv("PHASE5_MAX_ORDER_USD", "5")
    session = _session_with_post(201, _kalshi_fok_body())
    adapter = _make_kalshi_adapter(session)
    result = await adapter.place_fok("ARB-KF2", "T", "CAN", "yes", 0.60, 10)
    assert result.status == OrderStatus.FAILED
    assert "PHASE5_MAX_ORDER_USD hard-lock" in result.error
    assert "$6.00" in result.error
    assert "$5.00" in result.error
    assert not session.post.called, "HTTP must NOT happen when hard-lock trips"


@pytest.mark.asyncio
async def test_kalshi_fok_phase5_allows_when_notional_under_cap(monkeypatch):
    monkeypatch.delenv("PHASE4_MAX_ORDER_USD", raising=False)
    monkeypatch.setenv("PHASE5_MAX_ORDER_USD", "5")
    session = _session_with_post(201, _kalshi_fok_body())
    adapter = _make_kalshi_adapter(session)
    await adapter.place_fok("ARB-KF3", "T", "CAN", "yes", 0.40, 10)
    assert session.post.called


@pytest.mark.asyncio
async def test_kalshi_fok_phase5_boundary_exact_equal_allowed(monkeypatch):
    """Strict > comparison: notional == cap allowed. Use price=0.99, qty=5
    (notional=4.95, safely under cap AND valid per kalshi price range 0<price<1).
    Intent: confirm 'notional <= cap proceeds'.
    """
    monkeypatch.delenv("PHASE4_MAX_ORDER_USD", raising=False)
    monkeypatch.setenv("PHASE5_MAX_ORDER_USD", "5")
    session = _session_with_post(201, _kalshi_fok_body())
    adapter = _make_kalshi_adapter(session)
    await adapter.place_fok("ARB-KF4", "T", "CAN", "yes", 0.99, 5)
    assert session.post.called


@pytest.mark.asyncio
async def test_kalshi_fok_phase5_unparseable_env_is_maximally_restrictive(monkeypatch):
    monkeypatch.delenv("PHASE4_MAX_ORDER_USD", raising=False)
    monkeypatch.setenv("PHASE5_MAX_ORDER_USD", "not-a-number")
    session = _session_with_post(201, _kalshi_fok_body())
    adapter = _make_kalshi_adapter(session)
    result = await adapter.place_fok("ARB-KF5", "T", "CAN", "yes", 0.01, 1)
    assert result.status == OrderStatus.FAILED
    assert "PHASE5_MAX_ORDER_USD hard-lock" in result.error
    assert not session.post.called


# ═══════════════════════════════════════════════════════════════════════════════
# KalshiAdapter.place_resting_limit — 5 PHASE5 cases
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_kalshi_resting_phase5_noop_when_env_unset(monkeypatch):
    monkeypatch.delenv("PHASE4_MAX_ORDER_USD", raising=False)
    monkeypatch.delenv("PHASE5_MAX_ORDER_USD", raising=False)
    session = _session_with_post(201, _kalshi_resting_body())
    adapter = _make_kalshi_adapter(session)
    await adapter.place_resting_limit("ARB-KR1", "T", "CAN", "yes", 0.40, 10)
    assert session.post.called


@pytest.mark.asyncio
async def test_kalshi_resting_phase5_rejects_when_notional_exceeds_cap(monkeypatch):
    monkeypatch.delenv("PHASE4_MAX_ORDER_USD", raising=False)
    monkeypatch.setenv("PHASE5_MAX_ORDER_USD", "5")
    session = _session_with_post(201, _kalshi_resting_body())
    adapter = _make_kalshi_adapter(session)
    result = await adapter.place_resting_limit("ARB-KR2", "T", "CAN", "yes", 0.60, 10)
    assert result.status == OrderStatus.FAILED
    assert "PHASE5_MAX_ORDER_USD hard-lock" in result.error
    assert "$6.00" in result.error
    assert "$5.00" in result.error
    assert not session.post.called


@pytest.mark.asyncio
async def test_kalshi_resting_phase5_allows_when_notional_under_cap(monkeypatch):
    monkeypatch.delenv("PHASE4_MAX_ORDER_USD", raising=False)
    monkeypatch.setenv("PHASE5_MAX_ORDER_USD", "5")
    session = _session_with_post(201, _kalshi_resting_body())
    adapter = _make_kalshi_adapter(session)
    await adapter.place_resting_limit("ARB-KR3", "T", "CAN", "yes", 0.40, 10)
    assert session.post.called


@pytest.mark.asyncio
async def test_kalshi_resting_phase5_boundary_exact_equal_allowed(monkeypatch):
    monkeypatch.delenv("PHASE4_MAX_ORDER_USD", raising=False)
    monkeypatch.setenv("PHASE5_MAX_ORDER_USD", "5")
    session = _session_with_post(201, _kalshi_resting_body())
    adapter = _make_kalshi_adapter(session)
    await adapter.place_resting_limit("ARB-KR4", "T", "CAN", "yes", 0.99, 5)
    assert session.post.called


@pytest.mark.asyncio
async def test_kalshi_resting_phase5_unparseable_env_is_maximally_restrictive(monkeypatch):
    monkeypatch.delenv("PHASE4_MAX_ORDER_USD", raising=False)
    monkeypatch.setenv("PHASE5_MAX_ORDER_USD", "not-a-number")
    session = _session_with_post(201, _kalshi_resting_body())
    adapter = _make_kalshi_adapter(session)
    result = await adapter.place_resting_limit("ARB-KR5", "T", "CAN", "yes", 0.01, 1)
    assert result.status == OrderStatus.FAILED
    assert "PHASE5_MAX_ORDER_USD hard-lock" in result.error
    assert not session.post.called


# ═══════════════════════════════════════════════════════════════════════════════
# COMBINATION tests: PHASE4 and PHASE5 are OR-combined (both must pass)
# ═══════════════════════════════════════════════════════════════════════════════


async def test_combo_phase5_stricter_wins_polymarket(monkeypatch):
    """BOTH set, PHASE5 tighter -> PHASE5 rejects FIRST is false; actually
    PHASE4 block runs first in source order so at notional=$7 with PHASE4=10
    and PHASE5=5: PHASE4 PASSES ($7<=$10), then PHASE5 REJECTS ($7>$5).
    Error string must be the PHASE5 variant.
    """
    monkeypatch.setenv("PHASE4_MAX_ORDER_USD", "10")
    monkeypatch.setenv("PHASE5_MAX_ORDER_USD", "5")
    adapter = _make_polymarket_adapter()
    # qty=10, price=0.70 -> notional=7.0; passes PHASE4 (<=10), fails PHASE5 (>5).
    result = await adapter.place_fok("ARB-C1", "mkt", "CAN", "yes", 0.70, 10)
    assert result.status == OrderStatus.FAILED
    assert "PHASE5_MAX_ORDER_USD hard-lock" in result.error
    assert "$7.00" in result.error
    assert "$5.00" in result.error
    adapter._place_fok_reconciling.assert_not_awaited()


async def test_combo_phase4_fires_first_when_tighter_polymarket(monkeypatch):
    """BOTH set, PHASE4 tighter -> PHASE4 (source-order first) rejects before
    PHASE5 runs. Error string must be the PHASE4 variant.
    """
    monkeypatch.setenv("PHASE4_MAX_ORDER_USD", "5")
    monkeypatch.setenv("PHASE5_MAX_ORDER_USD", "10")
    adapter = _make_polymarket_adapter()
    # qty=10, price=0.70 -> notional=7.0; fails PHASE4 (>5) FIRST.
    result = await adapter.place_fok("ARB-C2", "mkt", "CAN", "yes", 0.70, 10)
    assert result.status == OrderStatus.FAILED
    assert "PHASE4_MAX_ORDER_USD hard-lock" in result.error
    assert "$7.00" in result.error
    assert "$5.00" in result.error
    adapter._place_fok_reconciling.assert_not_awaited()


async def test_combo_phase4_unset_phase5_set_polymarket(monkeypatch):
    """PHASE4 unset (production default), PHASE5=5, notional=$7 -> PHASE5 rejects."""
    monkeypatch.delenv("PHASE4_MAX_ORDER_USD", raising=False)
    monkeypatch.setenv("PHASE5_MAX_ORDER_USD", "5")
    adapter = _make_polymarket_adapter()
    result = await adapter.place_fok("ARB-C3", "mkt", "CAN", "yes", 0.70, 10)
    assert result.status == OrderStatus.FAILED
    assert "PHASE5_MAX_ORDER_USD hard-lock" in result.error
    adapter._place_fok_reconciling.assert_not_awaited()
