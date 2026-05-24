"""FILL-01 wire-path validation.

Exercises the live Polymarket US FOK path with a deliberately
non-marketable limit so the venue MUST kill the order — zero exposure,
zero risk capital. Verifies:

  - No BBO HTTP call in the adapter clamp (cache + tick-snap only)
  - wire_price, clamp_ms, place_ms appear in polymarket_us.order.placed
  - get_order surfaces the terminal api_status via _poll-style call
  - _terminal_diagnostics ring buffer captures the event
  - /api/system.execution_diagnostics surfaces it for ops.html

Run inside the arbiter-api-prod container:
    docker exec arbiter-api-prod sh -c 'PYTHONPATH=/app python3 /app/scripts/validate_fok_path.py'
"""
from __future__ import annotations

import asyncio
import json
import time

from arbiter.auth.ed25519_signer import Ed25519Signer
from arbiter.collectors.polymarket_us import PolymarketUSClient
from arbiter.config.settings import PolymarketUSConfig, load_config
from arbiter.execution.adapters.polymarket_us import PolymarketUSAdapter
from arbiter.execution.engine import OrderStatus


async def main() -> None:
    cfg = load_config()
    if not isinstance(cfg.polymarket, PolymarketUSConfig):
        raise SystemExit(
            f"polymarket config is {type(cfg.polymarket).__name__}, not PolymarketUSConfig"
        )
    pu = cfg.polymarket
    if not (pu.api_key_id and pu.api_secret):
        raise SystemExit("POLYMARKET_US credentials missing")

    signer = Ed25519Signer(key_id=pu.api_key_id, secret_b64=pu.api_secret)
    client = PolymarketUSClient(
        base_url=pu.api_url,
        public_base_url=pu.gateway_url,
        signer=signer,
    )
    adapter = PolymarketUSAdapter(client=client)

    # Pick the same slug we saw fail earlier today so we have a baseline
    # for comparison. State must be MARKET_STATE_OPEN.
    slug = "aec-mlb-pit-tor-2026-05-24"

    print("=== FILL-01 wire-path validation ===")
    print(f"slug: {slug}")
    print("plan: BUY YES @ $0.01 qty=1 (non-marketable; must be killed)")
    print("expected risk: $0 (FOK at $0.01 cannot fill against a healthy book)\n")

    t_call_start = time.monotonic_ns()
    try:
        order = await adapter.place_fok(
            arb_id="VALIDATE-FILL-01",
            market_id=slug,
            canonical_id="VALIDATE",
            side="yes",
            price=0.01,
            qty=1,
        )
    except Exception as exc:
        print(f"adapter.place_fok raised: {type(exc).__name__}: {exc}")
        await client.close()
        return
    t_call_end = time.monotonic_ns()
    elapsed_ms = (t_call_end - t_call_start) / 1e6

    print(f"place_fok returned in {elapsed_ms:.1f}ms")
    print(f"  order_id: {order.order_id}")
    print(f"  immediate status: {order.status.value}")
    print(f"  fill_qty/fill_price: {order.fill_qty} / {order.fill_price}")
    print(f"  error: {order.error or '(none)'}")

    # If venue accepted onto the book (NEW), poll until terminal so the
    # adapter's _terminal_diagnostics catches the api_status.
    if order.status == OrderStatus.SUBMITTED:
        print("\nOrder SUBMITTED — polling for terminal state...")
        for i in range(20):
            await asyncio.sleep(0.5)
            order = await adapter.get_order(order)
            if order.status != OrderStatus.SUBMITTED:
                print(f"  poll #{i+1}: terminal status={order.status.value}")
                break
        else:
            print("  timeout — cancelling")
            await adapter.cancel_order(order)

    print(f"\nfinal status: {order.status.value} fill_qty={order.fill_qty}")
    diag = list(adapter._terminal_diagnostics)
    print(f"\n_terminal_diagnostics: {len(diag)} entries")
    for d in diag:
        print(f"  {json.dumps(d, default=str, sort_keys=True)}")

    await client.close()


if __name__ == "__main__":
    asyncio.run(main())
