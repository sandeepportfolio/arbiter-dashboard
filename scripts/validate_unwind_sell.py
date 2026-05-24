"""Validate PolymarketUSAdapter.place_unwind_sell against the live venue.

Strategy (capital-safe, ~$0.10 max risk):
  1. BUY 1 YES via place_fok at best_ask + 1 tick (the FILL-01 buffer).
  2. Confirm position via /portfolio/positions.
  3. SELL 1 YES via place_unwind_sell with panic_price=0.01.
  4. Confirm position closed.
  5. Print the round-trip cost (spread + fees, ~$0.01-0.04).

This proves the NEW unwind-sell wire path that the FILL-01 commit added
to PolymarketUSAdapter — without which the stranded-position reconciler's
auto-close branch can't actually close PU lots.

Run inside the container:
    docker exec arbiter-api-prod sh -c 'PYTHONPATH=/app python3 /app/scripts/validate_unwind_sell.py'
"""
from __future__ import annotations

import asyncio
import json
import time

from arbiter.auth.ed25519_signer import Ed25519Signer
from arbiter.collectors.polymarket_us import PolymarketUSClient, _amount_value
from arbiter.config.settings import PolymarketUSConfig, load_config
from arbiter.execution.adapters.polymarket_us import PolymarketUSAdapter
from arbiter.execution.engine import OrderStatus


async def main() -> None:
    cfg = load_config()
    if not isinstance(cfg.polymarket, PolymarketUSConfig):
        raise SystemExit("polymarket is not PolymarketUSConfig")
    pu = cfg.polymarket
    signer = Ed25519Signer(key_id=pu.api_key_id, secret_b64=pu.api_secret)
    client = PolymarketUSClient(
        base_url=pu.api_url, public_base_url=pu.gateway_url, signer=signer,
    )
    adapter = PolymarketUSAdapter(client=client)

    slug = "aec-mlb-pit-tor-2026-05-24"
    book = await client.get_market_book(slug)
    md = book.get("marketData", {}) if isinstance(book, dict) else {}
    bids = md.get("bids") or []
    offers = md.get("offers") or []
    best_bid = _amount_value(bids[0].get("px")) if bids else 0.0
    best_ask = _amount_value(offers[0].get("px")) if offers else 0.0
    state = md.get("state", md.get("status"))
    print(f"=== place_unwind_sell wire validation ===")
    print(f"slug: {slug}")
    print(f"state: {state}")
    print(f"touch: bid={best_bid:.4f} ask={best_ask:.4f}\n")

    if state and str(state).upper() not in ("MARKET_STATE_OPEN", "OPEN", "ACTIVE"):
        print(f"Market not open ({state}); skipping unwind test.")
        await client.close()
        return
    if best_ask <= 0 or best_bid <= 0:
        print("No BBO; skipping.")
        await client.close()
        return

    # Step 1: buy 1 YES at touch + 1 tick.
    buy_price = round(best_ask + 0.01, 2)
    print(f"--- BUY 1 YES @ ${buy_price:.4f} (touch+1 tick) ---")
    buy = await adapter.place_fok(
        arb_id="VALIDATE-UNWIND-BUY",
        market_id=slug, canonical_id="VALIDATE-UNWIND",
        side="yes", price=buy_price, qty=1,
    )
    print(f"  order_id: {buy.order_id} immediate: {buy.status.value}")
    if buy.status == OrderStatus.SUBMITTED:
        for i in range(20):
            await asyncio.sleep(0.5)
            buy = await adapter.get_order(buy)
            if buy.status != OrderStatus.SUBMITTED:
                print(f"  poll #{i+1}: {buy.status.value}")
                break
    print(f"  final: status={buy.status.value} fill_qty={buy.fill_qty} fill_price={buy.fill_price}")

    if buy.status != OrderStatus.FILLED and float(buy.fill_qty or 0) < 1:
        print("\nBUY didn't fill; cannot test unwind path. Aborting.")
        await client.close()
        return

    # Step 2: sell via place_unwind_sell (panic at $0.01 — accept any bid).
    print(f"\n--- SELL 1 YES via place_unwind_sell (panic=$0.01) ---")
    sell = await adapter.place_unwind_sell(
        arb_id="VALIDATE-UNWIND-SELL",
        market_id=slug, canonical_id="VALIDATE-UNWIND",
        side="yes", qty=1, panic_price=0.01,
    )
    print(f"  order_id: {sell.order_id} immediate: {sell.status.value}")
    if sell.status == OrderStatus.SUBMITTED:
        for i in range(20):
            await asyncio.sleep(0.5)
            sell = await adapter.get_order(sell)
            if sell.status != OrderStatus.SUBMITTED:
                print(f"  poll #{i+1}: {sell.status.value}")
                break
    print(f"  final: status={sell.status.value} fill_qty={sell.fill_qty} fill_price={sell.fill_price}")

    # Step 3: verify positions
    resp = await client._signed("GET", "/portfolio/positions")
    positions = (resp or {}).get("positions", {}) or {}
    p = positions.get(slug)
    print(f"\nPost-trade position on {slug}: {p.get('netPosition') if p else 'none'}")

    if buy.status == OrderStatus.FILLED and sell.status == OrderStatus.FILLED:
        buy_cost = float(buy.fill_qty) * float(buy.fill_price)
        sell_rev = float(sell.fill_qty) * float(sell.fill_price)
        print(f"\nRound-trip: bought ${buy_cost:.4f}, sold ${sell_rev:.4f}, net ${sell_rev - buy_cost:.4f}")

    await client.close()


if __name__ == "__main__":
    asyncio.run(main())
