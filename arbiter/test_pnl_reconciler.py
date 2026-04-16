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
