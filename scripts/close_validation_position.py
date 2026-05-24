"""Close the 2-contract PIT YES position created by the FILL-01 wire-path
test. Sells at the current best bid to flatten immediately. Small loss
(spread + 1 tick) is the cost of the validation that proved the fix.

Run inside the container:
    docker exec arbiter-api-prod sh -c 'PYTHONPATH=/app python3 /app/scripts/close_validation_position.py'
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
    pu = cfg.polymarket
    if not isinstance(pu, PolymarketUSConfig):
        raise SystemExit("polymarket is not PolymarketUSConfig")
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
    print(f"slug: {slug}")
    print(f"current touch: bid={best_bid:.4f} ask={best_ask:.4f}")

    # Sell 2 YES at best_bid - 1 tick (aggressively marketable, mirroring
    # the +1 tick BUY buffer on the sell side: cross below the bid so
    # the venue cannot rest-and-expire).
    sell_price = max(best_bid - 0.01, 0.01)
    print(f"\nselling 2 YES @ ${sell_price:.4f} (best_bid - 1 tick)")
    order = await adapter.place_fok(
        arb_id="CLOSE-VALIDATE",
        market_id=slug,
        canonical_id="VALIDATE",
        side="SELL_YES",  # SELL_LONG via _us_order_params
        price=sell_price,
        qty=2,
    )
    print(f"  order_id: {order.order_id} immediate: {order.status.value}")

    if order.status == OrderStatus.SUBMITTED:
        for i in range(20):
            await asyncio.sleep(0.5)
            order = await adapter.get_order(order)
            if order.status != OrderStatus.SUBMITTED:
                print(f"  poll #{i+1}: terminal status={order.status.value}")
                break

    print(f"\nfinal: status={order.status.value} fill_qty={order.fill_qty} fill_price={order.fill_price}")
    print(f"diagnostics: {len(adapter._terminal_diagnostics)} entries")
    for d in list(adapter._terminal_diagnostics):
        print(f"  {json.dumps(d, default=str, sort_keys=True)}")

    await client.close()


if __name__ == "__main__":
    asyncio.run(main())
