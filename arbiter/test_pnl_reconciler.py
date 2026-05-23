from types import SimpleNamespace

import pytest

from arbiter.audit.pnl_reconciler import PnLReconciler


def make_execution(pnl: float, yes_platform: str = "kalshi", no_platform: str = "polymarket"):
    return SimpleNamespace(
        realized_pnl=pnl,
        opportunity=SimpleNamespace(
            yes_platform=yes_platform,
            no_platform=no_platform,
        ),
    )


def test_load_execution_history_splits_realized_pnl_across_platforms():
    reconciler = PnLReconciler(log_to_disk=False)
    reconciler.set_starting_balance("kalshi", 100.0)
    reconciler.set_starting_balance("polymarket", 200.0)

    reconciler.load_execution_history([make_execution(4.0)])
    report = reconciler.reconcile({"kalshi": 102.0, "polymarket": 202.0})

    assert reconciler.stats["recorded_pnl"] == {"kalshi": 2.0, "polymarket": 2.0}
    assert report.has_flags is False
    assert report.total_recorded_pnl == 4.0


def test_load_execution_history_rebuilds_instead_of_double_counting():
    reconciler = PnLReconciler(log_to_disk=False)
    reconciler.set_starting_balance("kalshi", 100.0)
    reconciler.set_starting_balance("polymarket", 100.0)

    reconciler.load_execution_history([make_execution(6.0)])
    assert reconciler.stats["recorded_pnl"] == {"kalshi": 3.0, "polymarket": 3.0}

    reconciler.load_execution_history([make_execution(2.0)])
    assert reconciler.stats["recorded_pnl"] == {"kalshi": 1.0, "polymarket": 1.0}


@pytest.mark.asyncio
async def test_rebaseline_preserves_execution_history_without_future_drift():
    reconciler = PnLReconciler(log_to_disk=False)
    executions = [make_execution(4.0)]

    await reconciler.rebaseline(
        {"kalshi": 102.0, "polymarket": 202.0},
        executions,
    )

    assert reconciler.stats["starting_balances"] == {
        "kalshi": 100.0,
        "polymarket": 200.0,
    }
    assert reconciler.stats["recorded_pnl"] == {
        "kalshi": 2.0,
        "polymarket": 2.0,
    }

    reconciler.load_execution_history(executions)
    report = reconciler.reconcile({"kalshi": 102.0, "polymarket": 202.0})

    assert report.has_flags is False
    assert reconciler.stats["flag_count"] == 0


def test_record_deposit_dedups_within_window():
    """Two deposits with same platform/before/after within window → second dropped."""
    reconciler = PnLReconciler(log_to_disk=False)
    reconciler.set_starting_balance("kalshi", 100.0)

    reconciler.record_deposit("kalshi", 50.0, balance_before=100.0, balance_after=150.0)
    assert len(reconciler.deposit_history) == 1
    assert reconciler.total_deposits_by_platform["kalshi"] == 50.0
    starting_after_first = reconciler.stats["starting_balances"]["kalshi"]

    # Same balance_before/balance_after again → must be skipped (no double-count).
    reconciler.record_deposit("kalshi", 50.0, balance_before=100.0, balance_after=150.0)
    assert len(reconciler.deposit_history) == 1
    assert reconciler.total_deposits_by_platform["kalshi"] == 50.0
    assert reconciler.stats["starting_balances"]["kalshi"] == starting_after_first


def test_record_deposit_dedup_tolerates_cent_level_noise():
    """Float jitter under the cent tolerance should still be treated as duplicate."""
    reconciler = PnLReconciler(log_to_disk=False)
    reconciler.set_starting_balance("kalshi", 100.0)

    reconciler.record_deposit("kalshi", 50.0, balance_before=100.0, balance_after=150.0)
    reconciler.record_deposit("kalshi", 50.0, balance_before=100.005, balance_after=150.003)
    assert len(reconciler.deposit_history) == 1


@pytest.mark.asyncio
async def test_backfill_initial_deposits_records_event_without_shifting_starting():
    """A platform with starting>0 and no deposit history gets its starting
    balance recorded as an initial-capital deposit. starting balance must
    NOT be shifted (record_deposit-style += amount would double-count)."""
    reconciler = PnLReconciler(log_to_disk=False)
    reconciler.set_starting_balance("forecastex", 298.95)

    backfilled = await reconciler.backfill_initial_deposits_as_events()

    assert backfilled == 1
    assert reconciler.stats["starting_balances"]["forecastex"] == 298.95
    assert reconciler.total_deposits_by_platform["forecastex"] == 298.95
    assert len(reconciler.deposit_history) == 1
    event = reconciler.deposit_history[0]
    assert event["platform"] == "forecastex"
    assert event["amount"] == 298.95
    assert event["balance_before"] == 0.0
    assert event["balance_after"] == 298.95


@pytest.mark.asyncio
async def test_backfill_is_idempotent():
    """Running backfill twice should only insert one event per platform."""
    reconciler = PnLReconciler(log_to_disk=False)
    reconciler.set_starting_balance("forecastex", 100.0)

    first = await reconciler.backfill_initial_deposits_as_events()
    second = await reconciler.backfill_initial_deposits_as_events()

    assert first == 1
    assert second == 0
    assert len(reconciler.deposit_history) == 1
    assert reconciler.total_deposits_by_platform["forecastex"] == 100.0


@pytest.mark.asyncio
async def test_backfill_skips_platforms_with_existing_deposit_history():
    """Kalshi/polymarket-style platforms already have deposit events — backfill
    must leave them alone."""
    reconciler = PnLReconciler(log_to_disk=False)
    reconciler.set_starting_balance("kalshi", 100.0)
    reconciler.record_deposit("kalshi", 50.0, balance_before=100.0, balance_after=150.0)
    starting_before = reconciler.stats["starting_balances"]["kalshi"]
    deposits_before = reconciler.total_deposits_by_platform["kalshi"]
    events_before = len(reconciler.deposit_history)

    backfilled = await reconciler.backfill_initial_deposits_as_events()

    assert backfilled == 0
    assert reconciler.stats["starting_balances"]["kalshi"] == starting_before
    assert reconciler.total_deposits_by_platform["kalshi"] == deposits_before
    assert len(reconciler.deposit_history) == events_before


@pytest.mark.asyncio
async def test_backfill_leaves_capital_math_invariant():
    """capital_basis = original_start + total_deposits must not change."""
    reconciler = PnLReconciler(log_to_disk=False)
    reconciler.set_starting_balance("forecastex", 298.95)

    starting_before = reconciler.stats["starting_balances"]["forecastex"]
    deposits_before = reconciler.total_deposits_by_platform.get("forecastex", 0.0)
    capital_basis_before = starting_before + deposits_before

    await reconciler.backfill_initial_deposits_as_events()

    starting_after = reconciler.stats["starting_balances"]["forecastex"]
    deposits_after = reconciler.total_deposits_by_platform["forecastex"]
    # capital_basis is (original_starting + total_deposits) where
    # original_starting = adjusted_starting - total_deposits. So the visible
    # capital_basis equals starting_balance always. Must not have moved.
    assert starting_after == starting_before
    # original_starting after backfill must be 0, total_deposits == starting.
    original_starting_after = starting_after - deposits_after
    assert original_starting_after == pytest.approx(0.0)
    assert deposits_after == pytest.approx(capital_basis_before)


def test_record_deposit_distinct_amounts_not_deduped():
    """A genuinely different deposit must NOT be dropped by the dedup check."""
    reconciler = PnLReconciler(log_to_disk=False)
    reconciler.set_starting_balance("kalshi", 100.0)

    reconciler.record_deposit("kalshi", 50.0, balance_before=100.0, balance_after=150.0)
    # New deposit on top — same platform, different before/after → keep it.
    reconciler.record_deposit("kalshi", 25.0, balance_before=150.0, balance_after=175.0)
    assert len(reconciler.deposit_history) == 2
    assert reconciler.total_deposits_by_platform["kalshi"] == 75.0


def test_record_deposit_dedup_window_per_platform():
    """Same balances on different platforms must still both record."""
    reconciler = PnLReconciler(log_to_disk=False)
    reconciler.set_starting_balance("kalshi", 100.0)
    reconciler.set_starting_balance("polymarket", 100.0)

    reconciler.record_deposit("kalshi", 50.0, balance_before=100.0, balance_after=150.0)
    reconciler.record_deposit("polymarket", 50.0, balance_before=100.0, balance_after=150.0)
    assert len(reconciler.deposit_history) == 2


def test_record_deposit_dedup_releases_after_window():
    """An identical event AFTER the window expires must be recorded."""
    import time
    reconciler = PnLReconciler(log_to_disk=False)
    reconciler.set_starting_balance("kalshi", 100.0)

    reconciler.record_deposit("kalshi", 50.0, balance_before=100.0, balance_after=150.0)
    # Push the prior event back beyond the dedup window.
    reconciler._deposit_events[-1].timestamp = (
        time.time() - reconciler.DEPOSIT_DEDUP_WINDOW_SEC - 1.0
    )
    reconciler.record_deposit("kalshi", 50.0, balance_before=100.0, balance_after=150.0)
    assert len(reconciler.deposit_history) == 2


def test_detect_deposits_does_not_double_record_within_window():
    """Two reconcile() calls with the same unexplained jump → one deposit_event."""
    reconciler = PnLReconciler(log_to_disk=False)
    reconciler.set_starting_balance("kalshi", 100.0)

    # First reconcile: $50 deposit detected, baseline shifts to $150.
    reconciler.reconcile({"kalshi": 150.0})
    assert len(reconciler.deposit_history) == 1

    # Simulate baseline regression (e.g., load_execution_history reset, or a
    # second poll racing the first) — manually revert starting balance so the
    # detector would otherwise re-fire on the next reconcile.
    reconciler._starting_balances["kalshi"] = 100.0
    reconciler.reconcile({"kalshi": 150.0})
    # Dedup must keep the count at 1.
    assert len(reconciler.deposit_history) == 1


def test_restored_state_resumes_deposit_detection_once_execution_ledger_is_present():
    """After DB restore, live execution context should re-enable deposit detection.

    The first restored reconciliation intentionally skips deposit detection so
    historical trading gains are not misclassified as deposits. Once the engine
    has supplied an execution ledger, a new unexplained balance jump should be
    treated as capital movement instead of permanent reconciliation drift.
    """
    reconciler = PnLReconciler(discrepancy_threshold=1.0, log_to_disk=False)
    reconciler.set_starting_balance("kalshi", 100.0)
    reconciler._restored_from_db = True

    report = reconciler.reconcile(
        {"kalshi": 151.0},
        executions=[make_execution(2.0, yes_platform="kalshi", no_platform="polymarket")],
    )

    assert report.has_flags is False
    assert reconciler.stats["starting_balances"]["kalshi"] == 150.0
    assert reconciler.stats["recorded_pnl"]["kalshi"] == 1.0
    assert reconciler.total_deposits_by_platform["kalshi"] == 50.0
    assert reconciler._restored_from_db is False


def test_runtime_reconciliation_uses_live_execution_ledger():
    from arbiter.main import sync_runtime_reconciliation

    reconciler = PnLReconciler(discrepancy_threshold=1.0, log_to_disk=False)
    reconciler.set_starting_balance("kalshi", 100.0)
    reconciler._restored_from_db = True
    monitor = SimpleNamespace(
        current_balances={"kalshi": SimpleNamespace(balance=151.0)}
    )
    engine = SimpleNamespace(
        execution_history=[
            make_execution(2.0, yes_platform="kalshi", no_platform="polymarket")
        ]
    )

    report = sync_runtime_reconciliation(reconciler, monitor, engine)

    assert report.has_flags is False
    assert reconciler.stats["recorded_pnl"]["kalshi"] == 1.0
    assert reconciler.total_deposits_by_platform["kalshi"] == 50.0
