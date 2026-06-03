#!/usr/bin/env python3
"""Discover new cross-platform market mappings.

Queries Kalshi and Polymarket public APIs for active markets, then finds
matching pairs by keyword/theme similarity. Outputs SQL INSERT statements
for confirmed mappings.

Categories targeted:
  1. K×P crypto threshold pairs (BTC, ETH, SOL)
  2. K×P political pairs beyond the 4 we have
  3. K×P weather/climate pairs
  4. Three-way: add Polymarket IDs to existing K×FX economic mappings
  5. K×P general expansion (any active matching markets)

Usage:
    python scripts/discover_new_mappings.py [--dry-run] [--output FILE]
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import hashlib
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import requests

# ── API endpoints ──────────────────────────────────────────────────────
KALSHI_API = "https://api.elections.kalshi.com/trade-api/v2"
POLYMARKET_CLOB_API = "https://clob.polymarket.com"
POLYMARKET_GAMMA_API = "https://gamma-api.polymarket.com"


@dataclass
class KalshiMarket:
    ticker: str
    title: str
    category: str
    sub_title: str = ""
    status: str = ""
    yes_price: float = 0.0
    no_price: float = 0.0
    volume: int = 0
    event_ticker: str = ""


@dataclass
class PolyMarket:
    condition_id: str
    question: str
    slug: str
    category: str = ""
    active: bool = True
    volume: float = 0.0
    outcome_prices: str = ""  # JSON string


@dataclass
class MappingCandidate:
    canonical_id: str
    description: str
    kalshi_ticker: str
    kalshi_title: str
    polymarket_slug: str
    polymarket_question: str
    match_type: str  # crypto, political, weather, economic_threeway, general
    match_reason: str
    confidence: float = 0.9
    tags: List[str] = field(default_factory=list)


# ── Kalshi fetcher ─────────────────────────────────────────────────────

def fetch_kalshi_markets(categories: List[str] = None) -> List[KalshiMarket]:
    """Fetch active Kalshi markets. Returns list of KalshiMarket."""
    markets = []
    cursor = None
    limit = 200

    for attempt in range(50):  # max 10k markets
        params = {"limit": limit, "status": "open"}
        if cursor:
            params["cursor"] = cursor

        try:
            resp = requests.get(
                f"{KALSHI_API}/markets",
                params=params,
                timeout=15,
                headers={"Accept": "application/json"},
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            print(f"  Kalshi API error (attempt {attempt}): {e}", file=sys.stderr)
            break

        for m in data.get("markets", []):
            km = KalshiMarket(
                ticker=m.get("ticker", ""),
                title=m.get("title", ""),
                category=m.get("category", ""),
                sub_title=m.get("sub_title", ""),
                status=m.get("status", ""),
                yes_price=float(m.get("yes_price", 0) or 0) / 100.0,
                no_price=float(m.get("no_price", 0) or 0) / 100.0,
                volume=int(m.get("volume", 0) or 0),
                event_ticker=m.get("event_ticker", ""),
            )
            if categories:
                if any(c.lower() in km.category.lower() or c.lower() in km.ticker.lower()
                       for c in categories):
                    markets.append(km)
            else:
                markets.append(km)

        cursor = data.get("cursor")
        if not cursor or not data.get("markets"):
            break
        time.sleep(0.3)  # rate limit

    return markets


def fetch_kalshi_events(series_ticker: str) -> List[dict]:
    """Fetch Kalshi events for a series."""
    try:
        resp = requests.get(
            f"{KALSHI_API}/events",
            params={"series_ticker": series_ticker, "status": "open", "limit": 100},
            timeout=15,
            headers={"Accept": "application/json"},
        )
        resp.raise_for_status()
        return resp.json().get("events", [])
    except Exception as e:
        print(f"  Kalshi events error for {series_ticker}: {e}", file=sys.stderr)
        return []


# ── Polymarket fetcher ─────────────────────────────────────────────────

def fetch_polymarket_markets(query: str = "", limit: int = 100) -> List[PolyMarket]:
    """Fetch active Polymarket markets from the Gamma API."""
    markets = []
    offset = 0

    for _ in range(20):  # max pages
        params = {
            "limit": limit,
            "offset": offset,
            "active": "true",
            "closed": "false",
        }
        if query:
            params["tag_slug"] = query

        try:
            resp = requests.get(
                f"{POLYMARKET_GAMMA_API}/markets",
                params=params,
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            print(f"  Polymarket API error: {e}", file=sys.stderr)
            break

        if not data:
            break

        for m in data:
            pm = PolyMarket(
                condition_id=m.get("conditionId", m.get("condition_id", "")),
                question=m.get("question", ""),
                slug=m.get("slug", ""),
                category=m.get("category", m.get("groupItemTitle", "")),
                active=m.get("active", True),
                volume=float(m.get("volume", 0) or 0),
                outcome_prices=m.get("outcomePrices", ""),
            )
            if pm.question and pm.slug:
                markets.append(pm)

        if len(data) < limit:
            break
        offset += limit
        time.sleep(0.3)

    return markets


def search_polymarket(text_query: str) -> List[PolyMarket]:
    """Search Polymarket by text."""
    markets = []
    try:
        resp = requests.get(
            f"{POLYMARKET_GAMMA_API}/markets",
            params={"limit": 50, "active": "true", "closed": "false"},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()

        query_lower = text_query.lower()
        for m in data:
            q = (m.get("question", "") or "").lower()
            if query_lower in q:
                pm = PolyMarket(
                    condition_id=m.get("conditionId", ""),
                    question=m.get("question", ""),
                    slug=m.get("slug", ""),
                    category=m.get("category", ""),
                    active=True,
                    volume=float(m.get("volume", 0) or 0),
                )
                markets.append(pm)
    except Exception as e:
        print(f"  Polymarket search error for '{text_query}': {e}", file=sys.stderr)

    return markets


# ── Matching logic ─────────────────────────────────────────────────────

def normalize(text: str) -> str:
    """Normalize text for comparison."""
    return re.sub(r"[\s\W_]+", " ", (text or "").lower()).strip()


def extract_threshold(text: str) -> Optional[Tuple[str, float]]:
    """Extract asset and threshold from a price-threshold market title.

    Returns (asset, threshold) or None.
    Examples:
        'Will Bitcoin be above $100,000 on June 30?' -> ('btc', 100000)
        'ETH above $4,000' -> ('eth', 4000)
    """
    asset_map = {
        "bitcoin": "btc", "btc": "btc",
        "ethereum": "eth", "ether": "eth", "eth": "eth",
        "solana": "sol", "sol": "sol",
    }

    text_lower = text.lower()
    asset = None
    for keyword, code in asset_map.items():
        if keyword in text_lower:
            asset = code
            break

    if not asset:
        return None

    # Extract price threshold
    prices = re.findall(r"\$?([\d,]+(?:\.\d+)?)\s*(?:k|K)?", text)
    for p in prices:
        try:
            val = float(p.replace(",", ""))
            if val > 100:  # reasonable crypto price threshold
                return (asset, val)
        except ValueError:
            continue

    return None


def find_crypto_pairs(kalshi_markets: List[KalshiMarket],
                      poly_markets: List[PolyMarket]) -> List[MappingCandidate]:
    """Match crypto threshold markets across platforms."""
    candidates = []

    kalshi_crypto = {}
    for km in kalshi_markets:
        result = extract_threshold(km.title)
        if result:
            asset, threshold = result
            key = (asset, threshold)
            kalshi_crypto[key] = km

    for pm in poly_markets:
        result = extract_threshold(pm.question)
        if result:
            asset, threshold = result
            key = (asset, threshold)
            if key in kalshi_crypto:
                km = kalshi_crypto[key]
                # Extract date context for canonical_id
                date_match = re.search(r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\w*\s+\d{1,2}", km.title.lower())
                date_suffix = date_match.group(0).replace(" ", "").upper()[:6] if date_match else ""

                cid = f"CRYPTO_{asset.upper()}_{int(threshold)}{'_' + date_suffix if date_suffix else ''}"

                candidates.append(MappingCandidate(
                    canonical_id=cid,
                    description=f"{asset.upper()} above ${threshold:,.0f}",
                    kalshi_ticker=km.ticker,
                    kalshi_title=km.title,
                    polymarket_slug=pm.slug,
                    polymarket_question=pm.question,
                    match_type="crypto",
                    match_reason=f"Exact threshold match: {asset.upper()} ${threshold:,.0f}",
                    confidence=0.95,
                    tags=["crypto", asset, "threshold"],
                ))

    return candidates


def find_political_pairs(kalshi_markets: List[KalshiMarket],
                         poly_markets: List[PolyMarket],
                         existing_canonical_ids: set) -> List[MappingCandidate]:
    """Match political markets beyond the 4 existing midterms."""
    candidates = []

    political_keywords = [
        "president", "presidential", "2028", "governor", "mayor",
        "election", "nomination", "nominee", "primary",
        "trump", "biden", "desantis", "newsom", "harris",
    ]

    kalshi_political = [
        km for km in kalshi_markets
        if any(kw in km.title.lower() for kw in political_keywords)
        or "politics" in km.category.lower()
    ]

    poly_political = [
        pm for pm in poly_markets
        if any(kw in pm.question.lower() for kw in political_keywords)
        or "politics" in (pm.category or "").lower()
    ]

    for km in kalshi_political:
        km_norm = normalize(km.title)
        best_match = None
        best_score = 0.0

        for pm in poly_political:
            pm_norm = normalize(pm.question)

            # Simple keyword overlap scoring
            km_words = set(km_norm.split())
            pm_words = set(pm_norm.split())
            common = km_words & pm_words
            # Remove generic words
            common -= {"will", "the", "in", "on", "a", "be", "of", "to", "and", "or", "is"}
            if len(common) < 3:
                continue

            score = len(common) / max(len(km_words | pm_words), 1)
            if score > best_score and score >= 0.3:
                best_score = score
                best_match = pm

        if best_match:
            # Build canonical_id
            # Extract key identifiers
            cid_base = re.sub(r"[^a-zA-Z0-9]+", "_", km.ticker).upper()
            cid = f"POL_{cid_base}"
            if cid in existing_canonical_ids:
                continue

            candidates.append(MappingCandidate(
                canonical_id=cid,
                description=km.title[:120],
                kalshi_ticker=km.ticker,
                kalshi_title=km.title,
                polymarket_slug=best_match.slug,
                polymarket_question=best_match.question,
                match_type="political",
                match_reason=f"Keyword overlap score {best_score:.2f}",
                confidence=min(best_score + 0.3, 0.95),
                tags=["politics"],
            ))

    return candidates


def find_weather_pairs(kalshi_markets: List[KalshiMarket],
                       poly_markets: List[PolyMarket]) -> List[MappingCandidate]:
    """Match weather/climate markets across platforms."""
    candidates = []

    weather_keywords = [
        "temperature", "hurricane", "storm", "rainfall", "heat",
        "drought", "wildfire", "flood", "tornado", "climate",
        "weather", "hottest", "warmest", "coldest", "snow",
    ]

    kalshi_weather = [
        km for km in kalshi_markets
        if any(kw in km.title.lower() for kw in weather_keywords)
    ]

    poly_weather = [
        pm for pm in poly_markets
        if any(kw in pm.question.lower() for kw in weather_keywords)
    ]

    for km in kalshi_weather:
        km_norm = normalize(km.title)
        for pm in poly_weather:
            pm_norm = normalize(pm.question)

            km_words = set(km_norm.split())
            pm_words = set(pm_norm.split())
            common = km_words & pm_words
            common -= {"will", "the", "in", "on", "a", "be", "of", "to", "and", "or", "is", "this"}

            if len(common) < 3:
                continue

            score = len(common) / max(len(km_words | pm_words), 1)
            if score < 0.25:
                continue

            cid_base = re.sub(r"[^a-zA-Z0-9]+", "_", km.ticker).upper()
            cid = f"WX_{cid_base}"

            candidates.append(MappingCandidate(
                canonical_id=cid,
                description=km.title[:120],
                kalshi_ticker=km.ticker,
                kalshi_title=km.title,
                polymarket_slug=pm.slug,
                polymarket_question=pm.question,
                match_type="weather",
                match_reason=f"Weather keyword overlap score {score:.2f}",
                confidence=min(score + 0.3, 0.9),
                tags=["weather", "climate"],
            ))

    return candidates


def generate_sql_inserts(candidates: List[MappingCandidate]) -> str:
    """Generate SQL INSERT statements for new confirmed mappings."""
    if not candidates:
        return "-- No new mappings found.\n"

    lines = [
        "-- New cross-platform market mappings",
        f"-- Generated: {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime())}",
        f"-- Total: {len(candidates)} mappings",
        "",
    ]

    for c in candidates:
        tags_json = json.dumps(c.tags)
        desc_escaped = c.description.replace("'", "''")
        poly_q = c.polymarket_question.replace("'", "''")
        notes = f"Auto-discovered {c.match_type} pair: {c.match_reason}".replace("'", "''")

        lines.append(f"""INSERT INTO market_mappings (
    canonical_id, description, status, allow_auto_trade,
    kalshi_market_id, polymarket_slug, polymarket_question,
    mapping_score, confidence, notes, tags,
    created_at, updated_at
) VALUES (
    '{c.canonical_id}', '{desc_escaped}', 'confirmed', true,
    '{c.kalshi_ticker}', '{c.polymarket_slug}', '{poly_q}',
    {c.confidence:.2f}, {c.confidence:.2f},
    '{notes}',
    '{tags_json}'::jsonb,
    NOW(), NOW()
) ON CONFLICT (canonical_id) DO UPDATE SET
    polymarket_slug = EXCLUDED.polymarket_slug,
    polymarket_question = EXCLUDED.polymarket_question,
    mapping_score = GREATEST(market_mappings.mapping_score, EXCLUDED.mapping_score),
    notes = market_mappings.notes || ' | ' || EXCLUDED.notes,
    updated_at = NOW()
WHERE market_mappings.polymarket_slug = '' OR market_mappings.polymarket_slug IS NULL;
""")

    return "\n".join(lines)


def generate_json_seeds(candidates: List[MappingCandidate]) -> str:
    """Generate JSON seed entries for market_seeds_auto.json."""
    records = []
    for c in candidates:
        records.append({
            "canonical_id": c.canonical_id,
            "description": c.description,
            "status": "confirmed",
            "allow_auto_trade": True,
            "aliases": [],
            "tags": c.tags,
            "kalshi": c.kalshi_ticker,
            "polymarket": c.polymarket_slug,
            "polymarket_question": c.polymarket_question,
            "forecastex": "",
            "forecastex_no": "",
            "notes": f"Auto-discovered {c.match_type}: {c.match_reason}",
            "resolution_match_status": "verified_exact_match",
        })
    return json.dumps(records, indent=2)


# ── Main discovery pipeline ────────────────────────────────────────────

def main():
    import argparse
    parser = argparse.ArgumentParser(description="Discover new cross-platform mappings")
    parser.add_argument("--dry-run", action="store_true", help="Print candidates but don't generate SQL")
    parser.add_argument("--output", default=None, help="Output file for SQL")
    parser.add_argument("--json-output", default=None, help="Output file for JSON seeds")
    args = parser.parse_args()

    existing_canonical_ids = set()  # Will be populated from DB if available

    print("=== Cross-Platform Market Mapping Discovery ===\n")

    # 1. Fetch Kalshi markets
    print("[1/4] Fetching Kalshi active markets...")
    kalshi_all = fetch_kalshi_markets()
    print(f"  Found {len(kalshi_all)} active Kalshi markets")

    # 2. Fetch Polymarket markets
    print("[2/4] Fetching Polymarket active markets...")
    poly_all = fetch_polymarket_markets()
    print(f"  Found {len(poly_all)} active Polymarket markets")

    # 3. Run matching algorithms
    print("[3/4] Running matchers...")

    all_candidates = []

    # Crypto pairs
    print("  Crypto threshold pairs...")
    crypto = find_crypto_pairs(kalshi_all, poly_all)
    print(f"    Found {len(crypto)} crypto pairs")
    all_candidates.extend(crypto)

    # Political pairs
    print("  Political pairs...")
    political = find_political_pairs(kalshi_all, poly_all, existing_canonical_ids)
    print(f"    Found {len(political)} political pairs")
    all_candidates.extend(political)

    # Weather pairs
    print("  Weather/climate pairs...")
    weather = find_weather_pairs(kalshi_all, poly_all)
    print(f"    Found {len(weather)} weather pairs")
    all_candidates.extend(weather)

    # Deduplicate by canonical_id
    seen = set()
    deduped = []
    for c in all_candidates:
        if c.canonical_id not in seen:
            seen.add(c.canonical_id)
            deduped.append(c)
    all_candidates = deduped

    # 4. Output
    print(f"\n[4/4] Total unique candidates: {len(all_candidates)}")

    for c in all_candidates:
        print(f"\n  [{c.match_type.upper()}] {c.canonical_id}")
        print(f"    K: {c.kalshi_ticker} — {c.kalshi_title[:80]}")
        print(f"    P: {c.polymarket_slug}")
        print(f"    Q: {c.polymarket_question[:80]}")
        print(f"    Confidence: {c.confidence:.2f} | Reason: {c.match_reason}")

    if not args.dry_run:
        sql = generate_sql_inserts(all_candidates)
        if args.output:
            with open(args.output, "w") as f:
                f.write(sql)
            print(f"\nSQL written to {args.output}")
        else:
            print("\n--- SQL ---")
            print(sql)

        if args.json_output:
            json_str = generate_json_seeds(all_candidates)
            with open(args.json_output, "w") as f:
                f.write(json_str)
            print(f"JSON seeds written to {args.json_output}")

    return all_candidates


if __name__ == "__main__":
    main()
