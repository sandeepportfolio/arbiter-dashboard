"""Fix #5: the deposit auto-detector must not silently swallow material drift.

PnLReconciler._detect_deposits auto-classifies any discrepancy >= $1 as a
deposit/withdrawal and rebaselines before reconcile()'s $100 drift flag ever
sees it — making the flag structurally unreachable for anything under $100. A
real -$10.30 ForecastEx drop with zero corroborating activity was swallowed
this way at INFO level. The fix keeps the $1 auto-absorb but ALSO fires an
on_large_deposit callback (wired to ExecutionEngine.record_incident) for any
move >= DEPOSIT_INCIDENT_THRESHOLD ($5), so it surfaces to the operator.
"""
import asyncio

import pytest

from arbiter.audit.pnl_reconciler import PnLReconciler


def _make_reconciler(captured):
    async def on_large_deposit(platform, amount, before, after):
        captured.append((platform, amount, before, after))

    return PnLReconciler(log_to_disk=False, on_large_deposit=on_large_deposit)


@pytest.mark.asyncio
async def test_large_withdrawal_fires_incident_callback():
    captured: list = []
    rec = _make_reconciler(captured)
    # -$10.30 with no corroborating trades — the exact real-world event.
    rec.record_deposit("forecastex", -10.30, 100.00, 89.70)
    await asyncio.sleep(0)  # let the ensure_future callback run
    assert captured == [("forecastex", -10.30, 100.00, 89.70)]


@pytest.mark.asyncio
async def test_large_deposit_fires_incident_callback():
    captured: list = []
    rec = _make_reconciler(captured)
    rec.record_deposit("kalshi", 25.00, 300.00, 325.00)
    await asyncio.sleep(0)
    assert len(captured) == 1
    assert captured[0][0] == "kalshi"
    assert captured[0][1] == 25.00


@pytest.mark.asyncio
async def test_small_drift_below_threshold_does_not_fire_incident():
    captured: list = []
    rec = _make_reconciler(captured)
    # $2.50 is above the $1 auto-absorb but below the $5 incident threshold —
    # genuine fee/rounding noise, must stay silent (no operator alert).
    rec.record_deposit("polymarket", 2.50, 300.00, 302.50)
    await asyncio.sleep(0)
    assert captured == []


@pytest.mark.asyncio
async def test_threshold_boundary_exactly_five_fires():
    captured: list = []
    rec = _make_reconciler(captured)
    rec.record_deposit("kalshi", -5.00, 100.00, 95.00)
    await asyncio.sleep(0)
    assert len(captured) == 1


def test_incident_threshold_constant_is_five_dollars():
    # Pin the tunable so a future edit that raises it back into the swallow
    # zone is a visible, deliberate change.
    assert PnLReconciler.DEPOSIT_INCIDENT_THRESHOLD == 5.00
