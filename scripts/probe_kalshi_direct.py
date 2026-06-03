#!/usr/bin/env python3
"""Direct Kalshi API probe for economic events — no PriceStore needed."""
import asyncio, json, os, sys
sys.path.insert(0, os.path.abspath(os.path.dirname(os.path.dirname(__file__))))
from arbiter.config.settings import load_config, KalshiConfig
from arbiter.collectors.kalshi import KalshiAuth
import aiohttp

async def kalshi_get(session, auth, base_url, path, params=None):
    """Authenticated GET request to Kalshi API."""
    # base_url already includes /trade-api/v2, path is relative like /events
    url = f"{base_url}{path}"
    # For signing, Kalshi wants the full path from root
    sign_path = "/trade-api/v2" + path
    method = "GET"
    headers = auth.get_headers(method, sign_path)
    async with session.get(url, headers=headers, params=params) as resp:
        if resp.status != 200:
            text = await resp.text()
            raise RuntimeError(f"Kalshi {resp.status}: {text[:200]}")
        return await resp.json()

async def main():
    cfg = load_config()
    kc = cfg.kalshi
    auth = KalshiAuth(kc.api_key_id, kc.private_key_path)

    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        # Fetch events page by page, looking for economic ones
        print("Fetching Kalshi events...")
        all_events = []
        cursor = None
        for page in range(50):
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

        # Filter economic events by ticker pattern and title keywords
        econ_keywords = ["CPI", "GDP", "FED", "FOMC", "UNRATE", "UNEMPLOYMENT",
                        "JOBLESS", "RETAIL", "HOUSING", "PAYROLL", "NFP", "PERMIT",
                        "DEBT", "CLIMATE", "TEMPERATURE", "GOLD", "CARBON", "PPI",
                        "PCE", "JOLTS", "NEWHOME", "INFLATION", "RATE", "NONFARM",
                        "CONSUMER PRICE", "INITIAL CLAIMS"]

        econ_events = []
        for ev in all_events:
            ticker = str(ev.get("event_ticker", "")).upper()
            title = str(ev.get("title", "")).upper()
            if any(kw in ticker or kw in title for kw in econ_keywords):
                econ_events.append(ev)

        print(f"Economic events: {len(econ_events)}")

        # For each econ event, fetch sub-markets
        results = []
        for ev in econ_events[:40]:
            ticker = ev.get("event_ticker", "")
            try:
                data = await kalshi_get(session, auth, kc.base_url,
                    "/markets", {"event_ticker": ticker, "limit": "50"})
                markets = data.get("markets", [])
                mkt_list = []
                for m in markets:
                    mkt_list.append({
                        "ticker": m.get("ticker", ""),
                        "title": str(m.get("title", ""))[:150],
                        "yes_sub_title": str(m.get("yes_sub_title", ""))[:150],
                        "subtitle": str(m.get("subtitle", ""))[:150],
                        "status": m.get("status", ""),
                        "floor_strike": m.get("floor_strike"),
                        "cap_strike": m.get("cap_strike"),
                    })
                results.append({
                    "event_ticker": ticker,
                    "title": str(ev.get("title", ""))[:120],
                    "category": ev.get("category", ""),
                    "market_count": len(markets),
                    "markets": mkt_list,
                })
            except Exception as e:
                results.append({"event_ticker": ticker, "error": str(e), "markets": []})

        with open("/tmp/kalshi_econ.json", "w") as f:
            json.dump(results, f, indent=2)

        # Print summary
        total_markets = sum(r.get("market_count", 0) for r in results)
        print(f"\nEvents with markets: {len(results)}, total sub-markets: {total_markets}")
        for r in results:
            cnt = r.get("market_count", 0)
            if cnt > 0:
                sample_tickers = [m["ticker"] for m in r.get("markets", [])[:3]]
                print(f"  {r['event_ticker']:<30s} {cnt:3d} mkts  {r['title'][:50]}  e.g. {sample_tickers}")

asyncio.run(main())
