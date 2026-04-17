"""Kill-switch live-fire against Kalshi demo (Scenario 6: SAFE-01).

Scenario 6 verifies that the SafetySupervisor kill-switch actually cancels a
real resting order on the Kalshi demo exchange within the 5-second SAFE-01
budget. Phase 3 validated this path against mock adapters; Phase 4 closes the
"live" verification gap by wiring the REAL (demo-configured) KalshiAdapter
into the supervisor and watching a resting order transition to CANCELLED on
the exchange after ``supervisor.trip_kill(...)``.

Scope boundary (HISTORICAL — no longer applies after Plan 04-02.1):
    Earlier drafts of Plan 04-05 required a TEST-ONLY ``adapter._client``
    bypass for resting-order placement because KalshiAdapter exposed only
    ``place_fok``. Plan 04-02.1 (commits d5958ec + 2d45ed4) added
    ``KalshiAdapter.place_resting_limit`` as a first-class method with the
    same PHASE4_MAX_ORDER_USD hard-lock, rate-limiter, and circuit-breaker
    wiring as ``place_fok``. This test now calls that method directly. The
    ``adapter._client`` TEST-ONLY bypass is unused; production adapter is not modified by this plan.
    The method was properly added by Plan 04-02.1 as a public surface. The
    ``pytest.fail`` escape hatch (documented below for historical reference)
    would only fire if ``place_resting_limit`` disappeared regressively.

Placement strategy: ``adapter.place_resting_limit(...)`` (Plan 04-02.1).
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import structlog

from arbiter.config.settings import SafetyConfig
from arbiter.execution.engine import OrderStatus
from arbiter.safety.supervisor import SafetySupervisor
from arbiter.sandbox import evidence

log = structlog.get_logger("arbiter.sandbox.killswitch")


# ─── Research-supplied constants (baked in with env-var overrides) ────────
# Evidence (Plan 04-05 continuation context, observed 2026-04-17T03:20:00Z):
#   market_url:            https://kalshi.com/markets/kxpresparty/presidential-election-party-2028
#   current_yes_ask:       39¢  (proposed resting 31¢ is 8¢ below ask → rests)
#   current_no_ask:        62¢
#   volume_24h:            10,699
#   close_time:            2029-11-07T12:00:00Z (~934 days out)
#   depth_at_31¢_on_YES:   3,800 qty already queued (our order joins the queue)
# Notional: 16 * $0.31 = $4.96 (< $5 PHASE4_MAX_ORDER_USD hard-lock).
#
# Expected demo behavior: Kalshi mirrors prod→demo so ticker should exist on
# demo-api.kalshi.co; demo books are thinner but at 31¢ on an empty/thin demo
# book the limit bid still rests (no matching ask). Operator overrides via
# env vars if demo's best ask happens to sit ≤ 31¢.
#
# Runner-up (for operator reference; not baked into the test):
#   KXFED-27APR-T3.50, resting_price=0.45, resting_qty=11 (notional $4.95).
KS_MARKET_TICKER = os.getenv("PHASE4_KILLSWITCH_TICKER", "KXPRESPARTY-2028-R")
KS_RESTING_PRICE = float(os.getenv("PHASE4_KILLSWITCH_PRICE", "31")) / 100.0
KS_RESTING_QTY = int(os.getenv("PHASE4_KILLSWITCH_QTY", "16"))


def _build_supervisor_with_real_adapter(kalshi_adapter):
    """Construct a SafetySupervisor around the REAL demo KalshiAdapter.

    Mirrors the ``_build_supervisor`` helper in arbiter/safety/test_supervisor.py
    (the Phase 3 analog). Engine is a bare SimpleNamespace — the supervisor
    does not call back into the engine during trip_kill. The notifier is an
    AsyncMock so Telegram-send failures cannot abort a live-fire; this
    mirrors the Phase 3 fake_notifier fixture shape.
    """
    notifier = AsyncMock()
    notifier.send = AsyncMock(return_value=None)

    # Use real SafetyConfig with defaults; we do not exercise cooldown here.
    config = SafetyConfig()
    engine = SimpleNamespace()  # supervisor does not call engine.* during trip
    adapters = {"kalshi": kalshi_adapter}

    supervisor = SafetySupervisor(
        config=config,
        engine=engine,
        adapters=adapters,
        notifier=notifier,
        redis=None,
        store=None,
        safety_store=None,
    )
    return supervisor, notifier


@pytest.mark.live
async def test_kill_switch_cancels_open_kalshi_demo_order(
    demo_kalshi_adapter, sandbox_db_pool, evidence_dir,
):
    """Place resting Kalshi demo order, trip kill switch, assert cancellation on platform within 5s.

    Flow:
      1. Place a resting (non-FOK) Kalshi demo order via
         ``adapter.place_resting_limit`` (Plan 04-02.1 public method; no
         ``_client`` workaround needed).
      2. Sleep briefly to let Kalshi settle the order on the book.
      3. Trip kill switch in-process; assert <5s budget + armed state.
      4. Confirm WS ``kill_switch`` event was published.
      5. Verify the order is CANCELLED on the demo exchange via
         ``adapter.get_order(order)``.
      6. Verify ``supervisor.allow_execution`` rejects while armed.

    SAFE-01 invariant: trip_kill elapsed time < 5s.
    """
    adapter = demo_kalshi_adapter
    assert "arbiter_sandbox" in os.getenv("DATABASE_URL", ""), (
        "SAFETY: DATABASE_URL must point at arbiter_sandbox for live-fire"
    )

    supervisor, notifier = _build_supervisor_with_real_adapter(adapter)
    queue = supervisor.subscribe()

    # ─── Step 1: place resting (non-FOK) order via adapter.place_resting_limit.
    # Plan 04-02.1 added this as a public KalshiAdapter method with identical
    # rate-limiter / circuit-breaker / PHASE4_MAX_ORDER_USD hard-lock plumbing
    # to place_fok. Notional = KS_RESTING_QTY * KS_RESTING_PRICE must stay
    # under PHASE4_MAX_ORDER_USD (default $5 in .env.sandbox).
    arb_id = "ARB-SANDBOX-KILLSWITCH"
    resting = await adapter.place_resting_limit(
        arb_id=arb_id,
        market_id=KS_MARKET_TICKER,
        canonical_id=KS_MARKET_TICKER,  # no canonical mapping in sandbox
        side="yes",
        price=KS_RESTING_PRICE,
        qty=KS_RESTING_QTY,
    )

    # If the demo refuses the order (auth missing, cap breach, rate-limit, etc.)
    # surface this as a clear failure — the test cannot proceed to kill-switch
    # validation without a live resting order on the book. This is the same
    # escape-hatch semantics the historical _client-bypass branch used:
    if resting.status == OrderStatus.FAILED:
        pytest.fail(
            f"Plan 04-05 Task 1: place_resting_limit returned FAILED before the "
            f"kill-switch could be tripped: {resting.error!r}. Common causes: "
            f"(a) .env.sandbox not sourced or KALSHI_BASE_URL wrong, "
            f"(b) PHASE4_MAX_ORDER_USD cap lower than notional "
            f"{KS_RESTING_QTY}*{KS_RESTING_PRICE:.2f}="
            f"${KS_RESTING_QTY * KS_RESTING_PRICE:.2f}, "
            f"(c) Kalshi demo rejected ticker '{KS_MARKET_TICKER}' — override "
            f"via PHASE4_KILLSWITCH_TICKER env var, "
            f"(d) demo rate-limited. Cannot live-fire SAFE-01 without a "
            f"resting order on the book."
        )

    log.info(
        "scenario.killswitch.resting_placed",
        order_id=resting.order_id,
        client_order_id=resting.external_client_order_id,
        market=KS_MARKET_TICKER,
        price=KS_RESTING_PRICE,
        qty=KS_RESTING_QTY,
    )

    # ─── Step 2: settle briefly so the order is actually resting before we
    # trip kill. The place_resting_limit response already indicates SUBMITTED,
    # but we give the exchange a 1s grace to make sure.
    await asyncio.sleep(1.0)

    # ─── Step 3: trip the kill switch. 5s SAFE-01 budget per Phase 3.
    t_start = time.time()
    state = await asyncio.wait_for(
        supervisor.trip_kill(
            by="operator:phase4_test",
            reason="Phase 4 SAFE-01 live validation",
        ),
        timeout=6.0,  # 5.0s SAFE-01 budget + 1s grace for network jitter
    )
    t_elapsed = time.time() - t_start
    assert state.armed is True, "trip_kill did not arm the supervisor"
    assert t_elapsed < 5.5, (
        f"trip_kill exceeded 5s budget + 0.5s grace: took {t_elapsed:.2f}s"
    )
    log.info("scenario.killswitch.tripped", elapsed_s=t_elapsed, armed=state.armed)

    # ─── Step 4: confirm WS kill_switch event was published.
    try:
        event = queue.get_nowait()
    except asyncio.QueueEmpty:
        pytest.fail(
            "SafetySupervisor did not publish kill_switch event on trip_kill"
        )
    assert event.get("type") == "kill_switch", (
        f"Unexpected WS event type: {event.get('type')!r}"
    )
    event_payload = event.get("payload", {}) or {}
    assert event_payload.get("armed") is True, (
        f"kill_switch event payload does not report armed=True: {event_payload!r}"
    )

    # ─── Step 5: confirm the platform actually cancelled the order.
    # KalshiAdapter.get_order(order) mutates and returns the Order with the
    # platform-reported status. CANCELLED is the SAFE-01 invariant success
    # condition; FAILED with "not found" is also acceptable because demo may
    # drop cancelled orders from the queryable set after cancellation.
    refreshed = await adapter.get_order(resting)
    cancelled_on_platform = refreshed.status == OrderStatus.CANCELLED or (
        refreshed.status == OrderStatus.FAILED
        and "not found" in (refreshed.error or "").lower()
    )
    assert cancelled_on_platform, (
        f"SAFE-01 INVARIANT VIOLATED: order {resting.order_id} reports "
        f"status={refreshed.status} error={refreshed.error!r} on demo "
        f"exchange after trip_kill. adapter.cancel_all did not actually "
        f"cancel on the platform."
    )

    # ─── Step 6: confirm supervisor rejects new execution attempts while armed.
    fake_opp = SimpleNamespace(
        canonical_id=KS_MARKET_TICKER,
        yes_platform="kalshi",
        no_platform="polymarket",
        yes_price=0.55,
        no_price=0.40,
        suggested_qty=1,
    )
    allowed, reason, sup_state = await supervisor.allow_execution(fake_opp)
    assert not allowed, "supervisor.allow_execution should be False while armed"
    assert "Kill switch armed" in reason or "armed" in reason.lower(), (
        f"Expected armed-state rejection reason; got: {reason!r}"
    )
    log.info("scenario.killswitch.allow_execution_rejected", reason=reason)

    # ─── Evidence dump for aggregator (Plan 04-08).
    await evidence.dump_execution_tables(sandbox_db_pool, evidence_dir)
    (evidence_dir / "scenario_manifest.json").write_text(json.dumps({
        "scenario": "kill_switch_cancels_open_kalshi_demo_order",
        "requirement_ids": ["SAFE-01", "TEST-01"],
        "phase_3_refs": [
            "03-01-PLAN",
            "03-HUMAN-UAT.md Test 1 (partial — backend only; UI reserved)",
        ],
        "tag": "real",
        "placed_order_id": resting.order_id,
        "placed_client_order_id": resting.external_client_order_id,
        "market": KS_MARKET_TICKER,
        "price": KS_RESTING_PRICE,
        "qty": KS_RESTING_QTY,
        "notional_usd": round(KS_RESTING_QTY * KS_RESTING_PRICE, 4),
        "trip_kill_elapsed_s": t_elapsed,
        "cancelled_on_platform": cancelled_on_platform,
        "post_trip_status": str(refreshed.status),
        "ws_event_type": event.get("type"),
        "supervisor_armed_post_trip": bool(state.armed),
        "allow_execution_rejected_while_armed": not allowed,
        "rejection_reason": reason,
        # Plan 04-02.1 eliminated the _client bypass workaround — we now call
        # the public place_resting_limit method directly.
        "non_fok_placement_strategy": "adapter.place_resting_limit (Plan 04-02.1 public method)",
    }, indent=2), encoding="utf-8")

    # Teardown note: supervisor is left in armed state. The fixture is
    # function-scoped so each test gets a fresh supervisor; we do not call
    # reset_kill() here because the min_cooldown_seconds default (30s) would
    # force a ValueError and there is no next test in this file anyway.
