#!/usr/bin/env python3
"""
Fetch and match sports championship markets between Kalshi and Polymarket.

Kalshi: Uses pre-fetched sports events from /tmp/kalshi_sports_events.json
        then fetches individual markets per event (rate-limited 1.5s/req).
Polymarket: Concurrent paginated /events endpoint (~11k events).
"""
import asyncio
import aiohttp
import json
import re
import difflib
import sys
from datetime import datetime, timezone
from pathlib import Path

POLY_SEM = asyncio.Semaphore(5)
KALSHI_DELAY = 1.5  # seconds between Kalshi requests

# Championship/outcome focused keywords (exclude pure prop markets)
CHAMPIONSHIP_KEYWORDS = [
    # Major US sports championships
    "world series", "super bowl", "stanley cup", "nba finals", "nba champion",
    "nba championship", "nfl champion", "mlb champion", "nhl champion",
    "pro football champion", "pro basketball champion", "pro baseball champion",
    "pro hockey champion",
    # Conference/division winners
    "nfc champion", "afc champion", "nfc east", "nfc west", "nfc north", "nfc south",
    "afc east", "afc west", "afc north", "afc south",
    "division winner", "conference winner",
    # Awards (season outcome markets)
    "mvp", "cy young", "heisman", "coach of the year",
    "offensive player of the year", "defensive player of the year",
    "offensive rookie", "defensive rookie",
    "rookie of the year",
    # Soccer
    "premier league", "champions league", "la liga", "bundesliga", "serie a",
    "ligue 1", "fa cup", "copa del rey", "europa league", "conference league",
    "ballon d'or", "golden boot", "world cup",
    "euros", "euro 2026",
    # Tennis
    "wimbledon", "us open", "australian open", "french open", "grand slam",
    "roland garros", "wta", "atp",
    # Golf
    "masters", "pga championship", "the open", "ryder cup", "us open golf",
    # Combat
    "ufc", "heavyweight title", "welterweight title", "middleweight title",
    "lightweight title", "featherweight title", "flyweight title",
    "cruiserweight title", "bantamweight title",
    "wbc", "wba", "ibf",
    # Other
    "college football national", "college basketball champion",
    "playoff qualifiers", "playoff qualifier",
    "ryder cup", "scottie scheffler grand slam",
    # Kalshi-specific names
    "college football national championship",
    "women's college basketball champion",
    "men's college basketball champion",
    "college football playoff",
    "national championship",
]

SPORT_CATEGORIES = {
    "baseball_mlb": ["world series", "mlb champion", "pro baseball champion"],
    "football_nfl": ["super bowl", "nfl champion", "pro football champion", "nfc champion",
                     "afc champion", "nfc east", "nfc west", "nfc north", "nfc south",
                     "afc east", "afc west", "afc north", "afc south", "nfl mvp",
                     "offensive player of the year", "defensive player of the year",
                     "offensive rookie", "defensive rookie", "coach of the year nfl",
                     "nfl playoff"],
    "basketball_nba": ["nba finals", "nba champion", "nba championship", "pro basketball champion",
                       "nba mvp", "nba rookie", "eastern conference", "western conference"],
    "hockey_nhl": ["stanley cup", "nhl champion", "pro hockey champion", "canadian team",
                   "hart trophy", "vezina", "norris"],
    "soccer": ["premier league", "champions league", "la liga", "bundesliga", "serie a",
               "ligue 1", "fa cup", "world cup", "euros", "euro 2026", "copa del rey",
               "europa league", "conference league", "ballon d'or", "golden boot",
               "manchester"],
    "tennis": ["wimbledon", "us open tennis", "australian open", "french open", "grand slam",
               "roland garros", "wta", "atp"],
    "golf": ["masters", "pga championship", "the open", "ryder cup", "scottie scheffler"],
    "combat": ["ufc", "heavyweight title", "welterweight", "middleweight", "lightweight",
               "featherweight", "flyweight", "cruiserweight", "bantamweight", "wbc", "wba", "ibf"],
    "college": ["heisman", "college football national", "college basketball champion",
                "college football playoff", "national championship", "college football"],
    "other_sports": [],
}


def normalize(text: str) -> str:
    text = text.lower()
    text = re.sub(r"[^a-z0-9 ]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def is_championship(text: str) -> bool:
    n = normalize(text)
    return any(kw in n for kw in CHAMPIONSHIP_KEYWORDS)


def categorize(text: str) -> str:
    n = normalize(text)
    for cat, kws in SPORT_CATEGORIES.items():
        if any(kw in n for kw in kws):
            return cat
    return "other_sports"


def fuzzy(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, normalize(a), normalize(b)).ratio()


async def poly_get(session: aiohttp.ClientSession, url: str, params: dict = None):
    async with POLY_SEM:
        try:
            async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=15)) as r:
                if r.status == 200:
                    return await r.json()
        except Exception:
            pass
    return None


async def kalshi_get(session: aiohttp.ClientSession, url: str, params: dict = None):
    await asyncio.sleep(KALSHI_DELAY)
    try:
        async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=15)) as r:
            if r.status == 200:
                return await r.json()
            if r.status == 429:
                print("  Kalshi 429 – sleeping 60s")
                await asyncio.sleep(60)
    except Exception as e:
        print(f"  Kalshi error: {e}", file=sys.stderr)
    return None


# ── Kalshi ──────────────────────────────────────────────────────────────────

async def fetch_kalshi_sports(session: aiohttp.ClientSession) -> list[dict]:
    """Load cached sports events, fetch markets per event."""
    cache = Path("/tmp/kalshi_sports_events.json")
    if not cache.exists():
        print("ERROR: /tmp/kalshi_sports_events.json not found. Run pre-fetch first.")
        return []

    all_events = json.loads(cache.read_text())
    sports_events = [e for e in all_events if e.get("category") == "Sports"]
    print(f"  Loaded {len(sports_events)} Kalshi Sports events from cache")

    all_markets, seen = [], set()

    for ev in sports_events:
        ev_ticker = ev.get("event_ticker", "")
        ev_title = ev.get("title", "")
        if not ev_ticker:
            continue

        cursor = None
        ev_mkts = []
        while True:
            params = {"event_ticker": ev_ticker, "limit": 200}
            if cursor:
                params["cursor"] = cursor
            data = await kalshi_get(
                session,
                "https://api.elections.kalshi.com/trade-api/v2/markets",
                params,
            )
            if not data:
                break
            mkts = data.get("markets", [])
            ev_mkts.extend(mkts)
            cursor = data.get("cursor")
            if not cursor or len(mkts) < 200:
                break

        for m in ev_mkts:
            mid = m.get("ticker")
            if mid and mid not in seen:
                seen.add(mid)
                title = m.get("title", "")
                all_markets.append({
                    "id": mid,
                    "title": title,
                    "event_title": ev_title,
                    "full_title": f"{ev_title} - {title}" if title and title != ev_title else ev_title,
                    "yes_bid": m.get("yes_bid"),
                    "yes_ask": m.get("yes_ask"),
                    "no_bid": m.get("no_bid"),
                    "no_ask": m.get("no_ask"),
                    "volume": m.get("volume"),
                    "status": m.get("status"),
                    "close_time": m.get("close_time"),
                    "series_ticker": m.get("series_ticker", ""),
                    "source": "kalshi",
                })
        if ev_mkts:
            print(f"    {ev_title[:52]}: {len(ev_mkts)} mkts")

    print(f"  Kalshi: {len(all_markets)} markets total")
    return all_markets


# ── Polymarket ───────────────────────────────────────────────────────────────

async def poly_event_page(session: aiohttp.ClientSession, offset: int) -> list[dict]:
    data = await poly_get(
        session,
        "https://gamma-api.polymarket.com/events",
        {"closed": "false", "limit": 100, "offset": offset, "active": "true"},
    )
    if not data or not isinstance(data, list):
        return []
    out = []
    for ev in data:
        ev_title = ev.get("title", "")
        for m in ev.get("markets", []):
            mid = m.get("id") or m.get("conditionId")
            if not mid:
                continue
            q = m.get("question", "") or m.get("title", "")
            full = f"{ev_title} - {q}" if q and q != ev_title else (q or ev_title)
            prices = m.get("outcomePrices", [])
            out.append({
                "id": str(mid),
                "title": q or ev_title,
                "event_title": ev_title,
                "full_title": full,
                "slug": m.get("slug", "") or ev.get("slug", ""),
                "yes_price": prices[0] if prices else None,
                "no_price": prices[1] if len(prices) > 1 else None,
                "volume": m.get("volume"),
                "liquidity": m.get("liquidity") or ev.get("liquidity"),
                "active": m.get("active", False),
                "end_date": m.get("endDate"),
                "source": "polymarket",
            })
    return out


async def fetch_polymarket(session: aiohttp.ClientSession) -> list[dict]:
    offsets = list(range(0, 11300, 100))
    print(f"  Dispatching {len(offsets)} Polymarket pages (semaphore=5)...")
    results = await asyncio.gather(*[poly_event_page(session, o) for o in offsets])
    out, seen = [], set()
    for page in results:
        for m in page:
            if m["id"] not in seen:
                seen.add(m["id"])
                out.append(m)
    print(f"  Polymarket: {len(out)} markets")
    return out


# ── Matching ─────────────────────────────────────────────────────────────────

def find_matches(kalshi: list[dict], poly: list[dict]) -> list[dict]:
    matches = []
    for km in kalshi:
        best, pm_best = 0.0, None
        k = normalize(km["full_title"])
        for pm in poly:
            s = fuzzy(k, normalize(pm["full_title"]))
            if s > best:
                best, pm_best = s, pm
        if best >= 0.5 and pm_best:
            combined = km["full_title"] + " " + pm_best["full_title"]
            matches.append({
                "score": round(best, 3),
                "category": categorize(combined),
                "kalshi": {
                    "id": km["id"],
                    "title": km["full_title"],
                    "series": km.get("series_ticker", ""),
                    "yes_bid": km.get("yes_bid"),
                    "yes_ask": km.get("yes_ask"),
                    "volume": km.get("volume"),
                    "close_time": km.get("close_time"),
                },
                "polymarket": {
                    "id": pm_best["id"],
                    "title": pm_best["full_title"],
                    "slug": pm_best.get("slug", ""),
                    "yes_price": pm_best.get("yes_price"),
                    "no_price": pm_best.get("no_price"),
                    "volume": pm_best.get("volume"),
                    "end_date": pm_best.get("end_date"),
                },
            })
    matches.sort(key=lambda x: -x["score"])
    return matches


async def verify(session: aiohttp.ClientSession, m: dict) -> dict:
    k_id, p_id = m["kalshi"]["id"], m["polymarket"]["id"]

    async def k_check():
        await asyncio.sleep(KALSHI_DELAY)
        return await poly_get(session, f"https://api.elections.kalshi.com/trade-api/v2/markets/{k_id}")

    kd, pd = await asyncio.gather(
        k_check(),
        poly_get(session, f"https://gamma-api.polymarket.com/markets/{p_id}"),
    )
    mkt = (kd or {}).get("market", {})
    m["kalshi_verified"] = bool(mkt) and mkt.get("status") not in ("settled", "finalized", "resolved")
    if isinstance(pd, dict):
        m["polymarket_verified"] = bool(pd.get("active"))
    elif isinstance(pd, list) and pd:
        m["polymarket_verified"] = bool(pd[0].get("active"))
    else:
        m["polymarket_verified"] = False
    return m


# ── Main ─────────────────────────────────────────────────────────────────────

async def main():
    out_path = Path("/Users/rentamac/Documents/arbiter/data/sports_championships_v2.json")
    out_path.parent.mkdir(exist_ok=True)

    print("=== Fetching Polymarket events (concurrent) ===")
    async with aiohttp.ClientSession() as session:
        poly_all = await fetch_polymarket(session)

    poly_sports = [m for m in poly_all if is_championship(m["full_title"] + " " + m["event_title"])]
    print(f"  {len(poly_sports)} Polymarket championship markets")

    print("\n=== Fetching Kalshi Sports markets (rate-limited 1.5s/req) ===")
    async with aiohttp.ClientSession() as session:
        kalshi_all = await fetch_kalshi_sports(session)

    kalshi_sports = [m for m in kalshi_all if is_championship(m["full_title"])]
    print(f"  {len(kalshi_sports)} Kalshi championship markets")
    for m in kalshi_sports[:8]:
        print(f"    {m['full_title'][:80]}")

    print(f"\n=== Matching (threshold ≥ 0.5) ===")
    matches = find_matches(kalshi_sports, poly_sports)
    print(f"  {len(matches)} candidates")
    for m in matches[:15]:
        print(f"  [{m['score']:.2f}] K: {m['kalshi']['title'][:55]}")
        print(f"         P: {m['polymarket']['title'][:55]}")

    print("\n=== Verifying live endpoints ===")
    async with aiohttp.ClientSession() as session:
        verified = await asyncio.gather(*[verify(session, m) for m in matches])

    confirmed = [m for m in verified if m.get("kalshi_verified") and m.get("polymarket_verified")]
    print(f"  {len(confirmed)} verified active")

    by_cat: dict[str, list] = {}
    for m in confirmed:
        by_cat.setdefault(m["category"], []).append(m)

    print("\n=== Results by Category ===")
    for cat, items in sorted(by_cat.items(), key=lambda x: -len(x[1])):
        print(f"  {cat}: {len(items)}")
        for it in items[:3]:
            print(f"    [{it['score']:.2f}] K: {it['kalshi']['title'][:55]}")
            print(f"           P: {it['polymarket']['title'][:55]}")

    output = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "summary": {
            "kalshi_sports_events": 103,
            "kalshi_championship_markets": len(kalshi_sports),
            "polymarket_total_markets": len(poly_all),
            "polymarket_championship_markets": len(poly_sports),
            "candidate_matches": len(matches),
            "verified_matches": len(confirmed),
            "by_category": {cat: len(items) for cat, items in by_cat.items()},
        },
        "matches": confirmed,
        "all_kalshi_sports": kalshi_sports,
        "all_polymarket_sports": poly_sports,
    }

    out_path.write_text(json.dumps(output, indent=2))
    print(f"\nWrote {out_path}")
    print(f"\nDone: {len(confirmed)} verified cross-platform sports championship matches.")


if __name__ == "__main__":
    asyncio.run(main())
