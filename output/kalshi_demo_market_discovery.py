"""Discover valid demo Kalshi markets for Phase 4 UAT re-sweep.

Goals:
  (a) Find a future-dated, 2-sided market with depth for killswitch/shutdown
      resting orders (Tests 5, 6, 9). Must rest BELOW best ask so it queues
      rather than fills. Minimal qty so the $5 PHASE4_MAX_ORDER_USD hard-lock
      is respected.
  (b) Find a thin-liquidity market for FOK rejection (Test 3). Must exist
      and accept orders but have minimal depth so our FOK fails to fill.
  (c) Find a liquid market for happy-path fill (Test 1). Must have yes_ask
      at a reasonable price with some existing offered qty, so a small FOK
      actually fills.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import aiohttp

sys.path.insert(0, os.fspath(Path(__file__).resolve().parent.parent))

from arbiter.collectors.kalshi import KalshiAuth

BASE = os.getenv("KALSHI_BASE_URL", "https://demo-api.kalshi.co/trade-api/v2")
API_KEY_ID = os.getenv("KALSHI_API_KEY_ID", "")
KEY_PATH = os.getenv("KALSHI_PRIVATE_KEY_PATH", "./keys/kalshi_demo_private.pem")

# Kalshi PSS signing requires the FULL path including /trade-api/v2 prefix
# (the adapter signs e.g. "/trade-api/v2/portfolio/orders"). Derive the prefix
# from the base URL so this works whether the base is demo or prod.
from urllib.parse import urlparse
SIGN_PREFIX = urlparse(BASE.rstrip("/")).path  # e.g. "/trade-api/v2"


async def signed_get(session: aiohttp.ClientSession, auth: KalshiAuth, path: str, params: dict | None = None) -> tuple[int, dict | str]:
    """Signed GET helper. Path is signed WITHOUT querystring (G-1 rule).

    The signed path must include the /trade-api/v2 prefix so the server-side
    signature validator accepts it.
    """
    query = ""
    if params:
        query = "&".join(f"{k}={v}" for k, v in params.items())
    url = f"{BASE.rstrip('/')}{path}" + (f"?{query}" if query else "")
    signed_path = f"{SIGN_PREFIX}{path}"  # e.g. "/trade-api/v2/portfolio/balance"
    headers = auth.get_headers("GET", signed_path)
    async with session.get(url, headers=headers) as resp:
        text = await resp.text()
        try:
            return resp.status, json.loads(text)
        except json.JSONDecodeError:
            return resp.status, text


async def main():
    auth = KalshiAuth(API_KEY_ID, KEY_PATH)

    out = {"base": BASE, "candidates": {}}

    async with aiohttp.ClientSession() as session:
        # Balance check (validates auth)
        status, body = await signed_get(session, auth, "/portfolio/balance")
        out["balance"] = {"status": status, "body": body}
        print(f"[balance] {status} {body}", file=sys.stderr)
        if status != 200:
            print(json.dumps(out))
            return

        # Fetch active open markets. Paginate up to 500.
        all_markets = []
        cursor = None
        page = 0
        while page < 20:
            params = {"status": "open", "limit": "200"}
            if cursor:
                params["cursor"] = cursor
            status, body = await signed_get(session, auth, "/markets", params)
            if status != 200 or not isinstance(body, dict):
                print(f"[markets] page {page} status={status}", file=sys.stderr)
                break
            ms = body.get("markets", [])
            all_markets.extend(ms)
            cursor = body.get("cursor") or None
            page += 1
            print(f"[markets] page={page} got={len(ms)} total={len(all_markets)} cursor={'y' if cursor else 'n'}", file=sys.stderr)
            if not cursor:
                break

        out["market_count"] = len(all_markets)

        # Filter: binary markets with yes_ask between 10c and 90c (actual price data
        # means there's at least some orderbook presence).
        candidates = []
        for m in all_markets:
            ticker = m.get("ticker", "")
            yes_ask = m.get("yes_ask")
            yes_bid = m.get("yes_bid")
            if yes_ask is None:
                continue
            try:
                ask = float(yes_ask)
                bid = float(yes_bid) if yes_bid is not None else 0.0
            except (TypeError, ValueError):
                continue
            if not (10 <= ask <= 90):
                continue
            candidates.append({
                "ticker": ticker,
                "title": m.get("title", ""),
                "yes_ask_cents": ask,
                "yes_bid_cents": bid,
                "no_ask_cents": m.get("no_ask"),
                "volume": m.get("volume", 0),
                "volume_24h": m.get("volume_24h", 0),
                "open_interest": m.get("open_interest", 0),
                "liquidity": m.get("liquidity", 0),
                "close_time": m.get("close_time", ""),
                "status": m.get("status", ""),
                "category": m.get("category", ""),
                "subtitle": m.get("subtitle", ""),
            })

        # Sort by volume (high-to-low) for the happy-path pick.
        by_volume = sorted(candidates, key=lambda c: c["volume"], reverse=True)
        # Sort by liquidity ascending for thin-market pick (but must have tradable asks).
        by_thin = sorted(
            [c for c in candidates if c["yes_ask_cents"] <= 85],
            key=lambda c: (c["liquidity"], c["volume"]),
        )

        out["candidates"]["liquid_top10"] = by_volume[:10]
        out["candidates"]["thin_bottom10"] = by_thin[:10]

        # Probe orderbooks on top 20 liquid to find one where resting below best
        # bid rests in the queue.
        probed = []
        for c in by_volume[:20]:
            status, body = await signed_get(session, auth, f"/markets/{c['ticker']}/orderbook")
            if status != 200 or not isinstance(body, dict):
                continue
            ob = body.get("orderbook", {}) or {}
            yes = ob.get("yes", [])
            no = ob.get("no", [])
            c_probe = dict(c)
            c_probe["orderbook"] = {"yes": yes, "no": no}
            # Count depth at each bid level.
            yes_depth = sum(int(lvl[1]) for lvl in yes) if yes else 0
            no_depth = sum(int(lvl[1]) for lvl in no) if no else 0
            c_probe["yes_depth_total"] = yes_depth
            c_probe["no_depth_total"] = no_depth
            probed.append(c_probe)

        out["candidates"]["probed_liquid"] = probed

        print(json.dumps(out, indent=2, default=str))


if __name__ == "__main__":
    asyncio.run(main())
