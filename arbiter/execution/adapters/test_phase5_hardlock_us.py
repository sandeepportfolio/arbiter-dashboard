"""Phase 5 hard-lock integration suite — Polymarket US adapter counterpart.

Ports every test from ``test_phase5_hardlock.py`` (legacy) to
``PolymarketUSAdapter``.  Each test group from the legacy file is reproduced
below with identical semantics, adapted to the US adapter's construction
pattern and its ``OrderRejected``-raising contract.

Design differences between legacy and US adapter that affect the port:
- Legacy adapter returns ``Order(status=FAILED)`` on hard-lock rejection.
  US adapter raises ``OrderRejected`` (spec §5.2).
- Legacy adapter reads caps at call time from ``os.getenv``.
  US adapter receives caps at construction time (constructor args).
- Legacy adapter stubs ``_place_fok_reconciling`` to prevent network calls.
  US adapter stubs ``_sign_and_send`` (same purpose).

Groups ported:
  A. PHASE5 gate — 5 unit tests (mirrors PolymarketAdapter PHASE5 cases)
  B. PHASE4 gate — 5 unit tests (mirrors PolymarketAdapter PHASE4 cases;
     PHASE4 is Gate 1 so its tests are included even though the legacy file
     focused on PHASE5 additions — the combination tests prove both gates)
  C. Combination tests — 3 tests (mirrors legacy combo section)
  D. Supervisor-armed gate — 3 tests (US adapter adds Gate 3; no legacy
     equivalent for Polymarket because legacy adapter doesn't check supervisor)

Tests NOT ported (inapplicable to US path):
  - KalshiAdapter.place_fok PHASE5 cases — different adapter, not applicable.
  - KalshiAdapter.place_resting_limit PHASE5 cases — same reason.
  - EOA signature type tests — US path uses Ed25519, no EOA concept.

Async dispatch: legacy Polymarket tests use ``async def`` with no marker
(root conftest's ``pytest_pyfunc_call`` handles them).  We follow the same
convention here for consistency.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from arbiter.execution.adapters.exceptions import OrderRejected
from arbiter.execution.adapters.polymarket_us import PolymarketUSAdapter


# ─── helpers ──────────────────────────────────────────────────────────────────

def _make_client() -> MagicMock:
    client = MagicMock()
    client.place_order = AsyncMock(
        return_value={"orderId": "ord-ok", "status": "FILLED"}
    )
    return client


def _make_supervisor(is_armed: bool = False) -> MagicMock:
    sv = MagicMock()
    sv.is_armed = is_armed
    return sv


def _make_adapter(
    *,
    phase4_max_usd: float | None = None,
    phase5_max_usd: float | None = None,
    supervisor=None,
    client=None,
) -> PolymarketUSAdapter:
    if client is None:
        client = _make_client()
    adapter = PolymarketUSAdapter(
        client=client,
        phase4_max_usd=phase4_max_usd,
        phase5_max_usd=phase5_max_usd,
        supervisor=supervisor,
    )
    # Stub _sign_and_send so successful paths don't touch network.
    # This mirrors how legacy tests stub _place_fok_reconciling.
    adapter._sign_and_send = AsyncMock(
        return_value=MagicMock(status="FILLED")
    )
    return adapter


# ═══════════════════════════════════════════════════════════════════════════════
# Group A — PHASE5 gate (5 tests, mirrors legacy PolymarketAdapter PHASE5 block)
# ═══════════════════════════════════════════════════════════════════════════════


async def test_us_phase5_noop_when_unset():
    """Unset PHASE5 (None) -> place_fok proceeds to _sign_and_send.
    Mirrors: test_poly_phase5_noop_when_env_unset.
    """
    adapter = _make_adapter(phase4_max_usd=None, phase5_max_usd=None)
    await adapter.place_fok("ARB-P1", "mkt", "CAN", "yes", 0.5, 100)
    adapter._sign_and_send.assert_awaited_once()


async def test_us_phase5_rejects_when_notional_exceeds_cap():
    """PHASE5=$5, qty=10, price=0.60 -> notional=6.00 > 5 -> OrderRejected.
    Mirrors: test_poly_phase5_rejects_when_notional_exceeds_cap.
    """
    adapter = _make_adapter(phase4_max_usd=None, phase5_max_usd=5.0)

    with pytest.raises(OrderRejected) as exc_info:
        await adapter.place_fok("ARB-P2", "mkt", "CAN", "yes", 0.60, 10)

    err = str(exc_info.value)
    assert "PHASE5" in err
    assert "$6.00" in err
    assert "$5.00" in err
    # Gate must fire BEFORE _sign_and_send
    assert adapter._sign_and_send.call_count == 0


async def test_us_phase5_allows_when_notional_under_cap():
    """PHASE5=$5, qty=10, price=0.40 -> notional=4.00 < 5 -> allowed.
    Mirrors: test_poly_phase5_allows_when_notional_under_cap.
    """
    adapter = _make_adapter(phase4_max_usd=None, phase5_max_usd=5.0)
    await adapter.place_fok("ARB-P3", "mkt", "CAN", "yes", 0.40, 10)
    adapter._sign_and_send.assert_awaited_once()


async def test_us_phase5_boundary_exact_equal_allowed():
    """Strict > comparison: notional == cap is allowed.
    Mirrors: test_poly_phase5_boundary_exact_equal_allowed.
    PHASE5=$5, qty=10, price=0.50 -> notional=5.00 == cap -> allowed.
    """
    adapter = _make_adapter(phase4_max_usd=None, phase5_max_usd=5.0)
    await adapter.place_fok("ARB-P4", "mkt", "CAN", "yes", 0.50, 10)
    adapter._sign_and_send.assert_awaited_once()


async def test_us_phase5_none_is_noop_not_zero():
    """None cap is a no-op: any notional is allowed (production default).
    Explicit guard: None must NOT be treated as 0.0 (would block everything).
    Mirrors the intent of: test_poly_phase5_noop_when_env_unset.
    """
    adapter = _make_adapter(phase4_max_usd=None, phase5_max_usd=None)
    # Large notional that would trip if cap were 0.0
    await adapter.place_fok("ARB-P5", "mkt", "CAN", "yes", 0.99, 1000)
    adapter._sign_and_send.assert_awaited_once()


# ═══════════════════════════════════════════════════════════════════════════════
# Group B — PHASE4 gate (5 tests)
# ═══════════════════════════════════════════════════════════════════════════════


async def test_us_phase4_noop_when_unset():
    """PHASE4=None -> place_fok proceeds to _sign_and_send.
    US counterpart of the legacy PHASE4 no-op test.
    """
    adapter = _make_adapter(phase4_max_usd=None, phase5_max_usd=None)
    await adapter.place_fok("ARB-Q1", "mkt", "CAN", "yes", 0.5, 100)
    adapter._sign_and_send.assert_awaited_once()


async def test_us_phase4_rejects_when_notional_exceeds_cap():
    """PHASE4=$5, qty=10, price=0.60 -> notional=6.00 > 5 -> OrderRejected.
    US counterpart of legacy PHASE4 rejection test.
    """
    adapter = _make_adapter(phase4_max_usd=5.0, phase5_max_usd=None)

    with pytest.raises(OrderRejected) as exc_info:
        await adapter.place_fok("ARB-Q2", "mkt", "CAN", "yes", 0.60, 10)

    err = str(exc_info.value)
    assert "PHASE4" in err
    assert "$6.00" in err
    assert "$5.00" in err
    assert adapter._sign_and_send.call_count == 0


async def test_us_phase4_allows_when_notional_under_cap():
    """PHASE4=$5, qty=10, price=0.40 -> notional=4.00 < 5 -> allowed."""
    adapter = _make_adapter(phase4_max_usd=5.0, phase5_max_usd=None)
    await adapter.place_fok("ARB-Q3", "mkt", "CAN", "yes", 0.40, 10)
    adapter._sign_and_send.assert_awaited_once()


async def test_us_phase4_boundary_exact_equal_allowed():
    """Strict > comparison: notional == PHASE4 cap is allowed."""
    adapter = _make_adapter(phase4_max_usd=5.0, phase5_max_usd=None)
    # price=0.50, qty=10 -> notional=5.00 == cap -> allowed
    await adapter.place_fok("ARB-Q4", "mkt", "CAN", "yes", 0.50, 10)
    adapter._sign_and_send.assert_awaited_once()


async def test_us_phase4_none_is_noop_not_zero():
    """PHASE4=None is a no-op, not a 0.0 cap.  Any notional passes."""
    adapter = _make_adapter(phase4_max_usd=None, phase5_max_usd=None)
    await adapter.place_fok("ARB-Q5", "mkt", "CAN", "yes", 0.99, 1000)
    adapter._sign_and_send.assert_awaited_once()


# ═══════════════════════════════════════════════════════════════════════════════
# Group C — Combination tests (mirrors legacy combo section exactly)
# ═══════════════════════════════════════════════════════════════════════════════


async def test_us_combo_phase5_stricter_wins():
    """BOTH set, PHASE5 tighter than PHASE4.
    PHASE4=10, PHASE5=5, qty=10, price=0.70 -> notional=7.0
    PHASE4 passes ($7 <= $10), then PHASE5 rejects ($7 > $5).
    Error must be PHASE5 variant.
    Mirrors: test_combo_phase5_stricter_wins_polymarket.
    """
    adapter = _make_adapter(phase4_max_usd=10.0, phase5_max_usd=5.0)

    with pytest.raises(OrderRejected) as exc_info:
        await adapter.place_fok("ARB-C1", "mkt", "CAN", "yes", 0.70, 10)

    err = str(exc_info.value)
    assert "PHASE5" in err
    assert "$7.00" in err
    assert "$5.00" in err
    assert adapter._sign_and_send.call_count == 0


async def test_us_combo_phase4_fires_first_when_tighter():
    """BOTH set, PHASE4 tighter than PHASE5.
    PHASE4=5, PHASE5=10, qty=10, price=0.70 -> notional=7.0
    PHASE4 rejects ($7 > $5) FIRST.  Error must be PHASE4 variant.
    Mirrors: test_combo_phase4_fires_first_when_tighter_polymarket.
    """
    adapter = _make_adapter(phase4_max_usd=5.0, phase5_max_usd=10.0)

    with pytest.raises(OrderRejected) as exc_info:
        await adapter.place_fok("ARB-C2", "mkt", "CAN", "yes", 0.70, 10)

    err = str(exc_info.value)
    assert "PHASE4" in err
    assert "$7.00" in err
    assert "$5.00" in err
    assert adapter._sign_and_send.call_count == 0


async def test_us_combo_phase4_unset_phase5_set():
    """PHASE4=None (production default), PHASE5=5, notional=$7 -> PHASE5 rejects.
    Mirrors: test_combo_phase4_unset_phase5_set_polymarket.
    """
    adapter = _make_adapter(phase4_max_usd=None, phase5_max_usd=5.0)

    with pytest.raises(OrderRejected) as exc_info:
        await adapter.place_fok("ARB-C3", "mkt", "CAN", "yes", 0.70, 10)

    err = str(exc_info.value)
    assert "PHASE5" in err
    assert adapter._sign_and_send.call_count == 0


# ═══════════════════════════════════════════════════════════════════════════════
# Group D — Supervisor-armed gate (3 tests; no legacy equivalent for Polymarket)
# ═══════════════════════════════════════════════════════════════════════════════


async def test_us_supervisor_armed_rejects_when_armed():
    """supervisor.is_armed=True -> OrderRejected (Gate 3).
    _sign_and_send must never be called.
    """
    supervisor = _make_supervisor(is_armed=True)
    adapter = _make_adapter(
        phase4_max_usd=100.0,   # caps pass
        phase5_max_usd=100.0,
        supervisor=supervisor,
    )

    with pytest.raises(OrderRejected) as exc_info:
        await adapter.place_fok("ARB-D1", "mkt", "CAN", "yes", 0.50, 10)

    assert "supervisor" in str(exc_info.value).lower() or "armed" in str(exc_info.value).lower()
    assert adapter._sign_and_send.call_count == 0


async def test_us_supervisor_not_armed_allows():
    """supervisor.is_armed=False -> Gate 3 passes, _sign_and_send called."""
    supervisor = _make_supervisor(is_armed=False)
    adapter = _make_adapter(
        phase4_max_usd=100.0,
        phase5_max_usd=100.0,
        supervisor=supervisor,
    )
    await adapter.place_fok("ARB-D2", "mkt", "CAN", "yes", 0.50, 10)
    adapter._sign_and_send.assert_awaited_once()


async def test_us_supervisor_none_skips_gate3():
    """supervisor=None (no supervisor injected) -> Gate 3 skipped entirely."""
    adapter = _make_adapter(
        phase4_max_usd=None,
        phase5_max_usd=None,
        supervisor=None,
    )
    await adapter.place_fok("ARB-D3", "mkt", "CAN", "yes", 0.50, 10)
    adapter._sign_and_send.assert_awaited_once()


# ═══════════════════════════════════════════════════════════════════════════════
# Notes on inapplicable legacy tests
# ═══════════════════════════════════════════════════════════════════════════════
#
# The following test groups from test_phase5_hardlock.py are NOT ported:
#
# - KalshiAdapter.place_fok PHASE5 cases (5 tests):
#   Inapplicable — different adapter (KalshiAdapter); US path uses
#   PolymarketUSAdapter which has no Kalshi code paths.
#
# - KalshiAdapter.place_resting_limit PHASE5 cases (5 tests):
#   Inapplicable — Kalshi-specific order type. PolymarketUSAdapter has no
#   place_resting_limit method.
#
# - EOA signature type tests (if any):
#   Inapplicable — US path uses Ed25519 header auth, not EOA wallet signing.
