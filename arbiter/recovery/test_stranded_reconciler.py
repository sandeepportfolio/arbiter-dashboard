"""Tests for StrandedPositionReconciler.

These tests mock out the venue fetchers and exercise:
  - tracking semantics (first_seen / last_seen / dedup)
  - new-position incident emission
  - prune-on-disappear
  - auto-close gate: notional cap, spread cap, illiquid skip, no-adapter
  - reconciler_from_env defaults
"""
from __future__ import annotations

import asyncio
import os
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from arbiter.recovery.stranded_reconciler import (
    ReconcilerSnapshot,
    StrandedPosition,
    StrandedPositionReconciler,
    reconciler_from_env,
)


def _stub_pos(**overrides) -> StrandedPosition:
    defaults = dict(
        platform="kalshi",
        market_id="K-X",
        side="YES",
        qty=10.0,
        cost_basis_usd=5.0,
        mtm_usd=5.5,
        unrealized_usd=0.5,
        best_bid=0.55,
        best_ask=0.56,
        title="Stub market",
        first_seen_ts=time.time(),
        last_seen_ts=time.time(),
    )
    defaults.update(overrides)
    return StrandedPosition(**defaults)



async def test_first_cycle_records_all_observed_as_new():
    engine = MagicMock()
    engine._record_incident = AsyncMock()
    rec = StrandedPositionReconciler(config=SimpleNamespace(), engine=engine)
    rec._fetch_kalshi_positions = AsyncMock(return_value=[
        _stub_pos(market_id="K-A", qty=5),
        _stub_pos(market_id="K-B", qty=10),
    ])
    rec._fetch_polymarket_us_positions = AsyncMock(return_value=[])

    snapshot = await rec.run_once()
    assert snapshot.stranded_count == 2
    # An incident must fire ONCE for each newly-observed lot.
    assert engine._record_incident.await_count == 2



async def test_subsequent_cycle_does_not_re_emit_for_same_lot():
    engine = MagicMock()
    engine._record_incident = AsyncMock()
    rec = StrandedPositionReconciler(config=SimpleNamespace(), engine=engine)
    rec._fetch_kalshi_positions = AsyncMock(return_value=[
        _stub_pos(market_id="K-A", qty=5),
    ])
    rec._fetch_polymarket_us_positions = AsyncMock(return_value=[])

    await rec.run_once()
    await rec.run_once()
    # First cycle: 1 incident. Second cycle: same lot still there, no
    # new incident. Total: 1.
    assert engine._record_incident.await_count == 1



async def test_position_pruned_when_it_disappears_from_venue():
    engine = MagicMock()
    engine._record_incident = AsyncMock()
    rec = StrandedPositionReconciler(config=SimpleNamespace(), engine=engine)
    rec._fetch_polymarket_us_positions = AsyncMock(return_value=[])
    # Cycle 1: position present.
    rec._fetch_kalshi_positions = AsyncMock(return_value=[
        _stub_pos(market_id="K-PRUNE", qty=3),
    ])
    await rec.run_once()
    assert ("kalshi", "K-PRUNE") in rec.tracked
    # Cycle 2: venue no longer reports it (closed / settled).
    rec._fetch_kalshi_positions = AsyncMock(return_value=[])
    snap = await rec.run_once()
    assert ("kalshi", "K-PRUNE") not in rec.tracked
    assert snap.stranded_count == 0



async def test_auto_close_disabled_by_default_does_not_call_adapter():
    """The safety default: ``auto_close=False`` means the reconciler
    only OBSERVES and EMITS incidents — never silently closes. The
    operator stays in the loop unless they explicitly opt in.
    """
    engine = MagicMock()
    engine._record_incident = AsyncMock()
    adapter = MagicMock()
    adapter.place_unwind_sell = AsyncMock()
    rec = StrandedPositionReconciler(
        config=SimpleNamespace(),
        engine=engine,
        adapters={"kalshi": adapter},
        auto_close=False,
    )
    rec._fetch_kalshi_positions = AsyncMock(return_value=[
        _stub_pos(market_id="K-A", qty=3),
    ])
    rec._fetch_polymarket_us_positions = AsyncMock(return_value=[])

    await rec.run_once()
    assert adapter.place_unwind_sell.await_count == 0



async def test_auto_close_skips_when_no_bbo():
    engine = MagicMock()
    engine._record_incident = AsyncMock()
    adapter = MagicMock()
    adapter.place_unwind_sell = AsyncMock()
    rec = StrandedPositionReconciler(
        config=SimpleNamespace(), engine=engine,
        adapters={"kalshi": adapter}, auto_close=True,
    )
    # Empty book → illiquid skip.
    rec._fetch_kalshi_positions = AsyncMock(return_value=[
        _stub_pos(market_id="K-NOBBO", best_bid=0.0, best_ask=0.0),
    ])
    rec._fetch_polymarket_us_positions = AsyncMock(return_value=[])

    await rec.run_once()
    assert adapter.place_unwind_sell.await_count == 0
    pos = rec.tracked[("kalshi", "K-NOBBO")]
    assert pos.auto_close_attempted
    assert "illiquid" in (pos.auto_close_result or "").lower()



async def test_auto_close_skips_when_notional_exceeds_cap():
    """Large positions stay for manual review. The default cap is $10,
    matching the live-trading position-USD ceiling — anything larger
    needs operator eyes before we touch it.
    """
    engine = MagicMock()
    engine._record_incident = AsyncMock()
    adapter = MagicMock()
    adapter.place_unwind_sell = AsyncMock()
    rec = StrandedPositionReconciler(
        config=SimpleNamespace(), engine=engine,
        adapters={"kalshi": adapter},
        auto_close=True, max_auto_close_notional_usd=10.0,
    )
    rec._fetch_kalshi_positions = AsyncMock(return_value=[
        _stub_pos(market_id="K-BIG", qty=100, cost_basis_usd=50.0,
                  best_bid=0.45, best_ask=0.46),
    ])
    rec._fetch_polymarket_us_positions = AsyncMock(return_value=[])

    await rec.run_once()
    assert adapter.place_unwind_sell.await_count == 0
    pos = rec.tracked[("kalshi", "K-BIG")]
    assert "notional" in (pos.auto_close_result or "").lower()



async def test_auto_close_skips_when_spread_too_wide():
    """Wide spreads mean the market is illiquid / unfairly priced; we
    refuse to dump position at a panic level when there's no real
    bid-side liquidity to absorb us.
    """
    engine = MagicMock()
    engine._record_incident = AsyncMock()
    adapter = MagicMock()
    adapter.place_unwind_sell = AsyncMock()
    rec = StrandedPositionReconciler(
        config=SimpleNamespace(), engine=engine,
        adapters={"kalshi": adapter},
        auto_close=True,
        max_auto_close_spread_bps=200.0,  # 2c max
    )
    rec._fetch_kalshi_positions = AsyncMock(return_value=[
        _stub_pos(market_id="K-WIDE", qty=5, cost_basis_usd=2.5,
                  best_bid=0.30, best_ask=0.60),  # 30c spread
    ])
    rec._fetch_polymarket_us_positions = AsyncMock(return_value=[])

    await rec.run_once()
    assert adapter.place_unwind_sell.await_count == 0
    pos = rec.tracked[("kalshi", "K-WIDE")]
    assert "spread" in (pos.auto_close_result or "").lower()



async def test_auto_close_fires_when_gates_pass():
    """When notional is small, spread is tight, BBO exists, and the
    adapter supports place_unwind_sell — the reconciler executes the
    close.
    """
    from arbiter.execution.engine import Order, OrderStatus

    engine = MagicMock()
    engine._record_incident = AsyncMock()
    adapter = MagicMock()
    adapter.place_unwind_sell = AsyncMock(
        return_value=Order(
            order_id="UNWIND-OK", platform="kalshi",
            market_id="K-OK", canonical_id="K-OK", side="yes",
            price=0.01, quantity=3,
            status=OrderStatus.FILLED, fill_qty=3, fill_price=0.55,
            timestamp=time.time(),
        )
    )
    rec = StrandedPositionReconciler(
        config=SimpleNamespace(), engine=engine,
        adapters={"kalshi": adapter},
        auto_close=True,
        max_auto_close_notional_usd=10.0,
        max_auto_close_spread_bps=200.0,
    )
    rec._fetch_kalshi_positions = AsyncMock(return_value=[
        _stub_pos(market_id="K-OK", qty=3, cost_basis_usd=1.5,
                  best_bid=0.55, best_ask=0.56),
    ])
    rec._fetch_polymarket_us_positions = AsyncMock(return_value=[])

    await rec.run_once()
    assert adapter.place_unwind_sell.await_count == 1
    pos = rec.tracked[("kalshi", "K-OK")]
    assert pos.auto_close_attempted
    assert "filled" in (pos.auto_close_result or "").lower()



async def test_auto_close_not_retried_in_following_cycle():
    """Once attempted (success OR failure), the position is marked so
    the next cycle doesn't loop on the same close.
    """
    from arbiter.execution.engine import Order, OrderStatus

    engine = MagicMock()
    engine._record_incident = AsyncMock()
    adapter = MagicMock()
    adapter.place_unwind_sell = AsyncMock(
        return_value=Order(
            order_id="UNWIND-2", platform="kalshi", market_id="K-R",
            canonical_id="K-R", side="yes",
            price=0.01, quantity=2,
            status=OrderStatus.CANCELLED, fill_qty=0, fill_price=0.0,
            timestamp=time.time(),
        )
    )
    rec = StrandedPositionReconciler(
        config=SimpleNamespace(), engine=engine,
        adapters={"kalshi": adapter}, auto_close=True,
        max_auto_close_notional_usd=10.0,
        max_auto_close_spread_bps=200.0,
    )
    rec._fetch_kalshi_positions = AsyncMock(return_value=[
        _stub_pos(market_id="K-R", qty=2, cost_basis_usd=1.0,
                  best_bid=0.55, best_ask=0.56),
    ])
    rec._fetch_polymarket_us_positions = AsyncMock(return_value=[])

    await rec.run_once()
    await rec.run_once()
    # Only ONE close attempt total despite two cycles.
    assert adapter.place_unwind_sell.await_count == 1



async def test_fetch_failure_does_not_prune_tracked_positions_of_failed_venue():
    """Regression: a transient venue outage MUST NOT wipe the
    persistent tracker for that venue's stranded positions. If it
    did, the next successful cycle would re-classify every still-
    stranded lot as ``new_key`` and re-emit a ``stranded_position``
    incident (and Telegram alert) per lot. The dedup invariant the
    operator relies on holds only when prune ignores positions
    whose platform failed to fetch this cycle.
    """
    engine = MagicMock()
    engine._record_incident = AsyncMock()
    rec = StrandedPositionReconciler(config=SimpleNamespace(), engine=engine)
    # Cycle 1: both venues return Kalshi positions seen.
    rec._fetch_kalshi_positions = AsyncMock(return_value=[
        _stub_pos(market_id="K-STAYS", qty=5),
    ])
    rec._fetch_polymarket_us_positions = AsyncMock(return_value=[])
    await rec.run_once()
    assert engine._record_incident.await_count == 1
    assert ("kalshi", "K-STAYS") in rec.tracked

    # Cycle 2: Kalshi fetch fails outright; Polymarket still healthy
    # but has nothing. The Kalshi position MUST remain tracked.
    rec._fetch_kalshi_positions = AsyncMock(
        side_effect=RuntimeError("kalshi 503"),
    )
    rec._fetch_polymarket_us_positions = AsyncMock(return_value=[])
    snap = await rec.run_once()
    assert ("kalshi", "K-STAYS") in rec.tracked, (
        "transient venue outage pruned tracked position — would cause "
        "incident re-emission on recovery"
    )
    assert snap.stranded_count == 1
    assert any("kalshi" in e.lower() for e in snap.errors)

    # Cycle 3: Kalshi recovers and re-reports the same lot. No new
    # incident must fire — dedup must hold across the outage.
    rec._fetch_kalshi_positions = AsyncMock(return_value=[
        _stub_pos(market_id="K-STAYS", qty=5),
    ])
    await rec.run_once()
    assert engine._record_incident.await_count == 1, (
        "stranded incident re-emitted after Kalshi recovery — dedup broke"
    )


async def test_fetch_failure_on_one_venue_does_not_kill_cycle():
    """If Polymarket is down, Kalshi positions must still surface and
    the error must appear in the snapshot's ``errors`` list.
    """
    engine = MagicMock()
    engine._record_incident = AsyncMock()
    rec = StrandedPositionReconciler(config=SimpleNamespace(), engine=engine)
    rec._fetch_kalshi_positions = AsyncMock(return_value=[
        _stub_pos(market_id="K-OK", qty=4),
    ])
    rec._fetch_polymarket_us_positions = AsyncMock(
        side_effect=RuntimeError("polymarket gateway 503"),
    )

    snap = await rec.run_once()
    assert snap.stranded_count == 1
    assert any("polymarket" in e or "503" in e for e in snap.errors)


def test_reconciler_from_env_reads_interval_and_caps(monkeypatch):
    monkeypatch.setenv("STRANDED_RECONCILE_INTERVAL_S", "120")
    monkeypatch.setenv("STRANDED_AUTO_CLOSE", "true")
    monkeypatch.setenv("STRANDED_AUTO_CLOSE_MAX_USD", "5")
    monkeypatch.setenv("STRANDED_AUTO_CLOSE_MAX_SPREAD_BPS", "300")
    monkeypatch.setenv("STRANDED_PENNY_COST_USD", "0.07")
    rec = reconciler_from_env(config=SimpleNamespace(), adapters={}, engine=None)
    assert rec._interval_s == 120.0
    assert rec._auto_close is True
    assert rec._max_auto_close_notional_usd == 5.0
    assert rec._max_auto_close_spread_bps == 300.0
    assert rec._penny_cost_threshold_usd == 0.07


# ───────────────────────────────────────────────────────────────────────
# classify_action tests — these document the mitigation policy
# ───────────────────────────────────────────────────────────────────────

def test_classify_let_settle_for_penny_position():
    """Penny positions (cost/share ≤ 0.05) → let_settle, regardless of
    bid liquidity. Closing fees swallow the recoverable value on
    Kalshi's per-contract floor; binary YES outcome gets $1/share
    while we'd lock in 2-3¢ MTM by closing.
    """
    adapter = MagicMock()
    adapter.place_unwind_sell = AsyncMock()
    rec = StrandedPositionReconciler(
        config=SimpleNamespace(), adapters={"kalshi": adapter},
        auto_close=True, penny_cost_threshold_usd=0.05,
    )
    # 23 contracts of 2¢ Hart-trophy longshot. Liquid book (3¢/4¢).
    pos = _stub_pos(market_id="K-HART-DUBOIS", qty=23, cost_basis_usd=0.46,
                    best_bid=0.03, best_ask=0.04)
    assert rec.classify_action(pos) == "let_settle"


def test_classify_close_market_for_real_value_liquid_polymarket():
    """The 2 polymarket midterms case: $27 cost, liquid binary,
    tight spread → must auto-close to recover value before months
    of opportunity cost.
    """
    adapter = MagicMock()
    adapter.place_unwind_sell = AsyncMock()
    rec = StrandedPositionReconciler(
        config=SimpleNamespace(), adapters={"polymarket": adapter},
        auto_close=True,
        max_auto_close_notional_usd=30.0,
        max_auto_close_spread_bps=1000.0,
        penny_cost_threshold_usd=0.05,
    )
    pos = _stub_pos(
        platform="polymarket", market_id="P-MIDTERMS-DEM",
        qty=50.0, cost_basis_usd=27.0,
        best_bid=0.53, best_ask=0.55,
    )
    assert rec.classify_action(pos) == "close_market"


def test_classify_large_notional_skips_auto_close():
    """A $50 lot exceeds the $30 cap → manual review."""
    adapter = MagicMock()
    adapter.place_unwind_sell = AsyncMock()
    rec = StrandedPositionReconciler(
        config=SimpleNamespace(), adapters={"polymarket": adapter},
        auto_close=True, max_auto_close_notional_usd=30.0,
    )
    pos = _stub_pos(platform="polymarket", market_id="P-WHALE",
                    qty=80.0, cost_basis_usd=50.0,
                    best_bid=0.55, best_ask=0.57)
    assert rec.classify_action(pos) == "large_notional"


def test_classify_no_adapter_when_venue_unconfigured():
    rec = StrandedPositionReconciler(
        config=SimpleNamespace(), adapters={},
        auto_close=True,
    )
    pos = _stub_pos(platform="forecastex", market_id="FX-X",
                    qty=10, cost_basis_usd=2.0)
    assert rec.classify_action(pos) == "no_adapter"


def test_classify_illiquid_no_bbo():
    adapter = MagicMock()
    adapter.place_unwind_sell = AsyncMock()
    rec = StrandedPositionReconciler(
        config=SimpleNamespace(), adapters={"kalshi": adapter},
        auto_close=True, penny_cost_threshold_usd=0.05,
    )
    pos = _stub_pos(qty=10, cost_basis_usd=5.0,
                    best_bid=0.0, best_ask=0.0)
    assert rec.classify_action(pos) == "illiquid_no_bbo"


def test_classify_spread_too_wide():
    adapter = MagicMock()
    adapter.place_unwind_sell = AsyncMock()
    rec = StrandedPositionReconciler(
        config=SimpleNamespace(), adapters={"kalshi": adapter},
        auto_close=True, max_auto_close_spread_bps=200.0,
        penny_cost_threshold_usd=0.05,
    )
    pos = _stub_pos(qty=10, cost_basis_usd=5.0,
                    best_bid=0.30, best_ask=0.60)
    assert rec.classify_action(pos) == "spread_too_wide"


async def test_let_settle_action_never_calls_adapter():
    """Penny → let_settle MUST NOT touch the venue. Fees would eat
    the entire recoverable value.
    """
    engine = MagicMock()
    engine._record_incident = AsyncMock()
    adapter = MagicMock()
    adapter.place_unwind_sell = AsyncMock()
    rec = StrandedPositionReconciler(
        config=SimpleNamespace(), engine=engine,
        adapters={"kalshi": adapter},
        auto_close=True,
        penny_cost_threshold_usd=0.05,
        max_auto_close_notional_usd=30.0,
        max_auto_close_spread_bps=1000.0,
    )
    rec._fetch_kalshi_positions = AsyncMock(return_value=[
        _stub_pos(market_id="K-PENNY", qty=23, cost_basis_usd=0.46,
                  best_bid=0.03, best_ask=0.04),
    ])
    rec._fetch_polymarket_us_positions = AsyncMock(return_value=[])

    await rec.run_once()
    assert adapter.place_unwind_sell.await_count == 0
    pos = rec.tracked[("kalshi", "K-PENNY")]
    assert pos.mitigation_action == "let_settle"
    assert "let_settle" in (pos.auto_close_result or "").lower()


async def test_close_market_action_unwinds_polymarket_midterm():
    """A liquid $27 polymarket midterm position → auto-closed."""
    from arbiter.execution.engine import Order, OrderStatus

    engine = MagicMock()
    engine._record_incident = AsyncMock()
    adapter = MagicMock()
    adapter.place_unwind_sell = AsyncMock(
        return_value=Order(
            order_id="UNWIND-MT", platform="polymarket",
            market_id="P-MIDTERMS", canonical_id="P-MIDTERMS", side="yes",
            price=0.01, quantity=50,
            status=OrderStatus.FILLED, fill_qty=50, fill_price=0.53,
            timestamp=time.time(),
        )
    )
    rec = StrandedPositionReconciler(
        config=SimpleNamespace(), engine=engine,
        adapters={"polymarket": adapter},
        auto_close=True,
        penny_cost_threshold_usd=0.05,
        max_auto_close_notional_usd=30.0,
        max_auto_close_spread_bps=1000.0,
    )
    rec._fetch_kalshi_positions = AsyncMock(return_value=[])
    rec._fetch_polymarket_us_positions = AsyncMock(return_value=[
        _stub_pos(
            platform="polymarket", market_id="P-MIDTERMS",
            qty=50.0, cost_basis_usd=27.0,
            best_bid=0.53, best_ask=0.55,
        ),
    ])

    await rec.run_once()
    assert adapter.place_unwind_sell.await_count == 1
    pos = rec.tracked[("polymarket", "P-MIDTERMS")]
    assert pos.mitigation_action == "closed"


async def test_mitigation_engine_decision_attached_per_position():
    """When a price_store + market_map_provider are wired, every
    tracked position gets a full MitigationEngine decision dict
    surfaced via mitigation_decision."""
    engine = MagicMock()
    engine._record_incident = AsyncMock()
    adapter = MagicMock()
    adapter.place_unwind_sell = AsyncMock()
    adapter.place_fok = AsyncMock()

    class _Store:
        async def get_all_for_market(self, canonical_id):
            return {}

    rec = StrandedPositionReconciler(
        config=SimpleNamespace(), engine=engine,
        adapters={"kalshi": adapter},
        auto_close=False,
        price_store=_Store(),
        market_map_provider=lambda: {},
    )
    rec._fetch_kalshi_positions = AsyncMock(return_value=[
        _stub_pos(market_id="K-EV-CHECK", qty=10, cost_basis_usd=5.0,
                  best_bid=0.45, best_ask=0.55),
    ])
    rec._fetch_polymarket_us_positions = AsyncMock(return_value=[])
    rec._fetch_forecastex_positions = AsyncMock(return_value=[])

    snap = await rec.run_once()
    pos = rec.tracked[("kalshi", "K-EV-CHECK")]
    assert pos.mitigation_decision is not None
    # Engine decisions carry an action + rationale + profitability.
    assert pos.mitigation_decision.get("action") in (
        "HOLD_TO_SETTLE", "CLOSE_NOW", "COMPLETE_ARB", "MANUAL_REVIEW", "HEDGE",
    )
    assert "profitability" in pos.mitigation_decision
    # snapshot dict carries it through to the API/UI consumers.
    assert any(s.get("mitigation_decision") is not None for s in snap.stranded)


async def test_classification_runs_when_auto_close_off_so_ui_can_render():
    """auto_close=False is the safety default. Operators still need
    to see the recommended action on each tracked position so they
    can decide whether to flip the switch.
    """
    engine = MagicMock()
    engine._record_incident = AsyncMock()
    adapter = MagicMock()
    adapter.place_unwind_sell = AsyncMock()
    rec = StrandedPositionReconciler(
        config=SimpleNamespace(), engine=engine,
        adapters={"kalshi": adapter},
        auto_close=False,  # OFF
        penny_cost_threshold_usd=0.05,
    )
    rec._fetch_kalshi_positions = AsyncMock(return_value=[
        _stub_pos(market_id="K-PENNY-OFF", qty=10, cost_basis_usd=0.20,
                  best_bid=0.02, best_ask=0.03),
    ])
    rec._fetch_polymarket_us_positions = AsyncMock(return_value=[])
    snap = await rec.run_once()
    assert adapter.place_unwind_sell.await_count == 0
    pos = rec.tracked[("kalshi", "K-PENNY-OFF")]
    assert pos.mitigation_action == "let_settle"
    # The dict serialization (what the API hands the UI) must carry it.
    assert any(s["mitigation_action"] == "let_settle" for s in snap.stranded)


def test_reconciler_from_env_clamps_interval_minimum(monkeypatch):
    """An overly short interval would hammer the venues' rate limits;
    the reconciler clamps to a 30s floor regardless of env.
    """
    monkeypatch.setenv("STRANDED_RECONCILE_INTERVAL_S", "5")
    rec = reconciler_from_env(config=SimpleNamespace(), adapters={}, engine=None)
    assert rec._interval_s == 30.0


def test_strand_arb_id_fits_db_varchar_40():
    """Regression: live deploy on 2026-05-24 tripped the kill switch
    because the original arb_id format
    ``STRAND-POLYMARKET-paccc-usho-midterms-2026-11-03-dem`` (52 chars)
    overflowed the execution_incidents.arb_id varchar(40) column.
    ExecutionStore.insert_incident raised, _handle_db_failure armed
    SafetySupervisor, live trading went read-only.

    The hash-based replacement MUST stay ≤ 40 chars for the longest
    realistic platform name + any slug content.
    """
    rec = StrandedPositionReconciler(config=SimpleNamespace())
    # Long slug — the actual production case that broke things.
    long = rec._strand_arb_id("polymarket", "paccc-usho-midterms-2026-11-03-dem")
    assert len(long) <= 40, f"arb_id too long: {long!r} ({len(long)} chars)"
    # Two different slugs that share the same prefix must NOT collide
    # (truncation-style ids would; hash-based ids won't).
    a = rec._strand_arb_id("polymarket", "aec-mlb-pit-tor-2026-05-24")
    b = rec._strand_arb_id("polymarket", "aec-mlb-pit-tor-2026-05-25")
    assert a != b, "same-prefix slugs collided — must use a hash, not a prefix"
    # Deterministic across calls (so dedup works).
    assert rec._strand_arb_id("polymarket", "paccc-usho-midterms-2026-11-03-dem") == long


# ───────────────────────────────────────────────────────────────────────
# Rate-limit, active-arb skip, Telegram notify tests
# ───────────────────────────────────────────────────────────────────────


async def test_close_attempt_rate_limited_within_window():
    """Once a close has been attempted on a position, subsequent
    reconciler cycles must NOT retry it until the cool-down window
    elapses. Replaces the legacy one-shot ``auto_close_attempted`` gate
    with a per-position sliding window so a single transient adapter
    failure doesn't permanently strand the position.
    """
    from arbiter.execution.engine import Order, OrderStatus

    engine = MagicMock()
    engine._record_incident = AsyncMock()
    engine._executions = []
    adapter = MagicMock()
    adapter.place_unwind_sell = AsyncMock(
        return_value=Order(
            order_id="UNWIND-RL", platform="kalshi", market_id="K-RL",
            canonical_id="K-RL", side="yes",
            price=0.01, quantity=3,
            status=OrderStatus.CANCELLED, fill_qty=0, fill_price=0.0,
            timestamp=time.time(),
        )
    )
    rec = StrandedPositionReconciler(
        config=SimpleNamespace(), engine=engine,
        adapters={"kalshi": adapter}, auto_close=True,
        max_auto_close_notional_usd=10.0,
        max_auto_close_spread_bps=200.0,
        close_retry_window_seconds=300.0,
    )
    rec._fetch_kalshi_positions = AsyncMock(return_value=[
        _stub_pos(market_id="K-RL", qty=3, cost_basis_usd=1.5,
                  best_bid=0.55, best_ask=0.56),
    ])
    rec._fetch_polymarket_us_positions = AsyncMock(return_value=[])
    rec._fetch_forecastex_positions = AsyncMock(return_value=[])

    await rec.run_once()
    assert adapter.place_unwind_sell.await_count == 1
    # Reset the one-shot flag — the rate-limit alone must hold the gate.
    rec.tracked[("kalshi", "K-RL")].auto_close_attempted = False
    await rec.run_once()
    assert adapter.place_unwind_sell.await_count == 1, (
        "rate-limit window failed to gate the retry"
    )

    # Manually expire the cool-down — the gate must re-open.
    rec._last_close_attempt_ts[("kalshi", "K-RL")] = time.time() - 400.0
    rec.tracked[("kalshi", "K-RL")].auto_close_attempted = False
    await rec.run_once()
    assert adapter.place_unwind_sell.await_count == 2


async def test_skip_position_that_is_part_of_active_arb():
    """A stranded lot whose (platform, market_id) is a leg of an in-flight
    ArbExecution must NOT be auto-closed — closing it would crystallise
    the arb's intended PnL and leave the other leg naked."""
    engine = MagicMock()
    engine._record_incident = AsyncMock()
    # Fabricate an active execution that owns the same kalshi market.
    leg_yes = SimpleNamespace(platform="kalshi", market_id="K-ACTIVE")
    leg_no = SimpleNamespace(platform="polymarket", market_id="P-ACTIVE")
    active_ex = SimpleNamespace(
        status="pending", leg_yes=leg_yes, leg_no=leg_no,
    )
    engine._executions = [active_ex]

    adapter = MagicMock()
    adapter.place_unwind_sell = AsyncMock()
    rec = StrandedPositionReconciler(
        config=SimpleNamespace(), engine=engine,
        adapters={"kalshi": adapter}, auto_close=True,
        max_auto_close_notional_usd=10.0,
        max_auto_close_spread_bps=200.0,
    )
    rec._fetch_kalshi_positions = AsyncMock(return_value=[
        _stub_pos(market_id="K-ACTIVE", qty=3, cost_basis_usd=1.5,
                  best_bid=0.55, best_ask=0.56),
    ])
    rec._fetch_polymarket_us_positions = AsyncMock(return_value=[])
    rec._fetch_forecastex_positions = AsyncMock(return_value=[])

    await rec.run_once()
    assert adapter.place_unwind_sell.await_count == 0
    pos = rec.tracked[("kalshi", "K-ACTIVE")]
    assert "active arb" in (pos.auto_close_result or "").lower()


async def test_skip_does_not_apply_to_terminal_arb_executions():
    """An ArbExecution whose status is closed/cancelled/simulated MUST
    NOT block the reconciler — those legs are no longer in flight."""
    from arbiter.execution.engine import Order, OrderStatus

    engine = MagicMock()
    engine._record_incident = AsyncMock()
    leg_yes = SimpleNamespace(platform="kalshi", market_id="K-DONE")
    leg_no = SimpleNamespace(platform="polymarket", market_id="P-DONE")
    closed_ex = SimpleNamespace(
        status="closed", leg_yes=leg_yes, leg_no=leg_no,
    )
    engine._executions = [closed_ex]

    adapter = MagicMock()
    adapter.place_unwind_sell = AsyncMock(
        return_value=Order(
            order_id="UNWIND-DONE", platform="kalshi", market_id="K-DONE",
            canonical_id="K-DONE", side="yes",
            price=0.01, quantity=3,
            status=OrderStatus.FILLED, fill_qty=3, fill_price=0.55,
            timestamp=time.time(),
        )
    )
    rec = StrandedPositionReconciler(
        config=SimpleNamespace(), engine=engine,
        adapters={"kalshi": adapter}, auto_close=True,
        max_auto_close_notional_usd=10.0,
        max_auto_close_spread_bps=200.0,
    )
    rec._fetch_kalshi_positions = AsyncMock(return_value=[
        _stub_pos(market_id="K-DONE", qty=3, cost_basis_usd=1.5,
                  best_bid=0.55, best_ask=0.56),
    ])
    rec._fetch_polymarket_us_positions = AsyncMock(return_value=[])
    rec._fetch_forecastex_positions = AsyncMock(return_value=[])

    await rec.run_once()
    assert adapter.place_unwind_sell.await_count == 1


async def test_close_action_sends_telegram_alert():
    """A successful close must trigger a Telegram alert with the venue,
    side, qty, fill, and PnL line. The notifier's dedup window already
    backstops repeat alerts."""
    from arbiter.execution.engine import Order, OrderStatus

    engine = MagicMock()
    engine._record_incident = AsyncMock()
    engine._executions = []
    adapter = MagicMock()
    adapter.place_unwind_sell = AsyncMock(
        return_value=Order(
            order_id="UNWIND-TG", platform="polymarket",
            market_id="P-TG", canonical_id="P-TG", side="yes",
            price=0.01, quantity=10,
            status=OrderStatus.FILLED, fill_qty=10, fill_price=0.55,
            timestamp=time.time(),
        )
    )
    notifier = MagicMock()
    notifier._enabled = True
    notifier.send = AsyncMock(return_value=True)

    rec = StrandedPositionReconciler(
        config=SimpleNamespace(), engine=engine,
        adapters={"polymarket": adapter},
        auto_close=True,
        max_auto_close_notional_usd=30.0,
        max_auto_close_spread_bps=1000.0,
        penny_cost_threshold_usd=0.05,
        notifier=notifier,
    )
    rec._fetch_kalshi_positions = AsyncMock(return_value=[])
    rec._fetch_polymarket_us_positions = AsyncMock(return_value=[
        _stub_pos(
            platform="polymarket", market_id="P-TG",
            qty=10, cost_basis_usd=4.5,         # 0.45/sh
            best_bid=0.55, best_ask=0.56,
        ),
    ])
    rec._fetch_forecastex_positions = AsyncMock(return_value=[])

    await rec.run_once()
    assert adapter.place_unwind_sell.await_count == 1
    assert notifier.send.await_count == 1
    body = notifier.send.await_args.args[0]
    assert "P-TG" in body
    assert "polymarket" in body.lower()
    assert "P&amp;L" in body or "P&L" in body


async def test_notify_attempt_suppressed_after_repeated_zero_fill():
    """BUG #2: After 3 zero-fill close attempts at the same price for the
    same position, suppress the Telegram alert (log only).

    Without this, a stuck GOP_SENATE_2026-style position pegs the
    reconciler against a price that never clears and spams a Telegram
    alert every cycle. Operators need to see the first 2-3 failures (so
    they can act) and then quiet down so the chat doesn't burn out.
    """
    notifier = MagicMock()
    notifier._enabled = True
    notifier.send = AsyncMock(return_value=True)
    rec = StrandedPositionReconciler(
        config=SimpleNamespace(), engine=None, adapters={},
        notifier=notifier,
    )
    pos = _stub_pos(market_id="GOP_SENATE_2026")
    # Three zero-fill attempts at the same price should produce only
    # the first <suppress_after> alerts.
    for _ in range(5):
        await rec._notify_attempt(
            pos, "COMPLETE_ARB", "polymarket", "P-GOP-2026", "yes",
            price=0.48, qty=10, status="CANCELLED", fill_qty=0.0,
        )
    assert notifier.send.await_count == 3, (
        f"expected 3 alerts before suppression, got {notifier.send.await_count}"
    )


async def test_notify_attempt_resets_when_price_changes():
    """A price change is a new situation worth alerting on — the suppression
    counter must reset so the operator sees the new attempt.
    """
    notifier = MagicMock()
    notifier._enabled = True
    notifier.send = AsyncMock(return_value=True)
    rec = StrandedPositionReconciler(
        config=SimpleNamespace(), engine=None, adapters={}, notifier=notifier,
    )
    pos = _stub_pos(market_id="K-PRICE-MOVE")
    # First 3 attempts at 0.48 → 3 alerts.
    for _ in range(4):
        await rec._notify_attempt(
            pos, "COMPLETE_ARB", "polymarket", "P-X", "yes",
            price=0.48, qty=10, status="CANCELLED", fill_qty=0.0,
        )
    assert notifier.send.await_count == 3
    # Price moved → reset → fresh alert allowed.
    await rec._notify_attempt(
        pos, "COMPLETE_ARB", "polymarket", "P-X", "yes",
        price=0.55, qty=10, status="CANCELLED", fill_qty=0.0,
    )
    assert notifier.send.await_count == 4


def test_reconciler_from_env_defaults_to_30s_interval(monkeypatch):
    """The 5-minute default was wrong — the reconciler should fire on
    the auto_executor cadence (~30s) so stranded mitigation reacts
    inside one trading cycle rather than five."""
    monkeypatch.delenv("STRANDED_RECONCILE_INTERVAL_S", raising=False)
    rec = reconciler_from_env(config=SimpleNamespace(), adapters={}, engine=None)
    assert rec._interval_s == 30.0


def test_reconciler_from_env_reads_close_retry_window(monkeypatch):
    monkeypatch.setenv("STRANDED_CLOSE_RETRY_WINDOW_S", "120")
    rec = reconciler_from_env(config=SimpleNamespace(), adapters={}, engine=None)
    assert rec._close_retry_window_s == 120.0


# ─── BUG #2 follow-up: order-time reprice gate ─────────────────────────


class _FakePriceStore:
    def __init__(self, quotes_by_canonical):
        self._quotes = quotes_by_canonical

    async def get_all_for_market(self, canonical_id):
        return self._quotes.get(canonical_id, {})


def _pp_ns(*, yes_ask=0.0, no_ask=0.0, yes_price=0.0, no_price=0.0,
           age_seconds=2.0):
    return SimpleNamespace(
        yes_ask=yes_ask, no_ask=no_ask,
        yes_price=yes_price, no_price=no_price,
        age_seconds=age_seconds,
    )


async def test_execute_complete_arb_skips_when_price_moved_beyond_tolerance():
    """The decision said BUY NO @ $0.48 but the live book now shows
    $0.55 ask. The reconciler MUST NOT submit the FOK — the GOP_SENATE
    loop is exactly this case."""
    from arbiter.execution.engine import Order, OrderStatus
    pos = _stub_pos(
        platform="polymarket", market_id="POLY-GOP-Y",
        qty=10, cost_basis_usd=4.8,   # 0.48/sh
        best_bid=0.47, best_ask=0.49,
    )
    adapter = MagicMock()
    adapter.place_fok = AsyncMock(return_value=Order(
        order_id="X", platform="kalshi", market_id="KX-GOP-N",
        canonical_id="GOP", side="no", price=0.48, quantity=10,
        status=OrderStatus.CANCELLED, timestamp=time.time(),
    ))
    quotes = {
        "GOP": {
            "kalshi": _pp_ns(no_ask=0.55, age_seconds=2.0),
        },
    }
    rec = StrandedPositionReconciler(
        config=SimpleNamespace(), engine=None,
        adapters={"kalshi": adapter, "polymarket": MagicMock()},
        price_store=_FakePriceStore(quotes),
        auto_close=True,
    )
    decision = {
        "complete_arb": {
            "venue": "kalshi", "market_id": "KX-GOP-N",
            "side": "no", "price": 0.48, "qty": 10,
            "canonical_id": "GOP",
        },
    }
    await rec._execute_complete_arb(pos, decision)
    assert adapter.place_fok.await_count == 0, (
        "FOK must not be submitted when live ask has moved past tolerance"
    )
    assert pos.auto_close_result and "close unprofitable" in pos.auto_close_result


async def test_execute_complete_arb_skips_when_price_data_is_stale():
    """No price update in >60s → skip entirely with a stale-data log line."""
    from arbiter.execution.engine import Order, OrderStatus
    pos = _stub_pos(
        platform="polymarket", market_id="POLY-S-Y",
        qty=10, cost_basis_usd=4.8,
        best_bid=0.47, best_ask=0.49,
    )
    adapter = MagicMock()
    adapter.place_fok = AsyncMock()
    quotes = {
        "S": {
            "kalshi": _pp_ns(no_ask=0.48, age_seconds=180.0),
        },
    }
    rec = StrandedPositionReconciler(
        config=SimpleNamespace(), engine=None,
        adapters={"kalshi": adapter, "polymarket": MagicMock()},
        price_store=_FakePriceStore(quotes), auto_close=True,
    )
    decision = {
        "complete_arb": {
            "venue": "kalshi", "market_id": "KX-S-N",
            "side": "no", "price": 0.48, "qty": 10, "canonical_id": "S",
        },
    }
    await rec._execute_complete_arb(pos, decision)
    assert adapter.place_fok.await_count == 0
    assert "stale price data" in (pos.auto_close_result or "")


async def test_execute_complete_arb_proceeds_when_price_still_valid():
    """Live ask within tolerance of the decision price — FOK is submitted."""
    from arbiter.execution.engine import Order, OrderStatus
    pos = _stub_pos(
        platform="polymarket", market_id="POLY-OK-Y",
        qty=10, cost_basis_usd=4.0,
        best_bid=0.39, best_ask=0.41,
    )
    adapter = MagicMock()
    adapter.place_fok = AsyncMock(return_value=Order(
        order_id="X", platform="kalshi", market_id="KX-OK-N",
        canonical_id="OK", side="no", price=0.49, quantity=10,
        status=OrderStatus.FILLED, fill_qty=10, fill_price=0.49,
        timestamp=time.time(),
    ))
    quotes = {
        "OK": {
            "kalshi": _pp_ns(no_ask=0.495, age_seconds=2.0),
        },
    }
    rec = StrandedPositionReconciler(
        config=SimpleNamespace(), engine=None,
        adapters={"kalshi": adapter, "polymarket": MagicMock()},
        price_store=_FakePriceStore(quotes), auto_close=True,
    )
    decision = {
        "complete_arb": {
            "venue": "kalshi", "market_id": "KX-OK-N",
            "side": "no", "price": 0.49, "qty": 10, "canonical_id": "OK",
        },
    }
    await rec._execute_complete_arb(pos, decision)
    assert adapter.place_fok.await_count == 1


async def test_execute_complete_arb_flips_to_hold_when_gap_is_hopeless():
    """When combined cost (paid + live ask) exceeds $1 by >=15%, mark the
    position HOLD_TO_SETTLE so we stop reattempting."""
    pos = _stub_pos(
        platform="polymarket", market_id="POLY-HOP-Y",
        qty=10, cost_basis_usd=5.0,   # 0.50/sh
        best_bid=0.49, best_ask=0.51,
    )
    adapter = MagicMock()
    adapter.place_fok = AsyncMock()
    # 0.50 cost + 0.70 ask = 1.20 → 20¢ over $1 → hopeless
    quotes = {"H": {"kalshi": _pp_ns(no_ask=0.70, age_seconds=2.0)}}
    rec = StrandedPositionReconciler(
        config=SimpleNamespace(), engine=None,
        adapters={"kalshi": adapter, "polymarket": MagicMock()},
        price_store=_FakePriceStore(quotes), auto_close=True,
    )
    decision = {
        "complete_arb": {
            "venue": "kalshi", "market_id": "KX-HOP-N",
            "side": "no", "price": 0.48, "qty": 10, "canonical_id": "H",
        },
    }
    await rec._execute_complete_arb(pos, decision)
    assert adapter.place_fok.await_count == 0
    assert pos.mitigation_action == "hold_to_settle"


# ─── Paired-inventory awareness (2026-07-10) ──────────────────────────────
#
# Live failure this guards: 100 filled arbs produced perfectly-hedged venue
# lots (57 kalshi YES <-> 57 FX NO, 43 FX YES <-> 43 kalshi NO) that the
# pairing-blind reconciler tracked as "$75 stranded exposure / -$74
# unrealized" and — during an FX outage — recommended COMPLETE_ARB for an
# already-paired Kalshi leg (buying the FX leg AGAIN = doubled exposure).


def _paired_store(mapping):
    store = MagicMock()
    store.paired_inventory_by_market = AsyncMock(return_value=mapping)
    return store


async def test_fully_paired_lot_is_not_stranded_and_emits_no_incident():
    engine = MagicMock()
    engine._record_incident = AsyncMock()
    store = _paired_store({("kalshi", "CONTROLS-2026-D", "yes"): 57.0})
    rec = StrandedPositionReconciler(
        config=SimpleNamespace(), engine=engine, store=store,
    )
    rec._fetch_kalshi_positions = AsyncMock(return_value=[
        _stub_pos(market_id="CONTROLS-2026-D", side="YES", qty=57.0),
    ])
    rec._fetch_polymarket_us_positions = AsyncMock(return_value=[])
    rec._fetch_forecastex_positions = AsyncMock(return_value=[])

    snapshot = await rec.run_once()

    pos = rec._tracked[("kalshi", "CONTROLS-2026-D")]
    assert pos.paired_qty == 57.0
    assert pos.mitigation_action == "paired_hold"
    assert snapshot.stranded_count == 0          # hedged inventory != strand
    assert snapshot.paired_count == 1
    assert engine._record_incident.await_count == 0


async def test_partially_paired_lot_strands_only_the_residual():
    """Venue lot 60 with 57 covered by filled arbs -> 3 contracts of true
    exposure. Mitigation must see the RESIDUAL quantity, never the full lot
    (deciding on the full lot is how COMPLETE_ARB doubles the paired legs)."""
    engine = MagicMock()
    engine._record_incident = AsyncMock()
    store = _paired_store({("kalshi", "CONTROLS-2026-D", "yes"): 57.0})
    mit = MagicMock()
    decision = MagicMock()
    decision.to_dict.return_value = {"action": "HOLD_TO_SETTLE", "autonomous_ok": False}
    decision.action = "HOLD_TO_SETTLE"
    mit.decide = AsyncMock(return_value=decision)
    rec = StrandedPositionReconciler(
        config=SimpleNamespace(), engine=engine, store=store,
        mitigation_engine=mit,
    )
    rec._fetch_kalshi_positions = AsyncMock(return_value=[
        _stub_pos(market_id="CONTROLS-2026-D", side="YES", qty=60.0,
                  cost_basis_usd=30.0, mtm_usd=6.0),
    ])
    rec._fetch_polymarket_us_positions = AsyncMock(return_value=[])
    rec._fetch_forecastex_positions = AsyncMock(return_value=[])

    snapshot = await rec.run_once()

    pos = rec._tracked[("kalshi", "CONTROLS-2026-D")]
    assert pos.paired_qty == 57.0
    assert snapshot.stranded_count == 1
    assert snapshot.paired_count == 0
    # Mitigation engine consulted with the residual view only.
    assert mit.decide.await_count == 1
    seen = mit.decide.await_args.args[0]
    assert seen.qty == 3.0
    assert engine._record_incident.await_count == 1


async def test_negative_qty_no_side_lot_nets_against_no_fills():
    """Kalshi NO inventory is a negative signed position; paired 'no' fills
    must net against it (sign-aware)."""
    engine = MagicMock()
    engine._record_incident = AsyncMock()
    store = _paired_store({("kalshi", "CONTROLS-2026-R", "no"): 43.0})
    rec = StrandedPositionReconciler(
        config=SimpleNamespace(), engine=engine, store=store,
    )
    rec._fetch_kalshi_positions = AsyncMock(return_value=[
        _stub_pos(market_id="CONTROLS-2026-R", side="NO", qty=-43.0),
    ])
    rec._fetch_polymarket_us_positions = AsyncMock(return_value=[])
    rec._fetch_forecastex_positions = AsyncMock(return_value=[])

    snapshot = await rec.run_once()

    pos = rec._tracked[("kalshi", "CONTROLS-2026-R")]
    assert pos.paired_qty == 43.0
    assert snapshot.stranded_count == 0
    assert snapshot.paired_count == 1


async def test_forecastex_conid_lot_nets_regardless_of_arb_side_label():
    """FX venue lots are per-conid longs; the arb-semantic side ('no') lives
    on the ORDER while the venue position is simply long that conid."""
    engine = MagicMock()
    engine._record_incident = AsyncMock()
    store = _paired_store({("forecastex", "745924270", "no"): 57.0})
    rec = StrandedPositionReconciler(
        config=SimpleNamespace(), engine=engine, store=store,
    )
    rec._fetch_kalshi_positions = AsyncMock(return_value=[])
    rec._fetch_polymarket_us_positions = AsyncMock(return_value=[])
    rec._fetch_forecastex_positions = AsyncMock(return_value=[
        _stub_pos(platform="forecastex", market_id="745924270",
                  side="YES", qty=57.0),
    ])

    snapshot = await rec.run_once()

    pos = rec._tracked[("forecastex", "745924270")]
    assert pos.paired_qty == 57.0
    assert snapshot.stranded_count == 0


async def test_store_failure_falls_back_to_no_netting():
    """Pairing info unavailable -> fail SAFE by over-reporting strands
    (today's behavior), never by hiding exposure."""
    engine = MagicMock()
    engine._record_incident = AsyncMock()
    store = MagicMock()
    store.paired_inventory_by_market = AsyncMock(side_effect=RuntimeError("db down"))
    rec = StrandedPositionReconciler(
        config=SimpleNamespace(), engine=engine, store=store,
    )
    rec._fetch_kalshi_positions = AsyncMock(return_value=[
        _stub_pos(market_id="CONTROLS-2026-D", side="YES", qty=57.0),
    ])
    rec._fetch_polymarket_us_positions = AsyncMock(return_value=[])
    rec._fetch_forecastex_positions = AsyncMock(return_value=[])

    snapshot = await rec.run_once()

    assert snapshot.stranded_count == 1
    assert engine._record_incident.await_count == 1


async def test_fetch_failure_defers_mitigation_decisions_that_cycle():
    """A failed venue positions-fetch means the cross-venue view is
    incomplete — mitigation decisions computed from it are untrustworthy
    (live 2026-07-10 drill: FX fetch failure -> COMPLETE_ARB recommended for
    an already-paired leg). Defer decisions; keep visibility."""
    engine = MagicMock()
    engine._record_incident = AsyncMock()
    mit = MagicMock()
    decision = MagicMock()
    decision.to_dict.return_value = {"action": "HOLD_TO_SETTLE", "autonomous_ok": False}
    decision.action = "HOLD_TO_SETTLE"
    mit.decide = AsyncMock(return_value=decision)
    rec = StrandedPositionReconciler(
        config=SimpleNamespace(), engine=engine, mitigation_engine=mit,
    )
    rec._fetch_kalshi_positions = AsyncMock(return_value=[
        _stub_pos(market_id="K-NEW", qty=5.0),
    ])
    rec._fetch_polymarket_us_positions = AsyncMock(return_value=[])
    rec._fetch_forecastex_positions = AsyncMock(side_effect=RuntimeError("gw down"))

    await rec.run_once()
    # Incomplete view: no mitigation decision this cycle, but the lot IS
    # tracked and the operator IS alerted (visibility preserved).
    assert mit.decide.await_count == 0
    assert engine._record_incident.await_count == 1

    rec._fetch_forecastex_positions = AsyncMock(return_value=[])
    await rec.run_once()
    # Healthy cycle: decision computed.
    assert mit.decide.await_count == 1


# ─── ForecastEx position identification (2026-07-10) ───────────────────────
#
# Live bug: the IBKR positions client returns records with exchs=null and NO
# listingExchange, and contractDesc reads "SENM NOV2026 2 C
# [SENM_1126_Democratic_YES 1]" — which contains no "FORECASTX" substring.
# The old filter ("FORECASTX" in exchange/desc) dropped EVERY FX position, so
# the reconciler tracked zero FX lots (paired count stuck at 1 = the kalshi
# leg only), leaving FX-side exposure invisible.

from arbiter.recovery.stranded_reconciler import is_forecastex_position


def test_identifies_fx_event_contract_by_bracketed_local_symbol():
    # The exact live record shape (exchs=null, no listingExchange).
    assert is_forecastex_position({
        "conid": 773659815,
        "contractDesc": "SENM   NOV2026 2 C [SENM_1126_Democratic_YES 1]",
        "assetClass": "OPT",
        "exchs": None,
    })
    assert is_forecastex_position({
        "conid": 745924270,
        "contractDesc": "SENM   NOV2026 1 P [SENM_1126_Republican_NO 1]",
        "assetClass": "OPT",
    })


def test_still_identifies_fx_by_explicit_exchange_marker():
    assert is_forecastex_position({
        "listingExchange": "FORECASTX",
        "contractDesc": "whatever",
    })
    assert is_forecastex_position({"exchs": "FORECASTX", "contractDesc": ""})


def test_rejects_non_forecastex_holdings():
    # An equity/option the user might also hold — no FX exchange, no
    # event-contract bracket pattern.
    assert not is_forecastex_position({
        "conid": 265598,
        "contractDesc": "AAPL",
        "listingExchange": "NASDAQ",
        "assetClass": "STK",
    })
    assert not is_forecastex_position({
        "contractDesc": "SPY   DEC2026 500 C",
        "assetClass": "OPT",
        "listingExchange": "CBOE",
    })


def test_rejects_empty_or_malformed():
    assert not is_forecastex_position({})
    assert not is_forecastex_position({"contractDesc": ""})


async def test_fetch_forecastex_positions_parses_live_record_shape():
    """End-to-end fetch of the exact live IBKR record (exchs=null, no
    listingExchange) must yield a StrandedPosition — regression for the
    NameError that removing the `desc` local introduced, which made the
    fetch raise every cycle and leave FX exposure untracked."""
    client = MagicMock()
    client.account_id = "U25953084"
    client.positions = AsyncMock(return_value=[
        {
            "conid": 773659815,
            "contractDesc": "SENM   NOV2026 2 C [SENM_1126_Democratic_YES 1]",
            "position": 51.0,
            "avgCost": 0.4798,
            "mktPrice": 0.46,
            "exchs": None,
            "assetClass": "OPT",
        },
        {"conid": 265598, "contractDesc": "AAPL", "listingExchange": "NASDAQ",
         "assetClass": "STK", "position": 10.0},
    ])
    client.market_snapshot = AsyncMock(return_value={})
    rec = StrandedPositionReconciler(
        config=SimpleNamespace(), forecastex_client=client,
    )
    out = await rec._fetch_forecastex_positions()
    assert len(out) == 1
    assert out[0].platform == "forecastex"
    assert out[0].market_id == "773659815"
    assert out[0].qty == 51.0
    assert "Democratic_YES" in out[0].title
