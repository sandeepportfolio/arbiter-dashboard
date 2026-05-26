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
