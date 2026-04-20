"""Kalshi demo FOK rejection on thin-liquidity market (Scenario 3: EXEC-01 invariant live-fire).

Live-fires EXEC-01 (FOK never partial-fills) against real Kalshi demo:
  1. Submit FOK on a thin-liquidity demo market with qty > available depth
  2. Kalshi returns HTTP 201 + body.status = "canceled" (Pitfall 3 - NOT an HTTP error)
  3. KalshiAdapter._FOK_STATUS_MAP maps "canceled" -> OrderStatus.CANCELLED
  4. Assert order.status == OrderStatus.CANCELLED (NEVER FILLED, NEVER PARTIAL)
  5. Dump execution_* tables + write scenario_manifest.json to evidence/04/

Operator pre-flight (Plan 04-03 Task 0):
  - Same env setup as happy path
  - Identify a thin-liquidity demo market (depth at target price < FOK_TARGET_QTY)

Run (operator-gated):
  set -a; source .env.sandbox; set +a
  pytest -m live --live arbiter/sandbox/test_kalshi_fok_rejection.py -v
"""
from __future__ import annotations

import json
import os

import pytest
import structlog

from arbiter.execution.engine import OrderStatus
from arbiter.sandbox import evidence

log = structlog.get_logger("arbiter.sandbox.kalshi_fok")

# -------- OPERATOR-SUPPLIED CONSTANTS (populated from Plan 04-03 Task 0) --------
# Thin-liquidity market: depth at FOK_TARGET_PRICE must be < FOK_TARGET_QTY so
# Kalshi cancels the whole FOK order (cannot fill completely -> status:canceled).
FOK_MARKET_TICKER = os.getenv("SANDBOX_FOK_TICKER", "REPLACE-WITH-OPERATOR-SUPPLIED-THIN-TICKER")
FOK_TARGET_PRICE = float(os.getenv("SANDBOX_FOK_PRICE", "0.50"))
FOK_TARGET_QTY = int(os.getenv("SANDBOX_FOK_QTY", "50"))


@pytest.mark.live
async def test_kalshi_fok_rejected_on_thin_market(
    demo_kalshi_adapter, sandbox_db_pool, evidence_dir,
):
    """FOK on thin demo market returns HTTP 201 with status:canceled -> adapter maps to CANCELLED."""
    adapter = demo_kalshi_adapter
    assert "arbiter_sandbox" in os.getenv("DATABASE_URL", ""), "wrong DB"

    # Fail-fast if operator forgot to wire the ticker.
    assert FOK_MARKET_TICKER != "REPLACE-WITH-OPERATOR-SUPPLIED-THIN-TICKER", (
        "Plan 04-03 Task 0: SANDBOX_FOK_TICKER env var not set AND the literal "
        "placeholder was not replaced. Operator must supply a thin-liquidity demo "
        "market ticker (depth < SANDBOX_FOK_QTY=50) for EXEC-01 live-fire."
    )

    arb_id = "ARB-SANDBOX-KALSHI-FOK-REJ"
    order = await adapter.place_fok(
        arb_id=arb_id,
        market_id=FOK_MARKET_TICKER,
        canonical_id=FOK_MARKET_TICKER,
        side="yes",
        price=FOK_TARGET_PRICE,
        qty=FOK_TARGET_QTY,
    )

    log.info(
        "scenario.kalshi_fok.order_returned",
        arb_id=arb_id,
        order_id=order.order_id,
        status=str(order.status),
        error=order.error,
    )

    # EXEC-01 invariant: thin-liquidity FOK MUST NOT partial-fill. Demo Kalshi
    # has shipped two shapes for the rejection, both of which satisfy EXEC-01:
    #   (a) HTTP 201 + body.status="canceled" -> adapter maps to OrderStatus.CANCELLED
    #       (Pitfall 3 legacy semantics, RESEARCH.md)
    #   (b) HTTP 409 + body.error.code="fill_or_kill_insufficient_resting_volume"
    #       -> adapter maps to OrderStatus.FAILED (current demo semantics, 2026-04-20;
    #       see G-4 in 04-HUMAN-UAT.md Test 3)
    # Both satisfy EXEC-01 because in both cases fill_qty == 0. We accept either
    # terminal status but enforce fill_qty == 0 as the actual invariant (G-4 fix,
    # Plan 04-09): status alone is brittle against platform response-shape drift;
    # fill_qty == 0 is the semantic guarantee we actually care about.
    assert order.status in (OrderStatus.CANCELLED, OrderStatus.FAILED), (
        f"EXEC-01 invariant: expected CANCELLED or FAILED on thin demo market "
        f"{FOK_MARKET_TICKER} with qty={FOK_TARGET_QTY}; got {order.status}. "
        f"Error: {order.error!r}. "
        f"If FILLED: market has more depth than expected -- pick a thinner market "
        f"or raise qty. If PARTIAL: EXEC-01 INVARIANT VIOLATED."
    )
    assert order.fill_qty == 0, (
        f"EXEC-01 INVARIANT VIOLATED: FOK rejection reported non-zero fill_qty="
        f"{order.fill_qty}. A rejected FOK MUST have zero fills."
    )

    # Evidence capture for Plan 04-08 aggregator.
    await evidence.dump_execution_tables(sandbox_db_pool, evidence_dir)
    (evidence_dir / "scenario_manifest.json").write_text(
        json.dumps(
            {
                "scenario": "kalshi_fok_rejected_on_thin_market",
                "requirement_ids": ["EXEC-01", "TEST-01"],
                "tag": "real",
                "order_id": order.order_id,
                "external_client_order_id": order.external_client_order_id,
                "market": FOK_MARKET_TICKER,
                "side": "yes",
                "price": FOK_TARGET_PRICE,
                "qty": FOK_TARGET_QTY,
                "status": str(order.status),
                # G-4 fix (Plan 04-09): EXEC-01 holds when the FOK reaches a
                # terminal non-fill state (CANCELLED or FAILED) AND fill_qty == 0.
                "exec_01_invariant_holds": (
                    order.status in (OrderStatus.CANCELLED, OrderStatus.FAILED)
                    and order.fill_qty == 0
                ),
            },
            indent=2,
        ),
        encoding="utf-8",
    )
