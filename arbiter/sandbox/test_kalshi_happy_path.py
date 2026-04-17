"""Kalshi demo happy-path lifecycle (Scenario 1: TEST-01 + TEST-04).

Live-fires the Kalshi FOK order lifecycle against demo-api.kalshi.co:
  1. Submit FOK at mid-price on a liquid demo market
  2. Observe fill (Order.status == OrderStatus.FILLED)
  3. Query GET /portfolio/fills?order_id=... for the fill record
  4. Assert fee_cost (authoritative per RESEARCH.md Pitfall 1 - see Pitfall 1)
     matches kalshi_order_fee() within +/-$0.01 (TEST-04 hard gate)
  5. Capture pre/post balance snapshots for TEST-03 aggregator (Plan 04-08)
  6. Dump execution_* tables + write scenario_manifest.json to evidence/04/

Operator pre-flight (Plan 04-03 Task 0):
  - .env.sandbox sourced (KALSHI_BASE_URL=https://demo-api.kalshi.co/...)
  - ./keys/kalshi_demo_private.pem exists
  - Demo account funded >=$50 via test card
  - arbiter_sandbox schema applied
  - HAPPY_MARKET_TICKER below is replaced with the chosen liquid demo market

Run (operator-gated):
  set -a; source .env.sandbox; set +a
  pytest -m live --live arbiter/sandbox/test_kalshi_happy_path.py -v
"""
from __future__ import annotations

import json
import os
from decimal import Decimal

import pytest
import structlog

from arbiter.config.settings import kalshi_order_fee
from arbiter.execution.engine import OrderStatus
from arbiter.sandbox import evidence, reconcile

log = structlog.get_logger("arbiter.sandbox.kalshi_happy")

# -------- OPERATOR-SUPPLIED CONSTANTS (populated from Plan 04-03 Task 0) --------
# Replace with the ticker identified in the Task 0 operator pre-flight.
# Happy-path market must have: liquid book (>=10 contracts depth at target price),
# price under ~$0.60 so notional qty*price stays small ($2-$3 range), demo-tradeable.
HAPPY_MARKET_TICKER = os.getenv("SANDBOX_HAPPY_TICKER", "REPLACE-WITH-OPERATOR-SUPPLIED-TICKER")
HAPPY_TARGET_PRICE = float(os.getenv("SANDBOX_HAPPY_PRICE", "0.50"))
HAPPY_TARGET_QTY = int(os.getenv("SANDBOX_HAPPY_QTY", "5"))


@pytest.mark.live
async def test_kalshi_happy_lifecycle(
    demo_kalshi_adapter, sandbox_db_pool, evidence_dir, balance_snapshot,
):
    """Submit FOK on liquid demo market -> fill -> DB row -> evidence dump -> fee_cost assertion."""
    adapter = demo_kalshi_adapter

    # Belt-and-suspenders DB guard (fixture already asserts, re-assert at test-body top
    # per plan so a misconfigured environment fails loudly before any network I/O).
    assert "arbiter_sandbox" in os.getenv("DATABASE_URL", ""), "wrong DB"

    # Fail-fast if operator forgot to wire the ticker (placeholder still present).
    assert HAPPY_MARKET_TICKER != "REPLACE-WITH-OPERATOR-SUPPLIED-TICKER", (
        "Plan 04-03 Task 0: SANDBOX_HAPPY_TICKER env var not set AND the literal "
        "placeholder ticker was not replaced in test_kalshi_happy_path.py. Operator "
        "must supply a liquid demo market ticker (e.g., export SANDBOX_HAPPY_TICKER=...)."
    )

    # TEST-03 input: pre-balance snapshot before FOK submit.
    pre_balances = await balance_snapshot()

    arb_id = "ARB-SANDBOX-KALSHI-HAPPY"
    order = await adapter.place_fok(
        arb_id=arb_id,
        market_id=HAPPY_MARKET_TICKER,
        canonical_id=HAPPY_MARKET_TICKER,  # single-platform scenario: canonical == market
        side="yes",
        price=HAPPY_TARGET_PRICE,
        qty=HAPPY_TARGET_QTY,
    )

    log.info(
        "scenario.kalshi_happy.order_returned",
        arb_id=arb_id,
        order_id=order.order_id,
        status=str(order.status),
        external_client_order_id=order.external_client_order_id,
    )

    # TEST-01 assertion: full lifecycle produced a FILLED order on a liquid market.
    assert order.status == OrderStatus.FILLED, (
        f"Expected FILLED on liquid demo market {HAPPY_MARKET_TICKER}; got {order.status}. "
        f"Error: {order.error!r}. Possible causes: market changed (thin liquidity now), "
        f"price moved past limit, demo account not funded. Re-pick the happy-path ticker."
    )

    # TEST-04 fee assertion: pull fill record from GET /portfolio/fills?order_id=...
    # and read the authoritative `fee_cost` field (Pitfall 1).
    fills = await _fetch_fills(adapter, order.order_id)
    assert fills, (
        f"TEST-04 FAILED: no fills returned by GET /portfolio/fills for FILLED order "
        f"{order.order_id}. Fill record should exist for a FILLED FOK; check Kalshi demo "
        f"endpoint availability or transient network issue."
    )
    first_fill = fills[0]

    # Kalshi Jan-2026 dollar-string migration: fee_cost is a fixed-point dollar string
    # (e.g. "0.12"). Parse via Decimal to avoid float precision artefacts on rounding.
    # Pitfall 1 (RESEARCH.md) selects fee_cost as the authoritative platform-fee field.
    platform_fee = float(Decimal(str(first_fill["fee_cost"])))
    computed_fee = kalshi_order_fee(HAPPY_TARGET_PRICE, HAPPY_TARGET_QTY)
    reconcile.assert_fee_matches("kalshi", platform_fee, computed_fee)

    log.info(
        "scenario.kalshi_happy.fee_match",
        platform_fee=platform_fee,
        computed_fee=computed_fee,
        delta=abs(platform_fee - computed_fee),
    )

    # TEST-03 input: post-balance snapshot after fill; Plan 04-08 aggregator asserts delta.
    post_balances = await balance_snapshot()

    # Evidence capture for Plan 04-08 aggregator + VALIDATION.md population.
    await evidence.dump_execution_tables(sandbox_db_pool, evidence_dir)
    evidence.write_balances(evidence_dir, pre_balances, post_balances)
    (evidence_dir / "scenario_manifest.json").write_text(
        json.dumps(
            {
                "scenario": "kalshi_happy_lifecycle",
                "requirement_ids": ["TEST-01", "TEST-04"],
                "tag": "real",
                "order_id": order.order_id,
                "external_client_order_id": order.external_client_order_id,
                "platform_fee": platform_fee,
                "computed_fee": computed_fee,
                "market": HAPPY_MARKET_TICKER,
                "side": "yes",
                "price": HAPPY_TARGET_PRICE,
                "qty": HAPPY_TARGET_QTY,
                "status": str(order.status),
            },
            indent=2,
        ),
        encoding="utf-8",
    )


async def _fetch_fills(adapter, order_id: str) -> list[dict]:
    """Retrieve fill records for a Kalshi order via GET /portfolio/fills?order_id=...

    KalshiAdapter exposes no public get_fills / get_fill method as of Phase 3.
    We reuse the adapter's already-authenticated aiohttp session + KalshiAuth
    signing helper to make the call. This is a TEST-ONLY bypass (scope boundary:
    Plan 04-03 is test-only; no production method is added to KalshiAdapter).

    Returns the `fills` list from the JSON response, or [] on any error. Each fill
    dict is expected to carry a `fee_cost` dollar-string field (Pitfall 1).
    """
    path = f"/trade-api/v2/portfolio/fills?order_id={order_id}"
    url = f"{adapter.config.kalshi.base_url}/portfolio/fills?order_id={order_id}"
    headers = adapter.auth.get_headers("GET", path)
    async with adapter.session.get(url, headers=headers) as response:
        body = await response.text()
        if response.status not in (200, 201):
            log.warning(
                "scenario.kalshi_happy.fills_http_error",
                status=response.status,
                body=body[:200],
            )
            return []
    try:
        data = json.loads(body)
    except Exception as exc:
        log.warning("scenario.kalshi_happy.fills_parse_failed", err=str(exc))
        return []
    if isinstance(data, dict):
        return list(data.get("fills", []) or [])
    return []
