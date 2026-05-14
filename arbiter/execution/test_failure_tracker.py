"""Tests for ``arbiter.execution.failure_tracker``."""
from __future__ import annotations

import time
from dataclasses import dataclass

import pytest

from arbiter.execution.engine import Order, OrderStatus
from arbiter.execution.failure_tracker import (
    FailureTracker,
    FailureTrackerConfig,
    _price_bucket_cents,
    make_failure_tracker_from_env,
)


@dataclass
class _Opp:
    canonical_id: str = "MKT_X"
    description: str = "Mkt X"
    yes_platform: str = "kalshi"
    no_platform: str = "polymarket"
    yes_price: float = 0.42
    no_price: float = 0.55


def _order(side: str, price: float, status: OrderStatus = OrderStatus.FAILED) -> Order:
    return Order(
        order_id=f"ARB-1-{side.upper()}-x",
        platform="kalshi" if side == "yes" else "polymarket",
        market_id="M",
        canonical_id="MKT_X",
        side=side,
        price=price,
        quantity=10,
        status=status,
    )


@dataclass
class _Exec:
    status: str
    leg_yes: Order
    leg_no: Order
    opportunity: _Opp


def test_price_bucket_cents_rounds_into_5c_buckets():
    assert _price_bucket_cents(0.40) == 40
    assert _price_bucket_cents(0.42) == 40
    assert _price_bucket_cents(0.44) == 40
    assert _price_bucket_cents(0.45) == 45
    assert _price_bucket_cents(0.49) == 45
    assert _price_bucket_cents(0.50) == 50


def test_price_bucket_clamps_invalid_bucket_size():
    assert _price_bucket_cents(0.42, bucket_size=0) == 42
    assert _price_bucket_cents(0.42, bucket_size=-1) == 42


def test_should_skip_returns_false_below_threshold():
    tracker = FailureTracker(
        config=FailureTrackerConfig(backoff_threshold=3, window_seconds=3600),
    )
    tracker._record_failure("MKT_X", "yes", 0.42)
    tracker._record_failure("MKT_X", "yes", 0.43)
    skip, reason = tracker.should_skip("MKT_X", "yes", 0.42)
    assert skip is False
    assert reason == ""


def test_should_skip_returns_true_after_threshold():
    tracker = FailureTracker(
        config=FailureTrackerConfig(backoff_threshold=3, window_seconds=3600),
    )
    for _ in range(3):
        tracker._record_failure("MKT_X", "yes", 0.42)
    skip, reason = tracker.should_skip("MKT_X", "yes", 0.42)
    assert skip is True
    assert "backoff" in reason.lower()


def test_should_skip_groups_prices_into_bucket():
    """0.41 and 0.43 land in the same 5c bucket so failures count together."""
    tracker = FailureTracker(
        config=FailureTrackerConfig(backoff_threshold=3, bucket_size_cents=5),
    )
    tracker._record_failure("MKT_X", "yes", 0.41)
    tracker._record_failure("MKT_X", "yes", 0.43)
    tracker._record_failure("MKT_X", "yes", 0.44)
    skip, _ = tracker.should_skip("MKT_X", "yes", 0.42)
    assert skip is True


def test_should_skip_distinct_buckets_do_not_share_count():
    tracker = FailureTracker(
        config=FailureTrackerConfig(backoff_threshold=3, bucket_size_cents=5),
    )
    tracker._record_failure("MKT_X", "yes", 0.41)
    tracker._record_failure("MKT_X", "yes", 0.43)
    tracker._record_failure("MKT_X", "yes", 0.48)  # different bucket
    skip, _ = tracker.should_skip("MKT_X", "yes", 0.42)
    assert skip is False


def test_should_skip_sides_isolated():
    tracker = FailureTracker(
        config=FailureTrackerConfig(backoff_threshold=3),
    )
    for _ in range(3):
        tracker._record_failure("MKT_X", "yes", 0.42)
    skip_yes, _ = tracker.should_skip("MKT_X", "yes", 0.42)
    skip_no, _ = tracker.should_skip("MKT_X", "no", 0.42)
    assert skip_yes is True
    assert skip_no is False


def test_window_prunes_old_failures():
    tracker = FailureTracker(
        config=FailureTrackerConfig(backoff_threshold=3, window_seconds=10.0),
    )
    now = time.time()
    # 3 failures, all outside the 10s window
    tracker._record_failure("MKT_X", "yes", 0.42, now=now - 50)
    tracker._record_failure("MKT_X", "yes", 0.42, now=now - 40)
    tracker._record_failure("MKT_X", "yes", 0.42, now=now - 30)
    # Threshold tripped backoff at the moment of the 3rd record, but
    # the cool-off itself has long expired:
    tracker._backoff_until.clear()
    skip, _ = tracker.should_skip("MKT_X", "yes", 0.42)
    assert skip is False


def test_record_execution_skips_filled_executions():
    tracker = FailureTracker(config=FailureTrackerConfig(backoff_threshold=2))
    execution = _Exec(
        status="filled",
        leg_yes=_order("yes", 0.42, status=OrderStatus.FILLED),
        leg_no=_order("no", 0.55, status=OrderStatus.FILLED),
        opportunity=_Opp(),
    )
    tracker.record_execution(execution)
    tracker.record_execution(execution)
    assert tracker.stats.recorded == 0


def test_record_execution_records_failed_leg_only():
    tracker = FailureTracker(config=FailureTrackerConfig(backoff_threshold=99))
    execution = _Exec(
        status="failed",
        leg_yes=_order("yes", 0.42, status=OrderStatus.FAILED),
        leg_no=_order("no", 0.55, status=OrderStatus.FILLED),  # not failed
        opportunity=_Opp(),
    )
    tracker.record_execution(execution)
    # Only the YES leg failed -> exactly one recorded sample.
    assert tracker.stats.recorded == 1
    snapshot = tracker.snapshot()
    sides = {p["side"] for p in snapshot["patterns"]}
    assert sides == {"yes"}


def test_record_execution_treats_recovering_as_failure():
    tracker = FailureTracker(config=FailureTrackerConfig(backoff_threshold=99))
    execution = _Exec(
        status="recovering",
        leg_yes=_order("yes", 0.42, status=OrderStatus.FILLED),  # survivor
        leg_no=_order("no", 0.55, status=OrderStatus.ABORTED),  # killed
        opportunity=_Opp(),
    )
    tracker.record_execution(execution)
    # The aborted leg is the actual failure.
    snapshot = tracker.snapshot()
    assert len(snapshot["patterns"]) == 1
    assert snapshot["patterns"][0]["side"] == "no"


def test_make_failure_tracker_from_env_reads_overrides():
    tracker = make_failure_tracker_from_env(
        engine=None,
        config_env={
            "FAILURE_TRACKER_WINDOW_SECONDS": "60",
            "FAILURE_TRACKER_BACKOFF_THRESHOLD": "2",
            "FAILURE_TRACKER_BACKOFF_SECONDS": "10",
            "FAILURE_TRACKER_BUCKET_CENTS": "10",
        },
    )
    assert tracker._config.window_seconds == 60.0
    assert tracker._config.backoff_threshold == 2
    assert tracker._config.backoff_seconds == 10.0
    assert tracker._config.bucket_size_cents == 10


def test_snapshot_returns_only_active_patterns():
    tracker = FailureTracker(
        config=FailureTrackerConfig(backoff_threshold=3, window_seconds=3600),
    )
    tracker._record_failure("MKT_X", "yes", 0.42)
    tracker._record_failure("MKT_Y", "no", 0.55)
    snap = tracker.snapshot()
    pairs = {(p["canonical_id"], p["side"]) for p in snap["patterns"]}
    assert pairs == {("MKT_X", "yes"), ("MKT_Y", "no")}
    assert snap["config"]["backoff_threshold"] == 3
    assert snap["stats"]["recorded"] == 2


def test_backoff_release_after_cooloff_expires():
    tracker = FailureTracker(
        config=FailureTrackerConfig(
            backoff_threshold=2,
            backoff_seconds=0.01,
        ),
    )
    tracker._record_failure("MKT_X", "yes", 0.42)
    tracker._record_failure("MKT_X", "yes", 0.42)
    skip, _ = tracker.should_skip("MKT_X", "yes", 0.42)
    assert skip is True
    # Wait out the cool-off and also prune the history so the live
    # threshold check doesn't immediately re-arm.
    time.sleep(0.02)
    # Move the recorded timestamps outside the window too.
    tracker._history.clear()
    skip_again, _ = tracker.should_skip("MKT_X", "yes", 0.42)
    assert skip_again is False
