#!/usr/bin/env python3
"""Probe Kalshi for economic event tickers that might match FX families."""
import asyncio, json, os, sys
sys.path.insert(0, os.path.abspath(os.path.dirname(os.path.dirname(__file__))))
from arbiter.config.settings import load_config
from arbiter.collectors.kalshi import KalshiCollector

# Keywords to search for in Kalshi event tickers/titles
ECON_KEYWORDS = [
    "CPI", "GDP", "FED", "FOMC", "UNRATE", "UNEMPLOYMENT", "JOBLESS",
    "RETAIL", "HOUSING", "PAYROLL", "NFP", "PERMIT", "DEBT", "CLIMATE",
    "TEMPERATURE", "GOLD", "CARBON", "PPI", "PCE", "JOLTS", "NEWHOME",
    "INFLATION", "RATE", "EMPLOYMENT",
]

async def main():
    cfg = load_config()
    collector = KalshiCollector(cfg.kalshi)

    print("Fetching all Kalshi events...")
    try:
        events = await collector.list_all_events(status="open")
    except Exception as e:
        print(f"ERROR: {e}")
        return

    print(f"Total events: {len(events)}")

    # Filter to economic events
    econ_events = []
    for ev in events:
        ticker = str(ev.get("event_ticker", "") or ev.get("ticker", "")).upper()
        title = str(ev.get("title", "")).upper()
        category = str(ev.get("category", "")).lower()

        if any(kw in ticker or kw in title for kw in ECON_KEYWORDS):
            econ_events.append({
                "ticker": ticker,
                "title": str(ev.get("title", ""))[:100],
                "category": category,
                "sub_title": str(ev.get("sub_title", ""))[:100],
            })

    print(f"Economic events: {len(econ_events)}")

    # For each economic event, get its sub-markets
    detailed = []
    for ev in econ_events[:30]:  # Cap at 30 to avoid rate limits
        try:
            markets = await collector.list_markets_for_event(ev["ticker"])
            market_summaries = []
            for m in markets[:10]:
                market_summaries.append({
                    "ticker": m.get("ticker", ""),
                    "title": str(m.get("title", ""))[:120],
                    "yes_sub_title": str(m.get("yes_sub_title", ""))[:120],
                    "status": m.get("status", ""),
                })
            ev["markets"] = market_summaries
            ev["market_count"] = len(markets)
        except Exception as e:
            ev["markets"] = []
            ev["market_count"] = 0
            ev["error"] = str(e)
        detailed.append(ev)

    with open("/tmp/kalshi_econ.json", "w") as f:
        json.dump(detailed, f, indent=2)

    print(f"\nWrote {len(detailed)} events to /tmp/kalshi_econ.json")
    for ev in detailed:
        print(f"  {ev['ticker']:<30s} {ev.get('market_count',0):3d} markets  {ev['title'][:60]}")

asyncio.run(main())
