"""Unit tests for arbiter.live.auto_abort.wire_auto_abort_on_reconcile.

Non-live, mocked supervisor + mocked reconcile_fn. Verifies the fail-closed
semantics spelled out in Plan 05-02 Task 2:

  1. Clean reconcile (empty list)              -> trip_kill NOT called
  2. Fee-mismatch breach (non-empty list)     -> trip_kill called exactly once
  3. reconcile_fn raises                       -> trip_kill STILL called (fail-closed)
  4. Double-invoke with breach                 -> trip_kill called twice
                                                  (the wrapper has no de-dup; the
                                                  real SafetySupervisor.trip_kill is
                                                  internally idempotent)
  5. reconcile_fn returns None (bug case)     -> treat as clean; trip_kill NOT called

Every call to supervisor.trip_kill uses the fixed tag
``by='system:phase5_reconcile_fail'`` so a dashboard filter on that tag can
distinguish Phase 5 reconcile-triggered arms from operator-triggered arms.
"""
from __future__ import annotations

from unittest.mock import AsyncMock

from arbiter.live.auto_abort import wire_auto_abort_on_reconcile


# ─── Test 1: clean reconcile — no trip ───────────────────────────────────────


async def test_clean_reconcile_does_not_trip_kill():
    """Empty discrepancy list: aborted=False, trip_kill NOT called."""
    supervisor = AsyncMock()
    supervisor.trip_kill = AsyncMock()

    async def reconcile_fn():
        return []

    result = await wire_auto_abort_on_reconcile(supervisor, reconcile_fn)

    assert result["aborted"] is False
    assert result["discrepancies"] == []
    supervisor.trip_kill.assert_not_called()


# ─── Test 2: fee-mismatch breach — exactly one trip ──────────────────────────


async def test_fee_mismatch_breach_trips_kill_once():
    """Non-empty discrepancy list: aborted=True, trip_kill called exactly once.

    by='system:phase5_reconcile_fail'; reason contains 'fee_mismatch' and '0.03'.
    """
    supervisor = AsyncMock()
    supervisor.trip_kill = AsyncMock()

    discrepancy = {
        "reason": "fee_mismatch",
        "discrepancy": 0.03,
        "platform": "kalshi",
        "leg_order_id": "K-1",
        "platform_fee": 0.08,
        "computed_fee": 0.05,
        "tolerance": 0.01,
    }

    async def reconcile_fn():
        return [discrepancy]

    result = await wire_auto_abort_on_reconcile(supervisor, reconcile_fn)

    assert result["aborted"] is True
    assert result["discrepancies"] == [discrepancy]
    # Exactly one trip_kill call
    supervisor.trip_kill.assert_awaited_once()
    call_kwargs = supervisor.trip_kill.call_args.kwargs
    assert call_kwargs.get("by") == "system:phase5_reconcile_fail", (
        f"expected by='system:phase5_reconcile_fail'; got {call_kwargs!r}"
    )
    reason = call_kwargs.get("reason", "")
    assert "fee_mismatch" in reason, f"reason must include 'fee_mismatch': {reason!r}"
    assert "0.03" in reason, f"reason must include discrepancy '0.03': {reason!r}"


# ─── Test 3: reconcile raises — fail-closed trip ─────────────────────────────


async def test_reconcile_exception_still_trips_kill_fail_closed():
    """reconcile_fn raises -> trip_kill STILL called (fail-closed per research)."""
    supervisor = AsyncMock()
    supervisor.trip_kill = AsyncMock()

    async def reconcile_fn():
        raise RuntimeError("sim")

    result = await wire_auto_abort_on_reconcile(supervisor, reconcile_fn)

    assert result["aborted"] is True
    assert result.get("error") == "sim"
    supervisor.trip_kill.assert_awaited_once()
    call_kwargs = supervisor.trip_kill.call_args.kwargs
    assert call_kwargs.get("by") == "system:phase5_reconcile_fail"
    reason = call_kwargs.get("reason", "")
    assert "reconcile_error" in reason, (
        f"reason must mention reconcile_error: {reason!r}"
    )
    assert "sim" in reason, f"reason must include original error text: {reason!r}"


# ─── Test 4: double-invoke with breach — wrapper does not de-dup ─────────────


async def test_double_invoke_with_breach_calls_trip_kill_twice():
    """Wrapper has no de-dup: double invocation produces two trip_kill calls.

    The real SafetySupervisor.trip_kill is internally idempotent
    (arbiter/safety/supervisor.py:154-159 — already-armed returns current state
    without re-cancelling). The wrapper does NOT add its own de-dup — responsibility
    is delegated to the supervisor.
    """
    supervisor = AsyncMock()
    supervisor.trip_kill = AsyncMock()

    async def reconcile_fn():
        return [{"reason": "fee_mismatch", "discrepancy": 0.02, "platform": "kalshi"}]

    await wire_auto_abort_on_reconcile(supervisor, reconcile_fn)
    await wire_auto_abort_on_reconcile(supervisor, reconcile_fn)

    assert supervisor.trip_kill.await_count == 2, (
        f"expected trip_kill called twice (no wrapper de-dup); "
        f"got {supervisor.trip_kill.await_count}"
    )


# ─── Test 5: reconcile returns None — treat as clean ─────────────────────────


async def test_reconcile_returns_none_treated_as_clean():
    """Defensive: reconcile_fn returns None (bug case) -> no trip, aborted=False."""
    supervisor = AsyncMock()
    supervisor.trip_kill = AsyncMock()

    async def reconcile_fn():
        return None

    result = await wire_auto_abort_on_reconcile(supervisor, reconcile_fn)

    assert result["aborted"] is False
    assert result["discrepancies"] == []
    supervisor.trip_kill.assert_not_called()
