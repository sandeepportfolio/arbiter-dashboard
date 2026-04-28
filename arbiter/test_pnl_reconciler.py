from types import SimpleNamespace

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
