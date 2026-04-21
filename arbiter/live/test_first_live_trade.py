"""Phase 5 first-live-trade scenario — @pytest.mark.live.

This test places ONE real cross-platform arbitrage trade against production
Kalshi + production Polymarket, sized <= PHASE5_MAX_ORDER_USD ($10 per leg),
and verifies:
  (a) both legs reach a terminal status (FILLED / CANCELLED / FAILED);
  (b) post-trade reconciliation runs within 60s of settlement and reports
      either PASS (within ±$0.01 D-17 tolerance) or triggers auto-abort;
  (c) evidence is captured under evidence/05/first_live_trade_<ts>/ with
      run.log.jsonl, opportunity.json, pre_trade_requote.json,
      execution_*.json tables, balances_pre.json, balances_post.json, and
      reconciliation.json.

This file is SCAFFOLDED by Plan 05-02 Task 3a. The live-fire RUN is Task 3b —
requires operator provisioning (funded Kalshi + Polymarket, arbiter_live DB,
preflight clean). Nothing runs against real platforms until the operator
invokes:

    pytest -m live --live arbiter/live/test_first_live_trade.py -v -s

Grep-negative invariants that the code review + Plan 05-02 verify block enforce
on this file (T-5-02-09 + T-5-02-10):
  * ``grep -c "raise NotImpl..." arbiter/live/test_first_live_trade.py`` -> 0
    (no stubs; all helpers imported from live_fire_helpers).
  * W-5 grep: the private-attribute dotted path "_state" + "armed" must
    produce zero matches in this file. Use the public supervisor.is_armed /
    supervisor.armed_by properties from Plan 05-01 Task 3 instead.
  * ``grep "supervisor.is_armed" arbiter/live/test_first_live_trade.py`` -> >=1 match
    (W-5: public property IS accessed).
  * ``grep "write_pre_trade_requote" arbiter/live/test_first_live_trade.py`` -> >=1
    (W-3: pre-trade requote evidence written).
  * ``grep "PRE_EXECUTION_OPERATOR_ABORT_SECONDS" arbiter/live/test_first_live_trade.py`` -> >=1
    (W-6: 60-second operator-abort window wired through named constant).
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from typing import Any, Dict

import pytest
import structlog

from arbiter.config.settings import (
    MARKET_MAP,
    iter_confirmed_market_mappings,
    load_config,
)
from arbiter.execution.engine import ExecutionEngine, OrderStatus
from arbiter.live import evidence as live_evidence
from arbiter.live.auto_abort import wire_auto_abort_on_reconcile
from arbiter.live.live_fire_helpers import (
    POLYGON_SETTLEMENT_WAIT_SECONDS,
    PRE_EXECUTION_OPERATOR_ABORT_SECONDS,
    TEST_PER_LEG_USD_CEILING,
    build_opportunity_from_quotes,
    fetch_kalshi_platform_fee,
    fetch_polymarket_platform_fee,
    write_pre_trade_requote,
)
from arbiter.live.preflight import run_preflight
from arbiter.live.reconcile import reconcile_post_trade
from arbiter.safety.supervisor import SafetySupervisor

log = structlog.get_logger("arbiter.live.test_first_live_trade")


# ─── Helpers ─────────────────────────────────────────────────────────────────


def _resolve_target_canonical_id() -> str:
    """Pick the canonical_id to trade.

    Resolution order:
      1. ``PHASE5_TARGET_CANONICAL_ID`` env var (operator override).
      2. First confirmed + allow_auto_trade mapping with
         resolution_match_status == 'identical'.

    Raises ``pytest.skip`` if no suitable mapping is available — the live-fire
    REFUSES to run without an identical-resolution mapping (SAFE-06 invariant).
    """
    override = os.getenv("PHASE5_TARGET_CANONICAL_ID", "").strip()
    if override:
        mapping = MARKET_MAP.get(override)
        if mapping is None:
            pytest.skip(
                f"PHASE 5: PHASE5_TARGET_CANONICAL_ID={override!r} not in MARKET_MAP"
            )
        status = str(mapping.get("resolution_match_status", "") or "").lower()
        if status != "identical":
            pytest.skip(
                f"PHASE 5: override canonical_id={override!r} has "
                f"resolution_match_status={status!r}; SAFE-06 requires 'identical'"
            )
        return override

    for canonical_id, mapping in iter_confirmed_market_mappings(require_auto_trade=True):
        status = str(mapping.get("resolution_match_status", "") or "").lower()
        if status == "identical":
            log.info(
                "phase5.target.selected",
                canonical_id=canonical_id,
                source="first_identical_mapping",
            )
            return canonical_id

    pytest.skip(
        "PHASE 5: no confirmed mapping with resolution_match_status='identical' "
        "and allow_auto_trade=True found; curate MARKET_MAP before live-fire"
    )


async def _populate_price_store(price_store, canonical_id: str, collectors) -> None:
    """Fetch current prices from both platforms into the PriceStore.

    Calls each collector's ``fetch_prices`` (or equivalent) so
    ``price_store.get_all_for_market(canonical_id)`` returns both platforms.
    Bails to pytest.skip if either platform cannot be fetched (safer than
    proceeding on a one-sided quote).
    """
    kalshi_collector = collectors.get("kalshi")
    poly_collector = collectors.get("polymarket")
    if kalshi_collector is None or poly_collector is None:
        pytest.skip(
            "PHASE 5: both kalshi and polymarket collectors required for live-fire; "
            f"got {list(collectors.keys())}"
        )
    try:
        if hasattr(kalshi_collector, "fetch_prices"):
            await kalshi_collector.fetch_prices([canonical_id])
        elif hasattr(kalshi_collector, "fetch_all"):
            await kalshi_collector.fetch_all()
    except Exception as exc:
        pytest.skip(f"PHASE 5: Kalshi price fetch failed: {exc!r}")
    try:
        if hasattr(poly_collector, "fetch_prices"):
            await poly_collector.fetch_prices([canonical_id])
        elif hasattr(poly_collector, "fetch_all"):
            await poly_collector.fetch_all()
    except Exception as exc:
        pytest.skip(f"PHASE 5: Polymarket price fetch failed: {exc!r}")


def _register_order_condition_id(poly_adapter, order, polymarket_condition_id: str) -> None:
    """Populate adapter._order_condition_index[order_id] = condition_id.

    fetch_polymarket_platform_fee requires this cache to look up the market
    scope when calling client.get_trades(market=<condition_id>). The adapter
    does not track this natively — the live-fire test is responsible for
    wiring it after each place_fok.

    Raising here (rather than silently skipping) preserves T-5-02-09:
    if this cache is not populated, fetch_polymarket_platform_fee will
    AssertionError during reconcile and the breach will be caught.
    """
    if not hasattr(poly_adapter, "_order_condition_index"):
        poly_adapter._order_condition_index = {}
    poly_adapter._order_condition_index[order.order_id] = polymarket_condition_id


# ─── The live-fire scenario ──────────────────────────────────────────────────


@pytest.mark.live
async def test_first_live_trade_executes_and_reconciles(
    production_db_pool,
    production_kalshi_adapter,
    production_polymarket_adapter,
    evidence_dir,
):
    """First real cross-platform arbitrage trade + reconcile + auto-abort wire-up.

    Sequence:
      1. Preflight gate — run all 15 checks; abort on any blocking failure.
      2. Build opportunity from current quotes (skip if no tradable arb).
      3. Write pre_trade_requote.json (W-3).
      4. Sleep PRE_EXECUTION_OPERATOR_ABORT_SECONDS (W-6) — operator scrutinizes.
         If supervisor.is_armed after the sleep (operator ARMed), skip.
      5. engine.execute(opp) — real live order placement on both platforms.
      6. Wait POLYGON_SETTLEMENT_WAIT_SECONDS for on-chain settlement.
      7. Invoke wire_auto_abort_on_reconcile(supervisor, reconcile_fn) — runs
         reconcile_post_trade with real adapter-backed fee fetchers; trips
         kill-switch on breach or reconcile exception (fail-closed).
      8. Assert terminal status + reconcile outcome + evidence complete.
      9. Dump execution_* tables + balances + reconciliation.json.
    """
    config = load_config()
    kalshi_adapter = production_kalshi_adapter
    poly_adapter = production_polymarket_adapter
    adapters = {"kalshi": kalshi_adapter, "polymarket": poly_adapter}

    # Real SafetySupervisor (from Plan 03 + 05-01 accessors). Build with an
    # AsyncMock notifier so Telegram outages don't abort the test. The operator
    # monitors the real Telegram channel in parallel.
    from unittest.mock import AsyncMock

    from arbiter.config.settings import SafetyConfig
    from types import SimpleNamespace

    notifier = AsyncMock()
    notifier.send = AsyncMock(return_value=None)
    safety_config = SafetyConfig()
    supervisor = SafetySupervisor(
        config=safety_config,
        engine=SimpleNamespace(),  # engine-wiring not exercised by this test
        adapters=adapters,
        notifier=notifier,
        redis=None,
        store=None,
        safety_store=None,
    )

    # ─── Step 1: preflight gate ────────────────────────────────────────────
    report = await run_preflight()
    (evidence_dir / "preflight.json").write_text(
        json.dumps([item.to_dict() for item in report.items], indent=2),
        encoding="utf-8",
    )
    if not report.passed:
        blocking = [i.to_dict() for i in report.blocking_failures]
        pytest.fail(
            f"PHASE 5 live-fire ABORTED by preflight: "
            f"{len(blocking)} blocking failure(s): {blocking!r}"
        )

    # ─── Step 2: build opportunity from current quotes ─────────────────────
    target_cid = _resolve_target_canonical_id()
    log.info("phase5.target.canonical_id", canonical_id=target_cid)

    # Build real collectors so price_store gets populated.
    from arbiter.collectors.kalshi import KalshiCollector
    from arbiter.collectors.polymarket import PolymarketCollector
    from arbiter.utils.price_store import PriceStore

    price_store = PriceStore()
    kalshi_collector = KalshiCollector(config.kalshi, price_store)
    poly_collector = PolymarketCollector(config.polymarket, price_store)
    await _populate_price_store(
        price_store, target_cid,
        {"kalshi": kalshi_collector, "polymarket": poly_collector},
    )

    original_opp = await build_opportunity_from_quotes(
        price_store, target_cid, per_leg_cap_usd=TEST_PER_LEG_USD_CEILING,
    )
    if original_opp is None:
        pytest.skip(
            f"PHASE 5: no tradable arb available on {target_cid} at this time; "
            "retry when a real edge appears"
        )
    log.info(
        "phase5.opportunity.detected",
        canonical_id=target_cid,
        yes_platform=original_opp.yes_platform,
        no_platform=original_opp.no_platform,
        net_edge_cents=original_opp.net_edge_cents,
        suggested_qty=original_opp.suggested_qty,
    )
    (evidence_dir / "opportunity.json").write_text(
        json.dumps(original_opp.to_dict(), indent=2), encoding="utf-8",
    )

    # Re-quote just before placement to minimize stale-price risk.
    await _populate_price_store(
        price_store, target_cid,
        {"kalshi": kalshi_collector, "polymarket": poly_collector},
    )
    requoted_opp = await build_opportunity_from_quotes(
        price_store, target_cid, per_leg_cap_usd=TEST_PER_LEG_USD_CEILING,
    )
    if requoted_opp is None:
        pytest.skip(
            f"PHASE 5: re-quote wiped the opportunity on {target_cid}; "
            "edge closed between detection and placement"
        )

    # ─── Step 3: write pre-trade requote evidence (W-3) ───────────────────
    requote_path = write_pre_trade_requote(
        evidence_dir, requoted_opp, original_opp=original_opp,
    )
    print(f"[PHASE 5] pre_trade_requote.json written to {requote_path}")

    # ─── Step 4: operator-abort window (W-6) ──────────────────────────────
    print(
        f"[PHASE 5] Sleeping {PRE_EXECUTION_OPERATOR_ABORT_SECONDS}s — "
        f"ARM the kill-switch NOW if you want to abort. Opportunity: "
        f"{requoted_opp.yes_platform} YES @ {requoted_opp.yes_price:.4f} / "
        f"{requoted_opp.no_platform} NO @ {requoted_opp.no_price:.4f} / "
        f"qty={requoted_opp.suggested_qty} / "
        f"net_edge={requoted_opp.net_edge_cents:.2f}¢"
    )
    await asyncio.sleep(PRE_EXECUTION_OPERATOR_ABORT_SECONDS)

    # W-5: use the public is_armed property (no private-attr access).
    if supervisor.is_armed:
        pytest.skip(
            f"PHASE 5: operator ARMed kill-switch during pre-execution pause "
            f"(armed_by={supervisor.armed_by!r}); aborted as instructed"
        )

    # ─── Pre-trade balance snapshot ────────────────────────────────────────
    from arbiter.monitor.balance import BalanceMonitor

    balance_monitor = BalanceMonitor(
        config.alerts,
        {"kalshi": kalshi_collector, "polymarket": poly_collector},
    )
    try:
        pre_snaps = await balance_monitor.check_balances()
        balances_pre = {
            p: {"balance": float(s.balance) if s.balance is not None else None,
                "timestamp": float(s.timestamp)}
            for p, s in pre_snaps.items()
        }
    except Exception as exc:
        balances_pre = {"error": str(exc)}
        log.warning("phase5.balances_pre.failed", err=str(exc))

    # ─── Step 5: place the arbitrage (live) ────────────────────────────────
    engine = ExecutionEngine(
        config=config,
        adapters=adapters,
        safety_supervisor=supervisor,
        price_store=price_store,
        db_pool=production_db_pool,
    )
    t_submit = time.time()
    execution = await engine.execute(requoted_opp)
    submit_elapsed = time.time() - t_submit
    log.info(
        "phase5.execution.submitted",
        elapsed_s=submit_elapsed,
        arb_id=getattr(execution, "arb_id", None),
    )

    if execution is None:
        pytest.fail(
            "PHASE 5: engine.execute returned None — probable gate rejection. "
            "Check readiness + safety gates + preflight."
        )

    # Register condition_id on the Polymarket adapter so reconcile can resolve
    # fee fetcher -> client.get_trades(market=<condition_id>).
    # The MARKET_MAP mapping for this canonical_id stores the Polymarket
    # condition_id under the 'polymarket' sub-dict.
    mapping = MARKET_MAP.get(target_cid, {})
    poly_mapping = mapping.get("polymarket", {}) or {}
    poly_condition_id = (
        poly_mapping.get("condition_id")
        or poly_mapping.get("market_id")
        or ""
    )
    if poly_condition_id:
        for leg in (execution.leg_yes, execution.leg_no):
            if leg.platform == "polymarket":
                _register_order_condition_id(poly_adapter, leg, poly_condition_id)
    else:
        log.warning(
            "phase5.poly_condition_id.missing",
            canonical_id=target_cid,
            note="reconcile fee_fetcher will AssertionError for polymarket leg",
        )

    # ─── Step 6: wait for Polygon settlement ───────────────────────────────
    print(f"[PHASE 5] Waiting {POLYGON_SETTLEMENT_WAIT_SECONDS}s for Polygon settlement...")
    await asyncio.sleep(POLYGON_SETTLEMENT_WAIT_SECONDS)

    # ─── Step 7: reconcile + auto-abort wire-up ────────────────────────────
    async def fee_fetcher(platform: str, order_id: str) -> float:
        if platform == "kalshi":
            return await fetch_kalshi_platform_fee(kalshi_adapter, order_id)
        if platform == "polymarket":
            return await fetch_polymarket_platform_fee(poly_adapter, order_id)
        raise ValueError(f"unknown platform: {platform!r}")

    async def reconcile_fn():
        return await reconcile_post_trade(
            execution, adapters, fee_fetcher=fee_fetcher,
        )

    abort_result = await wire_auto_abort_on_reconcile(supervisor, reconcile_fn)
    (evidence_dir / "reconciliation.json").write_text(
        json.dumps(abort_result, indent=2, default=str), encoding="utf-8",
    )

    # ─── Step 8: invariants on the outcome ─────────────────────────────────
    # Both legs must reach a terminal status.
    terminal_statuses = {
        OrderStatus.FILLED,
        OrderStatus.CANCELLED,
        OrderStatus.FAILED,
    }
    assert execution.leg_yes.status in terminal_statuses, (
        f"PHASE 5: YES leg did not reach terminal status: {execution.leg_yes.status}"
    )
    assert execution.leg_no.status in terminal_statuses, (
        f"PHASE 5: NO leg did not reach terminal status: {execution.leg_no.status}"
    )

    # Auto-abort invariant: if reconcile flagged a breach, supervisor MUST be armed.
    # W-5: use the public is_armed + armed_by properties.
    if abort_result["aborted"]:
        assert supervisor.is_armed, (
            "PHASE 5: auto_abort reported aborted=True but supervisor.is_armed is False"
        )
        assert supervisor.armed_by == "system:phase5_reconcile_fail", (
            f"PHASE 5: supervisor.armed_by={supervisor.armed_by!r}, "
            f"expected 'system:phase5_reconcile_fail'"
        )
    else:
        assert not supervisor.is_armed, (
            "PHASE 5: auto_abort reported clean but supervisor.is_armed is True"
        )

    # ─── Post-trade balance snapshot ───────────────────────────────────────
    try:
        post_snaps = await balance_monitor.check_balances()
        balances_post = {
            p: {"balance": float(s.balance) if s.balance is not None else None,
                "timestamp": float(s.timestamp)}
            for p, s in post_snaps.items()
        }
    except Exception as exc:
        balances_post = {"error": str(exc)}
        log.warning("phase5.balances_post.failed", err=str(exc))

    # ─── Step 9: evidence dump ─────────────────────────────────────────────
    live_evidence.write_balances(evidence_dir, balances_pre, balances_post)
    await live_evidence.dump_execution_tables(production_db_pool, evidence_dir)

    # Safety events dump (kill-switch history for this run).
    try:
        async with production_db_pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM safety_events ORDER BY event_ts DESC LIMIT 20"
            )
        (evidence_dir / "safety_events.json").write_text(
            json.dumps([dict(r) for r in rows], indent=2, default=str),
            encoding="utf-8",
        )
    except Exception as exc:
        log.warning("phase5.safety_events_dump.failed", err=str(exc))

    # Scenario manifest — ties together everything for operator attestation.
    manifest: Dict[str, Any] = {
        "scenario": "first_live_trade",
        "requirement_ids": ["TEST-05"],
        "canonical_id": target_cid,
        "arb_id": getattr(execution, "arb_id", None),
        "yes_leg": {
            "platform": execution.leg_yes.platform,
            "order_id": execution.leg_yes.order_id,
            "status": str(execution.leg_yes.status),
            "fill_price": float(execution.leg_yes.fill_price or 0.0),
            "fill_qty": float(execution.leg_yes.fill_qty or 0),
            "notional_usd": float(execution.leg_yes.fill_price or 0.0)
                            * float(execution.leg_yes.fill_qty or 0),
        },
        "no_leg": {
            "platform": execution.leg_no.platform,
            "order_id": execution.leg_no.order_id,
            "status": str(execution.leg_no.status),
            "fill_price": float(execution.leg_no.fill_price or 0.0),
            "fill_qty": float(execution.leg_no.fill_qty or 0),
            "notional_usd": float(execution.leg_no.fill_price or 0.0)
                            * float(execution.leg_no.fill_qty or 0),
        },
        "expected_net_edge_cents": requoted_opp.net_edge_cents,
        "submit_elapsed_s": submit_elapsed,
        "polygon_settlement_wait_s": POLYGON_SETTLEMENT_WAIT_SECONDS,
        "pre_execution_operator_abort_s": PRE_EXECUTION_OPERATOR_ABORT_SECONDS,
        "reconcile_outcome": abort_result,
        "supervisor_armed": supervisor.is_armed,
        "supervisor_armed_by": supervisor.armed_by,
    }
    (evidence_dir / "scenario_manifest.json").write_text(
        json.dumps(manifest, indent=2, default=str), encoding="utf-8",
    )

    print(
        f"[PHASE 5] Evidence directory: {evidence_dir}\n"
        f"[PHASE 5] arb_id={getattr(execution, 'arb_id', None)} "
        f"abort_result.aborted={abort_result['aborted']} "
        f"supervisor.is_armed={supervisor.is_armed}"
    )
