"""FILL-01 marketability test: place a 1-contract FOK at the EXACT
current touch (best_ask) and compare against best_ask + 1 tick.

This is the decisive test for the +1 tick buffer fix:
  - If at-touch FILLS:    buffer may not be needed; revisit hypothesis
  - If at-touch EXPIRES:  buffer is necessary (book moves under stale limit)

We fetch the current BBO BEFORE submitting so the at-touch limit is
genuinely at-touch as of submission. Two orders total, 1 contract each
— max risk ~$1.20 if both fill on the wrong outcome.

Run inside the arbiter-api-prod container:
    docker exec arbiter-api-prod sh -c 'PYTHONPATH=/app python3 /app/scripts/validate_fok_at_touch.py'
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


async def _fetch_touch(client: PolymarketUSClient, slug: str) -> tuple[float, float]:
    book = await client.get_market_book(slug)
    md = book.get("marketData", {}) if isinstance(book, dict) else {}
    bids = md.get("bids") or []
    offers = md.get("offers") or []
    best_bid = _amount_value(bids[0].get("px")) if bids else 0.0
    best_ask = _amount_value(offers[0].get("px")) if offers else 0.0
    return best_bid, best_ask


async def _attempt(adapter: PolymarketUSAdapter, slug: str, label: str, price: float):
    print(f"\n--- {label}: FOK BUY YES qty=1 @ ${price:.4f} ---")
    t0 = time.monotonic_ns()
    order = await adapter.place_fok(
        arb_id=f"VALIDATE-FOK-{label}",
        market_id=slug,
        canonical_id="VALIDATE",
        side="yes",
        price=price,
        qty=1,
    )
    elapsed_ms = (time.monotonic_ns() - t0) / 1e6
    print(f"  place_fok returned in {elapsed_ms:.1f}ms")
    print(f"  order_id: {order.order_id} immediate: {order.status.value}")

    if order.status == OrderStatus.SUBMITTED:
        for i in range(20):
            await asyncio.sleep(0.5)
            order = await adapter.get_order(order)
            if order.status != OrderStatus.SUBMITTED:
                print(f"  poll #{i+1}: terminal status={order.status.value}")
                break

    print(f"  FINAL: status={order.status.value} fill_qty={order.fill_qty} fill_price={order.fill_price}")
    return order


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
    best_bid, best_ask = await _fetch_touch(client, slug)
    print(f"=== FILL-01 marketability test ===")
    print(f"slug: {slug}")
    print(f"current touch: bid={best_bid:.4f} ask={best_ask:.4f}\n")

    if best_ask <= 0:
        print("Market not active (no ask). Aborting.")
        await client.close()
        return

    # Test 1: at the touch (legacy unbuffered behavior)
    at_touch = await _attempt(adapter, slug, "AT_TOUCH", best_ask)

    # Test 2: at touch + 1 tick (new buffered behavior)
    at_buf = await _attempt(adapter, slug, "PLUS_1_TICK", best_ask + 0.01)

    print(f"\n=== Summary ===")
    print(f"AT_TOUCH    ({best_ask:.4f}): status={at_touch.status.value} fill_qty={at_touch.fill_qty}")
    print(f"PLUS_1_TICK ({best_ask + 0.01:.4f}): status={at_buf.status.value} fill_qty={at_buf.fill_qty}")
    print(f"\n_terminal_diagnostics: {len(adapter._terminal_diagnostics)} entries")
    for d in list(adapter._terminal_diagnostics):
        print(f"  {json.dumps(d, default=str, sort_keys=True)}")

    await client.close()


if __name__ == "__main__":
    asyncio.run(main())
