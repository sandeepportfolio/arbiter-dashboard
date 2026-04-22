#!/usr/bin/env python3 -u
"""
Sports market mapping discovery v2.
Focuses on season-outcome / championship markets — best arbitrage pairs.
Strategy:
  - Polymarket: events API with tag_slug=sports
  - Kalshi: events API with known sports series tickers
  - Team-level fuzzy matching within aligned event types
"""

import json
import time
import re
import os
import sys
from urllib.request import urlopen, Request
from urllib.error import URLError, HTTPError
from collections import defaultdict

OUTPUT_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "data", "sports_mappings_v2.json")

# Known Kalshi sports series tickers (from API inspection)
# Each will be queried at:
#   /trade-api/v2/events?series_ticker=<ticker>&status=open&with_nested_markets=true
KALSHI_SPORTS_SERIES = [
    # NBA
    "KXNBA", "KXNBAEAST", "KXNBAWEST", "KXNBAMVP", "KXNBAROTY",
    "KXNBADPOY", "KXNBA1STPICK", "KXWNBA",
    # NFL
    "KXNFLSB", "KXNFLMVP", "KXNFLAFC", "KXNFLNFC",
    # MLB
    "KXMLBWS", "KXMLBMVP", "KXMLBCY", "KXMLBROTY", "KXMLBAL",
    "KXMLBALROTY", "KXMLBNLROTY", "KXMLBALCY", "KXMLBNLCY",
    # NHL
    "KXNHLSC", "KXNHLMVP", "KXNHLCAP", "KXNHLROTY",
    # Soccer
    "KXMWORLDCUP", "KXWWORLDCUP", "KXUCL", "KXEPL", "KXMLS",
    "KXLALIGA", "KXBUNDESLIGA", "KXSERIEA", "KXLIGUE1", "KXUCLADVANCE",
    # Tennis
    "KXUSOPEN", "KXWIMBLEDON", "KXAUSTRALIANOPEN", "KXFRENCHOPEN",
    "KXATPUSOPEN", "KXWTAUSOPEN", "KXATPWIMBLEDON", "KXWTAWIMBLEDON",
    # Golf
    "KXMASTERS", "KXPGACHAMP", "KXUSOPENGOLF", "KXOPENCHAMP",
    "KXPGAMASTERS",
    # UFC / Combat
    "KXUFC", "KXESPYUFC",
    # College
    "KXNCAABCHAMP", "KXNCAABMARCH", "KXNCAAF", "KXCFBPLAYOFF",
    "KXNCAAFCUSA",
    # F1 / Racing
    "KXF1", "KXNASCAR",
    # Olympics
    "KXOLYMPICS", "KXWOMENTION",
    # Award shows that overlap (sports awards)
    "KXNFLOPOY", "KXNFLDRPY", "KXNFLCOACH",
]


def p(msg):
    print(msg, flush=True)


def fetch_json(url, max_retries=3):
    for attempt in range(max_retries):
        try:
            req = Request(url)
            req.add_header("User-Agent", "arbiter-market-discovery/2.0")
            with urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode())
        except HTTPError as e:
            if e.code == 429:
                wait = 5 * (attempt + 1)
                p(f"  [rate limit] waiting {wait}s...")
                time.sleep(wait)
            else:
                p(f"  [http err {e.code}] {url[:70]}")
                break
        except URLError as e:
            p(f"  [url err] {url[:70]}: {e}")
            if attempt < max_retries - 1:
                time.sleep(2 ** attempt)
    return None


# ─── Polymarket ───────────────────────────────────────────────────────────────

def fetch_polymarket_sports_events():
    """Paginate Polymarket events with tag_slug=sports."""
    p("Fetching Polymarket sports events...")
    all_events = []
    seen_ids = set()
    offset = 0
    limit = 100

    while True:
        url = (f"https://gamma-api.polymarket.com/events"
               f"?closed=false&limit={limit}&offset={offset}&tag_slug=sports")
        data = fetch_json(url)
        if not data:
            break
        events = data if isinstance(data, list) else []
        if not events:
            break

        new = 0
        for e in events:
            eid = e.get("id") or e.get("slug", "")
            if eid not in seen_ids:
                seen_ids.add(eid)
                all_events.append(e)
                new += 1

        p(f"  offset={offset}: {len(events)} events (+{new} new, total: {len(all_events)})")
        if len(events) < limit:
            break
        offset += limit
        time.sleep(0.2)

    p(f"Total Polymarket sports events: {len(all_events)}")
    return all_events


def extract_poly_markets(events):
    """Flatten Polymarket events → individual team/player markets."""
    markets = []
    for event in events:
        event_title = event.get("title", "") or ""
        event_slug = event.get("slug", "") or ""
        for m in (event.get("markets") or []):
            question = m.get("question", "") or ""
            markets.append({
                "slug": m.get("slug", "") or "",
                "condition_id": m.get("conditionId", "") or "",
                "question": question,
                "event_title": event_title,
                "event_slug": event_slug,
                "end_date": m.get("endDate", "") or "",
                "volume": float(m.get("volumeNum") or 0),
                "liquidity": float(m.get("liquidityNum") or 0),
                "yes_price": _parse_price(m.get("outcomePrices", "")),
            })
    p(f"Extracted {len(markets)} Polymarket markets from {len(events)} events")
    return markets


def _parse_price(prices_str):
    try:
        prices = json.loads(prices_str)
        return float(prices[0]) if prices else 0.5
    except Exception:
        return 0.5


# ─── Kalshi ───────────────────────────────────────────────────────────────────

def fetch_kalshi_sports_events():
    """Fetch Kalshi sports events by known series tickers."""
    p("\nFetching Kalshi sports events...")
    all_events = []
    seen_tickers = set()

    for series_ticker in KALSHI_SPORTS_SERIES:
        url = (f"https://api.elections.kalshi.com/trade-api/v2/events"
               f"?status=open&series_ticker={series_ticker}"
               f"&with_nested_markets=true&limit=50")
        data = fetch_json(url)
        if not data:
            time.sleep(0.5)
            continue

        events = data.get("events", [])
        added = 0
        for e in events:
            ticker = e.get("event_ticker", "")
            if ticker not in seen_tickers:
                seen_tickers.add(ticker)
                all_events.append(e)
                added += 1

        if added:
            p(f"  {series_ticker}: {added} events (total: {len(all_events)})")
        time.sleep(0.3)  # be polite to Kalshi API

    p(f"Total Kalshi sports events: {len(all_events)}")
    return all_events


def extract_kalshi_markets(events):
    """Flatten Kalshi events → individual team/player markets."""
    markets = []
    for event in events:
        event_ticker = event.get("event_ticker", "") or ""
        event_title = event.get("title", "") or ""
        for m in (event.get("markets") or []):
            ticker = m.get("ticker", "") or ""
            title = m.get("title", "") or ""
            markets.append({
                "ticker": ticker,
                "title": title,
                "event_ticker": event_ticker,
                "event_title": event_title,
                "close_time": m.get("close_time", "") or "",
                "yes_bid": m.get("yes_bid") or 0,
                "yes_ask": m.get("yes_ask") or 0,
                "volume": m.get("volume") or 0,
            })
    p(f"Extracted {len(markets)} Kalshi markets from {len(events)} events")
    return markets


# ─── Matching ─────────────────────────────────────────────────────────────────

STOP_WORDS = {
    "the", "a", "an", "at", "in", "on", "for", "of", "to", "is", "will",
    "be", "by", "or", "and", "win", "who", "what", "when", "how", "many",
    "this", "that", "their", "its", "which", "have", "has", "had", "been",
    "with", "from", "into", "than", "it", "no", "not", "2025", "2026",
    "vs", "does", "would", "could", "should",
}

ALIASES = {
    "oklahoma city thunder": "thunder",
    "golden state warriors": "warriors",
    "los angeles lakers": "lakers",
    "los angeles clippers": "clippers",
    "new york knicks": "knicks",
    "new orleans pelicans": "pelicans",
    "portland trail blazers": "trail blazers",
    "minnesota timberwolves": "timberwolves",
    "philadelphia 76ers": "76ers",
    "san antonio spurs": "spurs",
    "san francisco 49ers": "49ers",
    "green bay packers": "packers",
    "kansas city chiefs": "chiefs",
    "new england patriots": "patriots",
    "los angeles rams": "rams",
    "los angeles chargers": "chargers",
    "washington commanders": "commanders",
    "toronto maple leafs": "maple leafs",
    "montreal canadiens": "canadiens",
    "new york rangers": "rangers",
    "vegas golden knights": "golden knights",
    "las vegas golden knights": "golden knights",
    "manchester city fc": "manchester city",
    "manchester united fc": "manchester united",
    "fc barcelona": "barcelona",
    "atletico madrid": "atletico",
    "paris saint-germain": "psg",
    "pro basketball": "nba",
    "pro football": "nfl",
    "pro baseball": "mlb",
    "pro hockey": "nhl",
}

EVENT_TYPE_MAP = {
    "nba_champion": ["nba", "basketball final", "basketball champion", "pro basketball champion",
                     "pro basketball finals"],
    "nfl_superbowl": ["super bowl", "nfl champion", "pro football champion"],
    "mlb_worldseries": ["world series", "mlb champion", "pro baseball champion"],
    "nhl_stanleycup": ["stanley cup", "nhl champion", "pro hockey champion"],
    "epl_winner": ["premier league", "epl"],
    "ucl_winner": ["champions league", "ucl"],
    "laliga_winner": ["la liga", "laliga"],
    "bundesliga_winner": ["bundesliga"],
    "seriea_winner": ["serie a"],
    "ligue1_winner": ["ligue 1", "ligue1"],
    "worldcup_winner": ["world cup", "worldcup", "fifa world cup"],
    "wimbledon": ["wimbledon"],
    "us_open_tennis": ["us open tennis", "us open"],
    "australian_open": ["australian open"],
    "french_open": ["french open", "roland garros"],
    "golf_major": ["masters", "pga championship", "us open golf", "open championship"],
    "ufc_title": ["ufc", "mma"],
    "nba_mvp": ["nba mvp", "basketball mvp"],
    "nfl_mvp": ["nfl mvp", "football mvp"],
    "mlb_mvp": ["mlb mvp", "baseball mvp"],
    "nba_roty": ["nba rookie", "basketball rookie"],
    "nba_east": ["eastern conference", "nba eastern"],
    "nba_west": ["western conference", "nba western"],
    "ncaa_basketball": ["ncaa", "march madness", "college basketball champion"],
    "ncaa_football": ["college football playoff", "cfb"],
    "mls_cup": ["mls"],
}


def classify_event_type(text):
    tl = text.lower()
    for etype, keywords in EVENT_TYPE_MAP.items():
        if any(k in tl for k in keywords):
            return etype
    return "other"


def normalize(text):
    text = text.lower()
    for old, new in ALIASES.items():
        text = text.replace(old, new)
    text = re.sub(r"[^\w\s]", " ", text)
    text = re.sub(r"\b20\d\d\b", "", text)  # remove years for base matching
    return text


def tokenize(text):
    tokens = re.split(r"\s+", normalize(text).strip())
    return set(t for t in tokens if len(t) > 1 and t not in STOP_WORDS)


def jaccard(a, b):
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def match_markets(kalshi_markets, poly_markets, threshold=0.3):
    """Match each Kalshi market to its best Polymarket counterpart."""
    p(f"\nMatching {len(kalshi_markets)} Kalshi vs {len(poly_markets)} Polymarket markets...")

    # Build event-type groups for poly
    poly_by_et = defaultdict(list)
    for pm in poly_markets:
        et = classify_event_type(pm["event_title"] + " " + pm["question"])
        poly_by_et[et].append(pm)

    matches = []
    no_match = 0

    for i, km in enumerate(kalshi_markets):
        if i % 50 == 0:
            p(f"  {i}/{len(kalshi_markets)} processed, {len(matches)} matches...")

        k_combined = km["title"] + " " + km["event_title"]
        et = classify_event_type(k_combined)
        k_tokens = tokenize(k_combined)

        # Search within event type first, then fall back to all
        candidates = poly_by_et.get(et, [])
        if len(candidates) < 5:
            candidates = poly_markets

        best_score = 0.0
        best_pm = None

        for pm in candidates:
            p_combined = pm["question"] + " " + pm["event_title"]
            p_tokens = tokenize(p_combined)
            score = jaccard(k_tokens, p_tokens)

            pm_et = classify_event_type(p_combined)
            if et == pm_et and et != "other":
                score += 0.15  # same event type bonus

            # Year match bonus
            ky = set(re.findall(r"\b(202\d)\b", k_combined))
            py = set(re.findall(r"\b(202\d)\b", p_combined))
            if ky and py and ky & py:
                score += 0.05

            score = min(1.0, score)
            if score > best_score:
                best_score = score
                best_pm = pm

        if best_score >= threshold and best_pm:
            matches.append({
                "kalshi_ticker": km["ticker"],
                "polymarket_slug": best_pm["slug"],
                "polymarket_condition_id": best_pm["condition_id"],
                "kalshi_title": km["title"],
                "polymarket_title": best_pm["question"],
                "similarity_score": round(best_score, 4),
                "category": "sports",
                "sport": et,
                "kalshi_event": km["event_ticker"],
                "kalshi_event_title": km["event_title"],
                "polymarket_event_title": best_pm["event_title"],
                "kalshi_close_time": km["close_time"],
                "polymarket_end_date": best_pm["end_date"],
                "kalshi_volume": km["volume"],
                "polymarket_volume": best_pm["volume"],
                "polymarket_liquidity": best_pm["liquidity"],
                "polymarket_yes_price": best_pm["yes_price"],
            })
        else:
            no_match += 1

    p(f"  Done. Matches: {len(matches)}, No match: {no_match}")
    return sorted(matches, key=lambda x: x["similarity_score"], reverse=True)


def dedup(matches):
    """Keep only the best Kalshi match per Polymarket market."""
    best = {}
    for m in matches:
        key = m["polymarket_condition_id"] or m["polymarket_slug"]
        if key not in best or m["similarity_score"] > best[key]["similarity_score"]:
            best[key] = m
    result = sorted(best.values(), key=lambda x: x["similarity_score"], reverse=True)
    p(f"After dedup: {len(result)} matches (was {len(matches)})")
    return result


def filter_cross_sport_errors(matches):
    """Remove obvious cross-sport false positives."""
    # Basketball/Hockey conference cross-matches
    # NBA East/West should NOT match NHL East/West
    NBA_BASKETBALL_TOKENS = {"nba", "basketball", "bulls", "celtics", "lakers", "knicks",
                              "heat", "bucks", "nets", "hawks", "pacers", "cavaliers",
                              "pistons", "raptors", "magic", "wizards", "hornets", "76ers",
                              "sixers", "thunder", "spurs", "mavericks", "suns", "clippers",
                              "warriors", "nuggets", "timberwolves", "jazz", "trail", "blazers",
                              "pelicans", "grizzlies", "rockets", "kings", "portland"}
    NHL_HOCKEY_TOKENS = {"nhl", "hockey", "bruins", "islanders", "rangers", "penguins",
                          "capitals", "hurricanes", "flyers", "devils", "senators",
                          "maple leafs", "canadiens", "sabres", "red wings", "blackhawks",
                          "blues", "predators", "wild", "stars", "avalanche", "coyotes",
                          "flames", "oilers", "canucks", "sharks", "ducks", "kings",
                          "golden knights", "kraken", "lightning", "panthers"}

    # Soccer vs basketball "serie a"
    SOCCER_TEAMS = {"napoli", "naples", "juventus", "inter", "milan", "roma", "atalanta",
                    "lazio", "fiorentina", "torino", "bologna", "udinese", "genoa",
                    "verona", "empoli", "lecce", "cagliari", "frosinone", "salernitana",
                    "monza", "parma", "venezia", "como", "cremonese"}

    filtered = []
    removed = 0

    for m in matches:
        ks = m["sport"]
        k_title_lower = m["kalshi_title"].lower()
        p_title_lower = m["polymarket_title"].lower()
        p_event_lower = m["polymarket_event_title"].lower()

        # NBA conference matching NHL conference
        if ks in ("nba_east", "nba_west"):
            # Check if polymarket market is actually hockey
            if any(tok in p_title_lower or tok in p_event_lower for tok in
                   ["nhl", "hockey", "stanley", "islanders", "bruins", "penguins",
                    "capitals", "canadiens"]):
                removed += 1
                continue

        # Soccer Serie A matching basketball Serie A
        if ks == "seriea_winner":
            p_combined = p_title_lower + " " + p_event_lower
            if "basketball" in p_combined:
                # Unless the Kalshi market is also basketball
                if "basketball" not in k_title_lower:
                    removed += 1
                    continue

        filtered.append(m)

    p(f"After cross-sport filter: {len(filtered)} matches (removed {removed} false positives)")
    return filtered


def spot_check(matches, n=15):
    p(f"\n=== TOP {n} MATCHES ===")
    for i, m in enumerate(matches[:n]):
        p(f"\n{i+1}. Score={m['similarity_score']:.3f} | sport={m['sport']}")
        p(f"   Kalshi: [{m['kalshi_ticker']}]")
        p(f"          {m['kalshi_title'][:80]}")
        p(f"          event: {m['kalshi_event_title'][:60]}")
        p(f"   Poly:   [{m['polymarket_slug'][:45]}]")
        p(f"          {m['polymarket_title'][:80]}")
        p(f"          event: {m['polymarket_event_title'][:60]}")


def main():
    poly_events = fetch_polymarket_sports_events()
    poly_markets = extract_poly_markets(poly_events)

    kalshi_events = fetch_kalshi_sports_events()
    kalshi_markets = extract_kalshi_markets(kalshi_events)

    if not poly_markets:
        p("ERROR: No Polymarket markets — aborting")
        sys.exit(1)
    if not kalshi_markets:
        p("ERROR: No Kalshi markets — aborting")
        sys.exit(1)

    raw_matches = match_markets(kalshi_markets, poly_markets, threshold=0.3)
    deduped = dedup(raw_matches)
    matches = filter_cross_sport_errors(deduped)

    spot_check(matches)

    p("\n=== STATS BY SPORT ===")
    sport_counts = defaultdict(int)
    for m in matches:
        sport_counts[m["sport"]] += 1
    for sport, cnt in sorted(sport_counts.items(), key=lambda x: -x[1]):
        p(f"  {sport}: {cnt}")

    total = len(matches)
    high = sum(1 for m in matches if m["similarity_score"] >= 0.5)
    very_high = sum(1 for m in matches if m["similarity_score"] >= 0.7)
    p(f"\nTotal matches (>= 0.30):  {total}")
    p(f"High confidence (>= 0.50): {high}")
    p(f"Very high (>= 0.70):       {very_high}")

    os.makedirs(os.path.dirname(OUTPUT_FILE), exist_ok=True)
    with open(OUTPUT_FILE, "w") as f:
        json.dump(matches, f, indent=2)
    p(f"\nSaved {total} mappings → {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
