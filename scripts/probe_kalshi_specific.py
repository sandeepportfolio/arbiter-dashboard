#!/usr/bin/env python3
"""Search Kalshi specifically for CPI, GDP, unemployment, fed, jobless markets."""
import asyncio, json, os, sys, time
sys.path.insert(0, os.path.abspath(os.path.dirname(os.path.dirname(__file__))))
from arbiter.config.settings import load_config
from arbiter.collectors.kalshi import KalshiAuth
import aiohttp

async def kalshi_get(session, auth, base_url, path, params=None):
    url = f"{base_url}{path}"
    sign_path = "/trade-api/v2" + path
    headers = auth.get_headers("GET", sign_path)
    async with session.get(url, headers=headers, params=params) as resp:
        if resp.status != 200:
            text = await resp.text()
            raise RuntimeError(f"Kalshi {resp.status}: {text[:200]}")
        return await resp.json()

# Specific Kalshi event ticker prefixes for economic indicators
ECON_PREFIXES = [
    "KXCPI", "KXGDP", "KXUNR", "KXFED", "KXIJC", "KXRSM", "KXHS",
    "KXBPMI", "KXPREMP", "KXND", "KXGT", "KXGCE", "KXPPI", "KXPCE",
    "KXJOLT", "KXNHS", "KXFF", "KXNFP", "KXJOBLESS", "KXHOUSING",
    "CPI", "GDP", "UNRATE", "FED", "FOMC", "NFP", "INXC",
    "KXFEDDECISION", "KXU3", "KXDEBT",
]

async def main():
    cfg = load_config()
    kc = cfg.kalshi
    auth = KalshiAuth(kc.api_key_id, kc.private_key_path)
    timeout = aiohttp.ClientTimeout(total=30)

    async with aiohttp.ClientSession(timeout=timeout) as session:
        # Fetch ALL events and filter by ticker prefix
        print("Fetching all Kalshi events...")
        all_events = []
        cursor = None
        for page in range(100):
            params = {"limit": "200"}
            if cursor:
                params["cursor"] = cursor
            data = await kalshi_get(session, auth, kc.base_url, "/events", params)
            events = data.get("events", [])
            all_events.extend(events)
            cursor = data.get("cursor")
            if not cursor:
                break
        print(f"Total events: {len(all_events)}")

        # Filter by prefix
        matched = []
        for ev in all_events:
            ticker = str(ev.get("event_ticker", "")).upper()
            for pfx in ECON_PREFIXES:
                if ticker.startswith(pfx.upper()):
                    matched.append(ev)
                    break

        print(f"Economic prefix matches: {len(matched)}")

        # Get markets for each
        results = []
        for i, ev in enumerate(matched):
            ticker = ev.get("event_ticker", "")
            if i > 0 and i % 10 == 0:
                await asyncio.sleep(1)  # Rate limit
            try:
                data = await kalshi_get(session, auth, kc.base_url,
                    "/markets", {"event_ticker": ticker, "limit": "100"})
                markets = data.get("markets", [])
                results.append({
                    "event_ticker": ticker,
                    "title": str(ev.get("title", ""))[:120],
                    "market_count": len(markets),
                    "markets": [{
                        "ticker": m.get("ticker", ""),
                        "title": str(m.get("title", ""))[:150],
                        "yes_sub_title": str(m.get("yes_sub_title", ""))[:100],
                        "status": m.get("status", ""),
                    } for m in markets]
                })
            except Exception as e:
                results.append({"event_ticker": ticker, "error": str(e)})

        with open("/tmp/kalshi_specific.json", "w") as f:
            json.dump(results, f, indent=2)

        total_mkts = sum(r.get("market_count", 0) for r in results)
        print(f"\n{len(results)} events, {total_mkts} sub-markets")
        for r in sorted(results, key=lambda x: x.get("event_ticker", "")):
            cnt = r.get("market_count", 0)
            if cnt:
                samples = [m["ticker"] for m in r.get("markets", [])[:3]]
                print(f"  {r['event_ticker']:<35s} {cnt:3d} mkts  {r.get('title','')[:50]}  {samples}")

asyncio.run(main())
