"""Polymarket real-$ happy path with fee reconstruction via get_trades (Scenario 2: TEST-02 + TEST-04).

Highest-risk live-fire scenario — burns real USDC. Belt-and-suspenders safety:
  1. Test body asserts notional <= PHASE4_MAX_ORDER_USD BEFORE calling adapter.
  2. PolymarketAdapter.place_fok PHASE4_MAX_ORDER_USD hard-lock (Plan 04-02).
  3. Test-wallet funding cap (~$10 USDC) — hardware limit.

Fee reconstruction (Pitfall 2 — authoritative): Polymarket's `post_order` response carries
NO fee field. After the FOK fill we call `client.get_trades(TradeParams(maker_address, market))`
and rebuild the platform-charged fee per-trade as
    (fee_rate_bps / 10_000) * size * price * (1 - price)
then compare against `polymarket_order_fee(price, qty, category=...)` within +/-$0.01.

A2 runtime verification: the first live run dumps the raw trade dict keys into
`polymarket_trades_raw.json` so the SUMMARY.md can document actual field names observed.
Reconstruction handles snake_case and camelCase fallbacks for resilience.

Pitfall 5: min_order_size is per-market — must pre-flight via `get_order_book(token_id)`.

Market constants (baked in from Phase 4 research-agent pre-flight, 2026-04-17):
  Happy-path target: "will-james-talarico-win-the-2028-democratic-presidential-nomination"
    token 52535923606561722941567320365820395300598958985353103429657683100920373025261
    best_ask=0.022, depth_at_target=71,328.57 shares, min_order_size=5,
    tick=0.001, liquidityClob=$47.28M, endDate=2028-11-07
    notional = 227 * 0.022 = $4.994 (<= $5 PHASE4_MAX_ORDER_USD hardlock)

Env-var overrides (operator can swap markets without code change):
  PHASE4_POLY_HAPPY_TOKEN, PHASE4_POLY_HAPPY_PRICE, PHASE4_POLY_HAPPY_QTY,
  PHASE4_POLY_HAPPY_CATEGORY
"""
from __future__ import annotations

import asyncio
import json
import os

import pytest
import structlog

from arbiter.config.settings import polymarket_order_fee
from arbiter.execution.engine import OrderStatus
from arbiter.sandbox import evidence, reconcile

log = structlog.get_logger("arbiter.sandbox.poly_happy")

# --- Market constants (research-agent pre-flight 2026-04-17; env-var overridable) ----

_DEFAULT_HAPPY_TOKEN = (
    "52535923606561722941567320365820395300598958985353103429657683100920373025261"
)
_DEFAULT_HAPPY_PRICE = 0.022
_DEFAULT_HAPPY_QTY = 227
_DEFAULT_HAPPY_CATEGORY = "politics"


def _float_env(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


def _int_env(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    try:
        return int(float(raw))
    except (TypeError, ValueError):
        return default


HAPPY_TOKEN_ID = os.getenv("PHASE4_POLY_HAPPY_TOKEN") or _DEFAULT_HAPPY_TOKEN
HAPPY_PRICE = _float_env("PHASE4_POLY_HAPPY_PRICE", _DEFAULT_HAPPY_PRICE)
HAPPY_QTY = _int_env("PHASE4_POLY_HAPPY_QTY", _DEFAULT_HAPPY_QTY)
HAPPY_CATEGORY = os.getenv("PHASE4_POLY_HAPPY_CATEGORY") or _DEFAULT_HAPPY_CATEGORY


@pytest.mark.live
async def test_polymarket_happy_lifecycle(
    poly_test_adapter, sandbox_db_pool, evidence_dir, balance_snapshot,
):
    """Submit real-$ FOK on Polymarket liquid market -> fill -> USDC delta + fee reconstruction via get_trades."""
    adapter = poly_test_adapter

    # Safety rail: sandbox DB only (matches other sandbox fixtures).
    assert "arbiter_sandbox" in os.getenv("DATABASE_URL", ""), (
        "SAFETY: DATABASE_URL must point at arbiter_sandbox; refusing to run a real-$ "
        "Polymarket scenario against a non-sandbox DB."
    )

    # BELT: triple-check notional before touching adapter. The adapter's PHASE4_MAX_ORDER_USD
    # hard-lock (Plan 04-02) is the FINAL line of defense; this assertion catches operator
    # error BEFORE the adapter sees the order, so a mis-provisioned test fails loudly here
    # instead of producing a generic hard-lock rejection in logs.
    notional = HAPPY_PRICE * HAPPY_QTY
    assert notional <= 5.0, (
        f"SAFETY: test notional ${notional:.4f} exceeds $5 limit "
        f"(HAPPY_PRICE={HAPPY_PRICE} * HAPPY_QTY={HAPPY_QTY}). "
        f"This would trip the PHASE4_MAX_ORDER_USD hard-lock; failing fast."
    )

    # Pre-flight (Pitfall 5): read min_order_size via get_order_book. Polymarket's
    # py-clob-client is synchronous, so wrap each SDK call in run_in_executor.
    client = adapter._get_client()
    assert client is not None, (
        "Polymarket client failed to initialize. Check POLY_PRIVATE_KEY, POLY_SIGNATURE_TYPE, "
        "POLY_FUNDER, and POLYMARKET_CLOB_URL in .env.sandbox."
    )

    loop = asyncio.get_event_loop()
    book = await loop.run_in_executor(None, client.get_order_book, HAPPY_TOKEN_ID)

    # Extract min_order_size (dict form OR object-attribute form — SDK returns a dict-like
    # OrderBookSummary; tolerate both shapes defensively).
    if isinstance(book, dict):
        min_order_size_raw = book.get("min_order_size", book.get("minOrderSize", 0))
    else:
        min_order_size_raw = getattr(
            book, "min_order_size", getattr(book, "minOrderSize", 0)
        )
    try:
        min_order_size = float(min_order_size_raw or 0)
    except (TypeError, ValueError):
        min_order_size = 0.0

    log.info(
        "scenario.poly_happy.pre_flight",
        token=HAPPY_TOKEN_ID,
        min_order_size=min_order_size,
        qty=HAPPY_QTY,
        price=HAPPY_PRICE,
        notional=notional,
    )
    assert HAPPY_QTY >= min_order_size, (
        f"qty={HAPPY_QTY} below market min_order_size={min_order_size}. "
        f"Platform will reject with INVALID_ORDER_MIN_SIZE. "
        f"Operator: pick a market with lower min_order_size or raise qty "
        f"(respecting the $5 notional cap: max_qty = floor(5.0 / price))."
    )

    # Pre-balance snapshot (TEST-03 input).
    pre_balances = await balance_snapshot()

    # Place FOK on the live book.
    arb_id = "ARB-SANDBOX-POLY-HAPPY"
    order = await adapter.place_fok(
        arb_id=arb_id,
        market_id=HAPPY_TOKEN_ID,
        canonical_id=HAPPY_TOKEN_ID,
        side="yes",
        price=HAPPY_PRICE,
        qty=HAPPY_QTY,
    )

    log.info(
        "scenario.poly_happy.order_returned",
        order_id=order.order_id,
        status=str(order.status),
        error=getattr(order, "error", None),
        fill_price=getattr(order, "fill_price", None),
        fill_qty=getattr(order, "fill_qty", None),
    )

    # TEST-02: full lifecycle -> FILLED.
    assert order.status == OrderStatus.FILLED, (
        f"Expected OrderStatus.FILLED on Polymarket token {HAPPY_TOKEN_ID}; got {order.status}. "
        f"Order.error={getattr(order, 'error', None)!r}. "
        f"Possible causes: market moved past limit, wallet not funded, "
        f"SignatureType/funder mismatch, min_order_size not met, "
        f"PHASE4_MAX_ORDER_USD hard-lock tripped."
    )

    # TEST-04: reconstruct platform fee from get_trades (Pitfall 2 — no fee in post_order response).
    # Pattern 4 from RESEARCH.md. A2 assumption: field names need runtime verification;
    # the test LOGS the raw keys and writes polymarket_trades_raw.json for SUMMARY.md.
    await asyncio.sleep(2.0)  # small delay for trade indexing

    try:
        from py_clob_client.clob_types import TradeParams  # type: ignore
    except ImportError:
        pytest.fail(
            "py_clob_client.clob_types.TradeParams missing — version mismatch; expected 0.34.6."
        )

    # Resolve maker_address: prefer SDK's get_address(), fall back to POLY_FUNDER env.
    get_address = getattr(client, "get_address", None)
    if callable(get_address):
        try:
            maker_address = get_address()
        except Exception:
            maker_address = os.getenv("POLY_FUNDER")
    else:
        maker_address = os.getenv("POLY_FUNDER")

    assert maker_address, (
        "Cannot resolve maker_address for get_trades: client.get_address() failed "
        "and POLY_FUNDER env var is unset."
    )

    trades = await loop.run_in_executor(
        None,
        lambda: client.get_trades(
            TradeParams(maker_address=maker_address, market=HAPPY_TOKEN_ID)
        ),
    )

    first_keys = []
    if trades and isinstance(trades[0], dict):
        first_keys = list(trades[0].keys())
    log.info(
        "scenario.poly_happy.trades_raw_keys",
        trades_count=len(trades),
        first_keys=first_keys,
    )

    # Dump raw trades to evidence/ for A2 field-name verification in SUMMARY.md.
    (evidence_dir / "polymarket_trades_raw.json").write_text(
        json.dumps(trades, indent=2, default=str), encoding="utf-8",
    )

    assert trades, (
        f"get_trades returned empty for maker={maker_address}, market={HAPPY_TOKEN_ID}. "
        f"Possible causes: trade not yet indexed (raise sleep above), "
        f"wrong maker_address, wrong market token_id param."
    )

    # Reconstruct platform fee per Pitfall 2:
    #   fee = sum over trades of (fee_rate_bps / 10_000) * size * price * (1 - price)
    # Field names fall back snake_case -> camelCase so A2 field-name drift is resilient.
    platform_fee = 0.0
    for idx, trade in enumerate(trades):
        if not isinstance(trade, dict):
            log.warning(
                "scenario.poly_happy.trade_non_dict", idx=idx, type=type(trade).__name__
            )
            continue
        rate_bps_raw = trade.get(
            "fee_rate_bps", trade.get("feeRateBps", trade.get("fee_rate", 0))
        )
        size_raw = trade.get(
            "size", trade.get("matched_amount", trade.get("matchedAmount", 0))
        )
        price_raw = trade.get("price", 0)
        try:
            rate_bps = float(rate_bps_raw or 0)
            size = float(size_raw or 0)
            trade_price = float(price_raw or 0)
        except (TypeError, ValueError):
            log.warning(
                "scenario.poly_happy.trade_unparseable",
                idx=idx,
                rate_bps_raw=rate_bps_raw,
                size_raw=size_raw,
                price_raw=price_raw,
            )
            continue
        trade_fee = (rate_bps / 10_000.0) * size * trade_price * (1.0 - trade_price)
        platform_fee += trade_fee
        log.info(
            "scenario.poly_happy.trade_fee",
            idx=idx,
            rate_bps=rate_bps,
            size=size,
            price=trade_price,
            fee=trade_fee,
        )

    # Compute expected fee via local fee function. polymarket_order_fee signature
    # (confirmed in arbiter/config/settings.py:80-85): accepts price, quantity, fee_rate?, category?.
    computed_fee = polymarket_order_fee(HAPPY_PRICE, HAPPY_QTY, category=HAPPY_CATEGORY)

    # TEST-04 hard gate: +/-$0.01 reconciliation.
    reconcile.assert_fee_matches("polymarket", platform_fee, computed_fee)
    log.info(
        "scenario.poly_happy.fee_match",
        platform_fee=platform_fee,
        computed_fee=computed_fee,
        discrepancy=platform_fee - computed_fee,
    )

    # Post-balance snapshot.
    post_balances = await balance_snapshot()

    await evidence.dump_execution_tables(sandbox_db_pool, evidence_dir)
    evidence.write_balances(evidence_dir, pre_balances, post_balances)
    (evidence_dir / "scenario_manifest.json").write_text(
        json.dumps(
            {
                "scenario": "polymarket_happy_lifecycle",
                "requirement_ids": ["TEST-02", "TEST-04"],
                "tag": "real",
                "order_id": order.order_id,
                "market_token_id": HAPPY_TOKEN_ID,
                "price": HAPPY_PRICE,
                "qty": HAPPY_QTY,
                "notional": notional,
                "category": HAPPY_CATEGORY,
                "platform_fee": platform_fee,
                "computed_fee": computed_fee,
                "fee_discrepancy": platform_fee - computed_fee,
                "status": str(order.status),
                "trades_count": len(trades),
                "trade_first_keys": first_keys,
                "min_order_size": min_order_size,
            },
            indent=2,
            default=str,
        ),
        encoding="utf-8",
    )
