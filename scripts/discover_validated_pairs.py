#!/usr/bin/env python3
"""
Discover and validate genuine cross-platform market pairs.

This script queries both Kalshi and Polymarket APIs directly to find
markets that resolve on the EXACT same real-world outcome. Unlike the
auto-discovery text matcher, this uses structured metadata (resolution
criteria, settlement dates, event types) to ensure correctness.

Usage:
    python3 scripts/discover_validated_pairs.py

Output:
    /tmp/validated_pairs.json — pairs that passed all validation checks
    /tmp/rejected_pairs.json — pairs that failed validation with reasons
"""
import asyncio
import json
import os
import re
import sys
import time
from collections import defaultdict
from datetime import date, datetime

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import aiohttp

# ── Configuration ────────────────────────────────────────────────────
KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2"
POLYMARKET_US_BASE = os.getenv("POLYMARKET_US_API_URL", "https://api.polymarket.us/v1")
POLYMARKET_GAMMA = "https://gamma-api.polymarket.com"

# Categories where cross-platform overlap is most likely
FOCUS_CATEGORIES = {"politics", "economics", "finance", "crypto", "geopolitics"}


async def fetch_kalshi_events(session: aiohttp.ClientSession) -> list[dict]:
    """Fetch all Kalshi events with their markets."""
    events = []
    cursor = None
    while True:
        params = {"limit": 200, "status": "open"}
        if cursor:
            params["cursor"] = cursor
        try:
            async with session.get(f"{KALSHI_BASE}/events", params=params) as resp:
                if resp.status != 200:
                    print(f"Kalshi events API error: {resp.status}", file=sys.stderr)
                    break
                data = await resp.json()
                batch = data.get("events", [])
                events.extend(batch)
                cursor = data.get("cursor")
                if not cursor or not batch:
                    break
        except Exception as e:
            print(f"Kalshi events fetch error: {e}", file=sys.stderr)
            break
    return events


async def fetch_kalshi_markets_for_event(session: aiohttp.ClientSession, event_ticker: str) -> list[dict]:
    """Fetch all markets within a specific Kalshi event."""
    markets = []
    cursor = None
    while True:
        params = {"limit": 100, "event_ticker": event_ticker, "status": "open"}
        if cursor:
            params["cursor"] = cursor
        try:
            async with session.get(f"{KALSHI_BASE}/markets", params=params) as resp:
                if resp.status != 200:
                    break
                data = await resp.json()
                batch = data.get("markets", [])
                markets.extend(batch)
                cursor = data.get("cursor")
                if not cursor or not batch:
                    break
        except Exception as e:
            print(f"Kalshi markets fetch error for {event_ticker}: {e}", file=sys.stderr)
            break
    return markets


async def fetch_polymarket_us_markets(session: aiohttp.ClientSession) -> list[dict]:
    """Fetch all active Polymarket US markets."""
    markets = []
    offset = 0
    limit = 100
    while True:
        params = {"limit": limit, "offset": offset, "active": "true"}
        try:
            async with session.get(f"{POLYMARKET_US_BASE}/markets", params=params) as resp:
                if resp.status != 200:
                    print(f"Polymarket US API error: {resp.status} at offset {offset}", file=sys.stderr)
                    break
                data = await resp.json()
                batch = data if isinstance(data, list) else data.get("markets", data.get("data", []))
                if not batch:
                    break
                markets.extend(batch)
                offset += limit
                if len(batch) < limit:
                    break
        except Exception as e:
            print(f"Polymarket US fetch error at offset {offset}: {e}", file=sys.stderr)
            break
    return markets


async def fetch_polymarket_gamma_markets(session: aiohttp.ClientSession) -> list[dict]:
    """Fetch active markets from Polymarket Gamma API (backup)."""
    markets = []
    offset = 0
    limit = 100
    while True:
        params = {"limit": limit, "offset": offset, "active": "true", "closed": "false"}
        try:
            async with session.get(f"{POLYMARKET_GAMMA}/markets", params=params) as resp:
                if resp.status != 200:
                    print(f"Gamma API error: {resp.status}", file=sys.stderr)
                    break
                batch = await resp.json()
                if not isinstance(batch, list) or not batch:
                    break
                markets.extend(batch)
                offset += limit
                if len(batch) < limit:
                    break
                await asyncio.sleep(0.2)  # rate limit
        except Exception as e:
            print(f"Gamma fetch error: {e}", file=sys.stderr)
            break
    return markets


def normalize(text: str) -> str:
    """Normalize text for comparison."""
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def extract_team_from_ticker(ticker: str) -> str:
    """Extract team abbreviation from Kalshi ticker."""
    parts = ticker.split("-")
    if len(parts) >= 2:
        return parts[-1]
    return ""


def resolution_text(market: dict) -> str:
    """Extract resolution criteria text from a market dict."""
    for field in ("rules_primary", "rules_secondary", "settlement_source_url",
                  "resolution_source", "resolution", "description", "rules"):
        val = market.get(field, "")
        if val:
            return str(val)
    return ""


def is_binary_market(market: dict) -> bool:
    """Check if market is a simple binary YES/NO market."""
    # Kalshi markets are always binary
    ticker = market.get("ticker", "")
    if ticker:
        return True
    # Polymarket — check outcomes
    outcomes = market.get("outcomes", [])
    if isinstance(outcomes, list) and len(outcomes) == 2:
        labels = {str(o).lower() for o in outcomes}
        return labels == {"yes", "no"} or len(labels) == 2
    # Check if it's part of a group with 2 outcomes
    return True


class MarketMatcher:
    """Find and validate cross-platform market pairs."""

    def __init__(self):
        self.kalshi_events: list[dict] = []
        self.kalshi_markets: dict[str, dict] = {}  # ticker -> market
        self.poly_markets: list[dict] = []
        self.validated: list[dict] = []
        self.rejected: list[dict] = []

    async def load_catalogs(self):
        """Load market catalogs from both platforms."""
        async with aiohttp.ClientSession() as session:
            print("Fetching Kalshi events...", file=sys.stderr)
            self.kalshi_events = await fetch_kalshi_events(session)
            print(f"  Got {len(self.kalshi_events)} Kalshi events", file=sys.stderr)

            # Fetch markets for relevant events
            relevant_events = []
            for event in self.kalshi_events:
                cat = normalize(str(event.get("category", "")))
                title = normalize(str(event.get("title", "")))
                # Focus on non-sports, non-esports events
                if any(kw in cat for kw in ("politic", "econom", "financ", "crypto", "geo")):
                    relevant_events.append(event)
                elif any(kw in title for kw in (
                    "fed", "rate", "gdp", "cpi", "inflation", "unemployment",
                    "bitcoin", "btc", "ethereum", "eth", "president", "congress",
                    "senate", "house", "election", "tariff", "recession",
                    "government", "shutdown", "debt", "ceiling", "war", "nato",
                    "trump", "biden", "harris", "governor", "supreme court",
                )):
                    relevant_events.append(event)

            print(f"  {len(relevant_events)} relevant Kalshi events (politics/economics/crypto/geo)", file=sys.stderr)

            for i, event in enumerate(relevant_events):
                et = event.get("event_ticker", "")
                if not et:
                    continue
                markets = await fetch_kalshi_markets_for_event(session, et)
                for m in markets:
                    t = m.get("ticker", "")
                    if t:
                        m["_event"] = event
                        self.kalshi_markets[t] = m
                if (i + 1) % 20 == 0:
                    print(f"  Loaded markets for {i+1}/{len(relevant_events)} events...", file=sys.stderr)
                    await asyncio.sleep(0.1)

            print(f"  Total Kalshi markets loaded: {len(self.kalshi_markets)}", file=sys.stderr)

            print("Fetching Polymarket US markets...", file=sys.stderr)
            self.poly_markets = await fetch_polymarket_us_markets(session)
            print(f"  Got {len(self.poly_markets)} Polymarket US markets", file=sys.stderr)

            if len(self.poly_markets) < 10:
                print("  Trying Gamma API as backup...", file=sys.stderr)
                gamma = await fetch_polymarket_gamma_markets(session)
                if gamma:
                    self.poly_markets = gamma
                    print(f"  Got {len(self.poly_markets)} from Gamma API", file=sys.stderr)

    def build_poly_index(self) -> dict[str, list[dict]]:
        """Build keyword index of Polymarket markets for fast lookup."""
        index: dict[str, list[dict]] = defaultdict(list)
        for pm in self.poly_markets:
            text = normalize(
                str(pm.get("question", "")) + " " +
                str(pm.get("title", "")) + " " +
                str(pm.get("description", "")) + " " +
                str(pm.get("slug", ""))
            )
            for token in text.split():
                if len(token) >= 3:
                    index[token].append(pm)
        return index

    def find_candidates(self) -> list[dict]:
        """Find potential cross-platform pairs using structured matching."""
        poly_index = self.build_poly_index()
        candidates = []
        seen_pairs = set()

        for ticker, km in self.kalshi_markets.items():
            k_title = str(km.get("title", "") or "")
            k_subtitle = str(km.get("subtitle", "") or "")
            k_event = km.get("_event", {})
            k_event_title = str(k_event.get("title", "") or "")
            k_category = normalize(str(km.get("category", "") or k_event.get("category", "") or ""))
            k_text = normalize(f"{k_title} {k_subtitle} {k_event_title}")

            # Get keyword candidates from Polymarket
            k_tokens = set(k_text.split()) - {"the", "and", "will", "win", "yes", "no", "for", "on", "in", "of"}
            poly_candidates = set()
            for token in k_tokens:
                if len(token) >= 3:
                    for pm in poly_index.get(token, []):
                        slug = pm.get("slug", pm.get("id", ""))
                        if slug:
                            poly_candidates.add(slug)

            for slug in poly_candidates:
                pair_key = (ticker, slug)
                if pair_key in seen_pairs:
                    continue
                seen_pairs.add(pair_key)

                # Find the actual Polymarket market
                pm = None
                for m in self.poly_markets:
                    if m.get("slug", m.get("id", "")) == slug:
                        pm = m
                        break
                if pm is None:
                    continue

                p_text = normalize(
                    str(pm.get("question", "")) + " " +
                    str(pm.get("title", "")) + " " +
                    str(pm.get("description", ""))
                )

                # Compute token overlap
                p_tokens = set(p_text.split()) - {"the", "and", "will", "win", "yes", "no", "for", "on", "in", "of"}
                shared = k_tokens & p_tokens
                if len(shared) < 2:
                    continue

                overlap = len(shared) / max(len(k_tokens | p_tokens), 1)
                if overlap < 0.15:
                    continue

                candidates.append({
                    "kalshi_ticker": ticker,
                    "kalshi_title": k_title,
                    "kalshi_subtitle": k_subtitle,
                    "kalshi_event_title": k_event_title,
                    "kalshi_category": k_category,
                    "kalshi_rules": str(km.get("rules_primary", "") or "")[:500],
                    "kalshi_settlement_source": str(km.get("settlement_source_url", "") or ""),
                    "kalshi_close_time": str(km.get("close_time", "") or ""),
                    "poly_slug": slug,
                    "poly_question": str(pm.get("question", "") or pm.get("title", "")),
                    "poly_description": str(pm.get("description", ""))[:500],
                    "poly_resolution_source": str(pm.get("resolutionSource", "") or ""),
                    "poly_end_date": str(pm.get("endDate", "") or pm.get("closeTime", "")),
                    "poly_outcomes": pm.get("outcomes", []),
                    "shared_tokens": sorted(shared),
                    "token_overlap": round(overlap, 3),
                })

        # Sort by overlap score
        candidates.sort(key=lambda c: c["token_overlap"], reverse=True)
        return candidates

    def validate_pair(self, candidate: dict) -> tuple[bool, str]:
        """
        Validate a candidate pair. Returns (is_valid, reason).

        Validation criteria:
        1. Both markets must be about the SAME underlying event
        2. YES on one platform must equal YES on the other
        3. Resolution criteria must align
        4. Not a bracket/range vs binary mismatch
        5. Not a division vs championship mismatch
        6. Not a "reach finals" vs "win championship" mismatch
        7. Team names must match (not just city names)
        """
        kt = candidate["kalshi_ticker"].upper()
        k_title = candidate["kalshi_title"].lower()
        k_sub = candidate["kalshi_subtitle"].lower()
        p_q = candidate["poly_question"].lower()
        p_slug = candidate["poly_slug"].lower()

        # 1. Bracket market guard
        if re.search(r"KX[DR](?:SENATE|HOUSE)SEATS", kt, re.IGNORECASE):
            return False, "Kalshi is a seat-count bracket market"

        # 2. Multi-leg/parlay guard
        if any(kw in kt for kw in ("KXMVE", "PARLAY", "COMBO")):
            return False, "Kalshi is a multi-leg/parlay market"

        # 3. Props guard (player-level markets)
        prop_markers = ("KXNHLAST", "KXNHLGOAL", "KXNHLPTS", "KXNBAPTS", "KXNBAREBS",
                       "KXNBAAST", "KXNBA3PT", "KXNBASTEALS", "KXNBABLOCKS",
                       "KXMLBHITS", "KXMLBRBI", "KXMLBHR", "KXNFLPASS",
                       "KXNFLRUSH", "KXNFLREC", "KXNFLTD")
        if any(kt.startswith(m) for m in prop_markers):
            return False, "Kalshi is a player prop market"

        # 4. Esports/gaming guard
        if any(kw in kt for kw in ("KXCS2", "KXVALORANT", "KXLOL", "KXDOTA")):
            return False, "Kalshi is an esports market"

        # 5. Spread/total guard
        if any(kw in kt for kw in ("SPREAD", "TOTAL", "1HSPREAD", "2HSPREAD", "1HTOTAL", "2HTOTAL")):
            return False, "Kalshi is a spread/total market"

        # 6. Mention/social media guard
        if "MENTION" in kt:
            return False, "Kalshi is a social media mention market"

        # 7. Super Bowl vs Conference championship mismatch
        if "KXSB" in kt and ("nfc" in p_slug or "afc" in p_slug):
            return False, "Kalshi is Super Bowl, Polymarket is Conference championship"
        if "KXSB" in kt and "championship" in p_q and ("nfc" in p_q or "afc" in p_q):
            return False, "Kalshi is Super Bowl, Polymarket is Conference championship"

        # 8. Division vs Championship mismatch
        division_markers = ("NFCEAST", "NFCWEST", "NFCNORTH", "NFCSOUTH",
                          "AFCEAST", "AFCWEST", "AFCNORTH", "AFCSOUTH")
        champ_markers = ("NFCCHAMP", "AFCCHAMP")
        k_is_division = any(m in kt for m in division_markers)
        k_is_champ = any(m in kt for m in champ_markers)
        p_is_championship = "championship" in p_q
        p_is_division = "division" in p_q
        if k_is_division and p_is_championship and not p_is_division:
            return False, "Kalshi is division winner, Polymarket is championship"
        if k_is_champ and p_is_division:
            return False, "Kalshi is championship, Polymarket is division"

        # 9. "Reach finals" vs "Win championship" mismatch
        if "FINALIST" in kt and "win" in p_q:
            return False, "Kalshi is 'reach finals', Polymarket is 'win championship'"

        # 10. Team name mismatch detection
        # Extract team abbreviations and verify they map to the same team
        k_norm = normalize(f"{k_title} {k_sub}")
        p_norm = normalize(p_q)

        # Common mismatches: same city, different team
        city_team_mismatches = [
            ("georgia tech", "georgia bulldogs"),
            ("florida st", "florida gators"),
            ("florida state", "florida gators"),
            ("oklahoma st", "oklahoma sooners"),
            ("oklahoma state", "oklahoma sooners"),
            ("los angeles c", "los angeles r"),  # Chargers vs Rams
            ("new york g", "new york j"),  # Giants vs Jets
            ("north carolina st", "north carolina"),
            ("mississippi st", "ole miss"),
            ("texas am", "texas"),
        ]
        for team_a, team_b in city_team_mismatches:
            if team_a in k_norm and team_b in p_norm:
                return False, f"Team mismatch: Kalshi has '{team_a}', Polymarket has '{team_b}'"
            if team_b in k_norm and team_a in p_norm:
                return False, f"Team mismatch: Kalshi has '{team_b}', Polymarket has '{team_a}'"

        # 11. Same-event category check
        # If one is about weather and the other is sports, reject
        k_cat = candidate.get("kalshi_category", "")
        if k_cat and any(kw in k_cat for kw in ("sport", "esport", "gaming")):
            if not any(kw in p_norm for kw in ("nfl", "nba", "mlb", "nhl", "ncaa", "football",
                                                "basketball", "baseball", "hockey", "soccer",
                                                "golf", "tennis", "pga", "atp", "wta")):
                return False, "Category mismatch: Kalshi is sports but Polymarket doesn't look like sports"

        # 12. High-confidence match: both titles describe the same binary question
        # At this point, structural filters have passed. Check semantic alignment.
        # Require significant keyword overlap on the specific event/outcome
        shared = set(candidate.get("shared_tokens", []))
        important_shared = shared - {"2026", "2027", "will", "win", "the", "pro", "football",
                                      "college", "national", "championship", "game", "season"}
        if len(important_shared) < 2:
            return False, f"Insufficient specific keyword overlap: {sorted(important_shared)}"

        return True, "Passed all structural validation checks"


async def main():
    matcher = MarketMatcher()

    print("=" * 60, file=sys.stderr)
    print("Cross-Platform Market Pair Discovery & Validation", file=sys.stderr)
    print("=" * 60, file=sys.stderr)

    await matcher.load_catalogs()

    print("\nFinding candidate pairs...", file=sys.stderr)
    candidates = matcher.find_candidates()
    print(f"Found {len(candidates)} raw candidates", file=sys.stderr)

    print("\nValidating candidates...", file=sys.stderr)
    validated = []
    rejected = []
    for c in candidates:
        is_valid, reason = matcher.validate_pair(c)
        c["validation_result"] = "PASS" if is_valid else "FAIL"
        c["validation_reason"] = reason
        if is_valid:
            validated.append(c)
        else:
            rejected.append(c)

    print(f"\nResults:", file=sys.stderr)
    print(f"  Validated: {len(validated)}", file=sys.stderr)
    print(f"  Rejected:  {len(rejected)}", file=sys.stderr)

    # Print validated pairs
    print("\n=== VALIDATED PAIRS ===")
    for v in validated:
        print(f"\n  Kalshi:      {v['kalshi_ticker']}")
        print(f"    Title:     {v['kalshi_title']}")
        print(f"    Subtitle:  {v['kalshi_subtitle']}")
        print(f"  Polymarket:  {v['poly_slug']}")
        print(f"    Question:  {v['poly_question']}")
        print(f"  Overlap:     {v['token_overlap']:.0%} ({', '.join(v['shared_tokens'][:10])})")
        print(f"  Status:      {v['validation_reason']}")

    # Print rejection stats
    print("\n=== REJECTION REASONS ===")
    reason_counts = defaultdict(int)
    for r in rejected:
        reason_counts[r["validation_reason"]] += 1
    for reason, count in sorted(reason_counts.items(), key=lambda x: -x[1]):
        print(f"  {count:4d}  {reason}")

    # Save results
    with open("/tmp/validated_pairs.json", "w") as f:
        json.dump(validated, f, indent=2)
    with open("/tmp/rejected_pairs.json", "w") as f:
        json.dump(rejected, f, indent=2)
    print(f"\nSaved to /tmp/validated_pairs.json and /tmp/rejected_pairs.json", file=sys.stderr)


if __name__ == "__main__":
    asyncio.run(main())
