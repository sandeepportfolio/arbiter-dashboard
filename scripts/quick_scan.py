#!/usr/bin/env python3
"""Quick scan of Kalshi and Polymarket for matching markets."""
import requests
import json
import sys
import time
import re

KALSHI_API = "https://api.elections.kalshi.com/trade-api/v2"
POLY_API = "https://gamma-api.polymarket.com"

def scan_kalshi(keywords, max_pages=30):
    """Scan Kalshi for markets matching any keyword."""
    results = []
    cursor = None
    for page in range(max_pages):
        params = {"limit": 200, "status": "open"}
        if cursor:
            params["cursor"] = cursor
        try:
            r = requests.get(f"{KALSHI_API}/markets", params=params, timeout=15)
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            print(f"Kalshi page {page} error: {e}", file=sys.stderr)
            break
        for m in data.get("markets", []):
            title = (m.get("title") or "").lower()
            ticker = (m.get("ticker") or "").lower()
            cat = (m.get("category") or "").lower()
            if any(kw in title or kw in ticker or kw in cat for kw in keywords):
                results.append({
                    "ticker": m.get("ticker", ""),
                    "title": m.get("title", ""),
                    "category": m.get("category", ""),
                    "event_ticker": m.get("event_ticker", ""),
                    "volume": m.get("volume", 0),
                })
        cursor = data.get("cursor")
        if not cursor or not data.get("markets"):
            break
        time.sleep(0.2)
    return results


def scan_polymarket(keywords, max_pages=10):
    """Scan Polymarket for markets matching any keyword."""
    results = []
    offset = 0
    for page in range(max_pages):
        try:
            r = requests.get(f"{POLY_API}/markets", params={"limit": 100, "offset": offset, "active": "true", "closed": "false"}, timeout=15)
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            print(f"Polymarket page {page} error: {e}", file=sys.stderr)
            break
        if not data:
            break
        for m in data:
            q = (m.get("question") or "").lower()
            slug = (m.get("slug") or "").lower()
            if any(kw in q or kw in slug for kw in keywords):
                results.append({
                    "slug": m.get("slug", ""),
                    "question": m.get("question", ""),
                    "conditionId": m.get("conditionId", ""),
                    "volume": m.get("volume", 0),
                })
        if len(data) < 100:
            break
        offset += 100
        time.sleep(0.2)
    return results


def extract_threshold(text):
    """Extract (asset, threshold) from crypto price text."""
    text_lower = text.lower()
    asset_map = {"bitcoin": "btc", "btc": "btc", "ethereum": "eth", "ether": "eth", "eth": "eth", "solana": "sol", "sol": "sol"}
    asset = None
    for kw, code in asset_map.items():
        if kw in text_lower:
            asset = code
            break
    if not asset:
        return None
    prices = re.findall(r"\$?([\d,]+(?:\.\d+)?)", text)
    for p in prices:
        try:
            val = float(p.replace(",", ""))
            if val >= 100:
                return (asset, val)
        except ValueError:
            continue
    return None


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "all"

    if mode in ("crypto", "all"):
        print("=== KALSHI CRYPTO ===")
        kc = scan_kalshi(["bitcoin", "btc", "ethereum", "eth", "solana", "sol"])
        print(f"Found {len(kc)} Kalshi crypto markets")
        for m in kc:
            thr = extract_threshold(m["title"])
            print(f"  {m['ticker']:40s} | {m['title'][:80]} | thr={thr}")

        print("\n=== POLYMARKET CRYPTO ===")
        pc = scan_polymarket(["bitcoin", "btc", "ethereum", "eth", "solana", "sol"])
        print(f"Found {len(pc)} Polymarket crypto markets")
        for m in pc:
            thr = extract_threshold(m["question"])
            print(f"  {m['slug'][:40]:40s} | {m['question'][:80]} | thr={thr}")

        # Match by threshold
        print("\n=== CRYPTO MATCHES ===")
        k_by_thr = {}
        for m in kc:
            thr = extract_threshold(m["title"])
            if thr:
                k_by_thr.setdefault(thr, []).append(m)
        for m in pc:
            thr = extract_threshold(m["question"])
            if thr and thr in k_by_thr:
                for km in k_by_thr[thr]:
                    print(f"  MATCH: {thr[0].upper()} ${thr[1]:,.0f}")
                    print(f"    K: {km['ticker']} — {km['title'][:80]}")
                    print(f"    P: {m['slug']} — {m['question'][:80]}")

    if mode in ("political", "all"):
        print("\n=== KALSHI POLITICAL ===")
        kp = scan_kalshi(["president", "2028", "governor", "mayor", "election", "trump", "nominee", "primary", "congress"])
        print(f"Found {len(kp)} Kalshi political markets")
        for m in kp[:30]:
            print(f"  {m['ticker']:40s} | {m['title'][:80]}")

        print("\n=== POLYMARKET POLITICAL ===")
        pp = scan_polymarket(["president", "2028", "governor", "mayor", "election", "trump", "nominee", "primary", "congress"])
        print(f"Found {len(pp)} Polymarket political markets")
        for m in pp[:30]:
            print(f"  {m['slug'][:40]:40s} | {m['question'][:80]}")

    if mode in ("weather", "all"):
        print("\n=== KALSHI WEATHER ===")
        kw = scan_kalshi(["temperature", "hurricane", "storm", "weather", "hottest", "warmest", "rainfall", "heat", "wildfire", "flood"])
        print(f"Found {len(kw)} Kalshi weather markets")
        for m in kw[:30]:
            print(f"  {m['ticker']:40s} | {m['title'][:80]}")

        print("\n=== POLYMARKET WEATHER ===")
        pw = scan_polymarket(["temperature", "hurricane", "storm", "weather", "hottest", "warmest", "rainfall", "heat", "wildfire", "flood"])
        print(f"Found {len(pw)} Polymarket weather markets")
        for m in pw[:30]:
            print(f"  {m['slug'][:40]:40s} | {m['question'][:80]}")
