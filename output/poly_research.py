"""Phase 4 Polymarket market research — read-only public API.

Goal: find two token_ids:
  1. happy: price <= $0.20, min_order_size * price <= $5, depth >= 5 contracts at ask
  2. fok: thin book — depth at target price < qty (intentional reject scenario)

Both must satisfy qty * price <= $5 (PHASE4_MAX_ORDER_USD hardlock).
"""

import json
import sys
import time
from datetime import datetime, timezone
from urllib.request import urlopen, Request
from urllib.error import HTTPError, URLError

OUT_DIR = r"C:\Users\sande\Documents\arbiter-dashboard\output"


def http_get(url, timeout=20):
    req = Request(url, headers={"User-Agent": "arbiter-phase4-research/1.0"})
    with urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def fetch_gamma_markets(limit=500, offset=0):
    """Gamma API — richer metadata (categories, volume, tags)."""
    url = (
        f"https://gamma-api.polymarket.com/markets"
        f"?active=true&closed=false&archived=false"
        f"&limit={limit}&offset={offset}&order=volume&ascending=false"
    )
    return http_get(url)


def fetch_clob_book(token_id):
    url = f"https://clob.polymarket.com/book?token_id={token_id}"
    return http_get(url)


def main():
    print("Fetching Gamma markets (active, by volume desc)...", flush=True)
    all_markets = []
    for offset in (0, 500, 1000):
        try:
            page = fetch_gamma_markets(limit=500, offset=offset)
            if not isinstance(page, list):
                print(f"  offset={offset}: unexpected shape: {type(page)}", flush=True)
                break
            all_markets.extend(page)
            print(f"  offset={offset}: got {len(page)} markets (cumulative {len(all_markets)})", flush=True)
            if len(page) < 500:
                break
        except Exception as e:
            print(f"  offset={offset}: error {e}", flush=True)
            break

    print(f"Total active markets fetched: {len(all_markets)}", flush=True)
    # dump slim index
    slim = []
    for m in all_markets:
        slim.append({
            "slug": m.get("slug"),
            "question": m.get("question"),
            "category": m.get("category"),
            "volume": m.get("volume"),
            "liquidity": m.get("liquidity"),
            "volume24hr": m.get("volume24hr"),
            "outcomes": m.get("outcomes"),
            "outcomePrices": m.get("outcomePrices"),
            "clobTokenIds": m.get("clobTokenIds"),
            "orderMinSize": m.get("orderMinSize"),
            "endDate": m.get("endDate"),
            "active": m.get("active"),
            "closed": m.get("closed"),
            "acceptingOrders": m.get("acceptingOrders"),
        })
    with open(f"{OUT_DIR}\\poly_markets_slim.json", "w", encoding="utf-8") as f:
        json.dump(slim, f, indent=2, default=str)
    print(f"Wrote slim index: {len(slim)} rows", flush=True)

    # Filter for happy-path candidates: need active + acceptingOrders + token_ids + low price
    candidates_happy = []
    for m in slim:
        if not m["active"] or m["closed"]:
            continue
        if m.get("acceptingOrders") is False:
            continue
        prices_raw = m.get("outcomePrices")
        tokens_raw = m.get("clobTokenIds")
        if not prices_raw or not tokens_raw:
            continue
        try:
            prices = json.loads(prices_raw) if isinstance(prices_raw, str) else prices_raw
            tokens = json.loads(tokens_raw) if isinstance(tokens_raw, str) else tokens_raw
        except Exception:
            continue
        if len(prices) != 2 or len(tokens) != 2:
            continue
        # Each outcome has its own price; find ones <= 0.20
        for idx, (p_str, tok) in enumerate(zip(prices, tokens)):
            try:
                p = float(p_str)
            except Exception:
                continue
            if 0.02 <= p <= 0.20:
                candidates_happy.append({
                    **m,
                    "outcome_idx": idx,
                    "token_id": tok,
                    "price": p,
                })
    # sort by liquidity desc
    def liq_key(x):
        try:
            return float(x.get("liquidity") or 0)
        except Exception:
            return 0
    candidates_happy.sort(key=liq_key, reverse=True)
    print(f"Happy-path candidates (price<=0.20, active, accepting): {len(candidates_happy)}", flush=True)
    with open(f"{OUT_DIR}\\poly_happy_candidates.json", "w", encoding="utf-8") as f:
        json.dump(candidates_happy[:100], f, indent=2, default=str)

    # Thin-book candidates: very low liquidity/volume, ideally active
    candidates_fok = []
    for m in slim:
        if not m["active"] or m["closed"]:
            continue
        if m.get("acceptingOrders") is False:
            continue
        tokens_raw = m.get("clobTokenIds")
        prices_raw = m.get("outcomePrices")
        if not tokens_raw or not prices_raw:
            continue
        try:
            liq = float(m.get("liquidity") or 0)
        except Exception:
            liq = 0
        # thin == liquidity present but small
        if 10 < liq < 500:
            candidates_fok.append(m)
    candidates_fok.sort(key=liq_key)
    print(f"Thin-book candidates: {len(candidates_fok)}", flush=True)
    with open(f"{OUT_DIR}\\poly_fok_candidates.json", "w", encoding="utf-8") as f:
        json.dump(candidates_fok[:100], f, indent=2, default=str)


if __name__ == "__main__":
    main()
