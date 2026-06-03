#!/usr/bin/env python3
"""Targeted platform scan for new cross-platform mapping candidates.

Queries Kalshi events by series ticker and Polymarket by text search.
Designed to be run from the host machine (needs network access).
"""
import requests
import json
import sys
import time
import re

KALSHI_API = "https://api.elections.kalshi.com/trade-api/v2"
POLY_API = "https://gamma-api.polymarket.com"


def kalshi_events_for_series(series: str):
    """Fetch all events for a Kalshi series ticker."""
    try:
        r = requests.get(f"{KALSHI_API}/events", params={"series_ticker": series, "status": "open", "limit": 100}, timeout=15)
        r.raise_for_status()
        return r.json().get("events", [])
    except Exception as e:
        print(f"  Kalshi events error for {series}: {e}", file=sys.stderr)
        return []


def kalshi_markets_for_event(event_ticker: str):
    """Fetch markets for a specific Kalshi event."""
    try:
        r = requests.get(f"{KALSHI_API}/markets", params={"event_ticker": event_ticker, "status": "open", "limit": 200}, timeout=15)
        r.raise_for_status()
        return r.json().get("markets", [])
    except Exception as e:
        print(f"  Kalshi markets error for {event_ticker}: {e}", file=sys.stderr)
        return []


def kalshi_all_series():
    """Fetch all Kalshi series."""
    cursor = None
    all_series = []
    for _ in range(20):
        params = {"limit": 100}
        if cursor:
            params["cursor"] = cursor
        try:
            r = requests.get(f"{KALSHI_API}/series", params=params, timeout=15)
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            print(f"  Kalshi series error: {e}", file=sys.stderr)
            break
        for s in data.get("series", []):
            all_series.append(s)
        cursor = data.get("cursor")
        if not cursor or not data.get("series"):
            break
        time.sleep(0.2)
    return all_series


def poly_search(text_query: str, limit=50):
    """Search Polymarket for markets matching text."""
    try:
        # Use the text search endpoint
        r = requests.get(f"{POLY_API}/markets",
                         params={"limit": limit, "active": "true", "closed": "false"},
                         timeout=15)
        r.raise_for_status()
        data = r.json()
        query_lower = text_query.lower()
        results = []
        for m in data:
            q = (m.get("question") or "").lower()
            slug = (m.get("slug") or "").lower()
            if query_lower in q or query_lower in slug:
                results.append(m)
        return results
    except Exception as e:
        print(f"  Poly search error for '{text_query}': {e}", file=sys.stderr)
        return []


def poly_fetch_all(max_pages=20):
    """Fetch all active Polymarket markets."""
    all_markets = []
    offset = 0
    for page in range(max_pages):
        try:
            r = requests.get(f"{POLY_API}/markets",
                             params={"limit": 100, "offset": offset, "active": "true", "closed": "false", "order": "volume", "ascending": "false"},
                             timeout=15)
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            print(f"  Poly page {page} error: {e}", file=sys.stderr)
            break
        if not data:
            break
        all_markets.extend(data)
        if len(data) < 100:
            break
        offset += 100
        time.sleep(0.2)
    return all_markets


def normalize(text):
    return re.sub(r"[\s\W_]+", " ", (text or "").lower()).strip()


def word_overlap_score(a, b):
    """Score word overlap between two strings, excluding stopwords."""
    stop = {"will", "the", "in", "on", "a", "be", "of", "to", "and", "or", "is", "by", "before", "after", "this", "that", "for"}
    wa = set(normalize(a).split()) - stop
    wb = set(normalize(b).split()) - stop
    if not wa or not wb:
        return 0.0
    common = wa & wb
    return len(common) / len(wa | wb)


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "scan"

    if mode == "series":
        # List all Kalshi series tickers and categories
        print("=== ALL KALSHI SERIES ===")
        series = kalshi_all_series()
        print(f"Total series: {len(series)}")
        cats = {}
        for s in series:
            cat = s.get("category", "?")
            cats.setdefault(cat, []).append(s.get("ticker", ""))
        for cat, tickers in sorted(cats.items()):
            print(f"\n{cat} ({len(tickers)}):")
            for t in sorted(tickers)[:20]:
                print(f"  {t}")
            if len(tickers) > 20:
                print(f"  ... and {len(tickers) - 20} more")

    elif mode == "crypto":
        # Scan Kalshi series for crypto
        print("=== KALSHI CRYPTO SERIES ===")
        for prefix in ["KXBTC", "KXETH", "KXSOL", "KXCRYPTO", "KXBITCOIN", "INXBTC", "INXETH", "INXSOL"]:
            events = kalshi_events_for_series(prefix)
            if events:
                print(f"\n{prefix}: {len(events)} events")
                for e in events[:10]:
                    et = e.get("event_ticker", "")
                    title = e.get("title", "")
                    print(f"  {et}: {title[:100]}")
                    mkts = kalshi_markets_for_event(et)
                    for m in mkts[:5]:
                        print(f"    {m['ticker']}: {m['title'][:80]} vol={m.get('volume',0)}")
            time.sleep(0.3)

        print("\n=== POLYMARKET CRYPTO (by search) ===")
        for query in ["bitcoin price", "btc price", "ethereum price", "solana price", "bitcoin above", "eth above"]:
            print(f"\nSearch: '{query}'")
            results = poly_search(query)
            for m in results[:5]:
                print(f"  {m.get('slug','')[:50]}: {m.get('question','')[:80]}")
            time.sleep(0.3)

    elif mode == "political":
        print("=== KALSHI POLITICAL SERIES ===")
        for prefix in ["PRES", "KXPRES", "KXGOV", "KXSEN", "KXHOUSE", "KXELECT", "KXPOT", "CONTROLH", "CONTROLS", "POTUS"]:
            events = kalshi_events_for_series(prefix)
            if events:
                print(f"\n{prefix}: {len(events)} events")
                for e in events[:10]:
                    et = e.get("event_ticker", "")
                    title = e.get("title", "")
                    print(f"  {et}: {title[:100]}")
                    mkts = kalshi_markets_for_event(et)
                    for m in mkts[:5]:
                        print(f"    {m['ticker']}: {m['title'][:80]}")
            time.sleep(0.3)

        print("\n=== POLYMARKET POLITICAL ===")
        for query in ["2028 president", "2028 election", "governor 2026", "midterm", "presidential nomination"]:
            print(f"\nSearch: '{query}'")
            results = poly_search(query)
            for m in results[:5]:
                print(f"  {m.get('slug','')[:50]}: {m.get('question','')[:80]}")
            time.sleep(0.3)

    elif mode == "weather":
        print("=== KALSHI WEATHER SERIES ===")
        for prefix in ["KXHURR", "KXTEMP", "KXWEATHER", "HIGHTEMP", "KXWILD", "KXSTORM"]:
            events = kalshi_events_for_series(prefix)
            if events:
                print(f"\n{prefix}: {len(events)} events")
                for e in events[:10]:
                    et = e.get("event_ticker", "")
                    title = e.get("title", "")
                    print(f"  {et}: {title[:100]}")
            time.sleep(0.3)

        print("\n=== POLYMARKET WEATHER ===")
        for query in ["hurricane", "temperature", "wildfire", "flood", "hottest"]:
            print(f"\nSearch: '{query}'")
            results = poly_search(query)
            for m in results[:5]:
                print(f"  {m.get('slug','')[:50]}: {m.get('question','')[:80]}")
            time.sleep(0.3)

    elif mode == "scan":
        # Full scan: get Polymarket markets and try to match against Kalshi
        print("=== FULL POLYMARKET SCAN (top by volume) ===")
        all_poly = poly_fetch_all(max_pages=10)
        print(f"Total Polymarket active: {len(all_poly)}")

        # Group by category-like features
        cats = {}
        for m in all_poly:
            q = m.get("question", "")
            # Classify
            ql = q.lower()
            if any(k in ql for k in ["bitcoin", "btc", "ethereum", "eth", "solana", "sol", "crypto"]):
                cat = "crypto"
            elif any(k in ql for k in ["president", "election", "governor", "senator", "congress", "midterm", "nominee", "trump", "harris"]):
                cat = "politics"
            elif any(k in ql for k in ["temperature", "hurricane", "weather", "wildfire", "flood", "storm", "drought"]):
                cat = "weather"
            elif any(k in ql for k in ["gdp", "inflation", "cpi", "unemployment", "fed ", "interest rate", "jobs report", "nonfarm"]):
                cat = "economics"
            else:
                cat = "other"
            cats.setdefault(cat, []).append(m)

        for cat, markets in sorted(cats.items()):
            print(f"\n--- {cat.upper()} ({len(markets)} markets) ---")
            for m in sorted(markets, key=lambda x: -float(x.get("volume", 0) or 0))[:15]:
                print(f"  [{float(m.get('volume',0) or 0):>12,.0f}] {m.get('slug','')[:40]:40s} | {m.get('question','')[:70]}")
