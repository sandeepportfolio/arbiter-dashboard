#!/usr/bin/env python3
"""
Validation script for market_seeds_auto.json batch 3 (indices 150-250).

Strategy:
  - Kalshi: live API (markets persist after resolution)
  - Polymarket: cached polymarket_raw.json (closed markets disappear from live API)
"""

import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import aiohttp

KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2"
SEEDS_PATH = Path("/Users/rentamac/Documents/arbiter/.claude/worktrees/busy-ptolemy-f0ad15/arbiter/mapping/fixtures/market_seeds_auto.json")
POLY_CACHE = Path("/Users/rentamac/Documents/arbiter/.claude/worktrees/busy-ptolemy-f0ad15/scripts/output/polymarket_raw.json")
REPORT_PATH = Path(__file__).parent.parent / "data" / "validation_report_batch3.json"

TODAY = datetime.now(timezone.utc).date()


def build_poly_slug_index(cache_path: Path) -> dict[str, dict]:
    """Build slug → market dict from cached Polymarket data."""
    with open(cache_path) as f:
        markets = json.load(f)
    return {m["slug"]: m for m in markets if "slug" in m}


async def check_kalshi(session: aiohttp.ClientSession, ticker: str) -> dict:
    url = f"{KALSHI_BASE}/markets/{ticker}"
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            if resp.status == 200:
                data = await resp.json()
                market = data.get("market", data)
                return {
                    "exists": True,
                    "status": market.get("status", "unknown"),
                    "title": market.get("title", ""),
                    "yes_sub_title": market.get("yes_sub_title", ""),
                    "no_sub_title": market.get("no_sub_title", ""),
                    "event_ticker": market.get("event_ticker", ""),
                    "close_time": market.get("close_time", ""),
                    "expiration_time": market.get("expiration_time", ""),
                    "result": market.get("result", ""),
                    "http_status": 200,
                }
            elif resp.status == 404:
                return {"exists": False, "http_status": 404, "reason": "not_found"}
            elif resp.status == 429:
                await asyncio.sleep(2.0)
                # Retry once
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as r2:
                    if r2.status == 200:
                        data = await r2.json()
                        market = data.get("market", data)
                        return {"exists": True, "status": market.get("status", ""), "title": market.get("title", ""), "yes_sub_title": market.get("yes_sub_title", ""), "no_sub_title": market.get("no_sub_title", ""), "event_ticker": market.get("event_ticker", ""), "close_time": market.get("close_time", ""), "expiration_time": market.get("expiration_time", ""), "result": market.get("result", ""), "http_status": 200}
                    return {"exists": False, "http_status": r2.status, "reason": f"rate_limited_retry_{r2.status}"}
            else:
                return {"exists": False, "http_status": resp.status, "reason": f"http_{resp.status}"}
    except asyncio.TimeoutError:
        return {"exists": False, "http_status": 0, "reason": "timeout"}
    except Exception as e:
        return {"exists": False, "http_status": 0, "reason": str(e)[:80]}


def check_polymarket_cache(slug: str, poly_index: dict[str, dict]) -> dict:
    market = poly_index.get(slug)
    if not market:
        return {"exists": False, "source": "cache", "reason": "slug_not_in_cache"}
    return {
        "exists": True,
        "source": "cache",
        "id": market.get("id", ""),
        "question": market.get("question", ""),
        "slug": market.get("slug", ""),
        "end_date": market.get("endDate", ""),
        "active": market.get("active"),
        "closed": market.get("closed"),
    }


def determine_validity(entry: dict, kalshi_result: dict, poly_result: dict) -> tuple[bool, str]:
    if not kalshi_result["exists"]:
        return False, f"kalshi_not_found:{kalshi_result.get('reason', 'unknown')}"

    if not poly_result["exists"]:
        return False, f"polymarket_not_in_cache:{poly_result.get('reason', 'unknown')}"

    # Both exist — verify event alignment by checking player name
    # Kalshi stores player in yes_sub_title or no_sub_title
    k_player = (kalshi_result.get("yes_sub_title") or kalshi_result.get("no_sub_title") or "").lower()
    p_question = poly_result.get("question", "").lower()
    entry_desc = entry["description"].lower()

    # Extract player name from description (e.g. "2026 Masters Round 1 Leader: Aaron Rai" → "aaron rai")
    if ":" in entry_desc:
        player_part = entry_desc.split(":")[-1].strip()
        player_words = player_part.split()
    else:
        player_words = []

    # Check player name appears in either Kalshi or Polymarket question
    player_match = True
    if player_words and k_player:
        # At least one name word should match Kalshi's player field
        player_match = any(w in k_player for w in player_words if len(w) > 2)

    if not player_match:
        return False, f"player_mismatch:entry='{player_part}' kalshi='{k_player[:40]}'"

    # Resolution date check
    resolution_date_str = entry.get("resolution_date", "")
    is_expired = False
    if resolution_date_str:
        try:
            resolution_date = datetime.strptime(resolution_date_str, "%Y-%m-%d").date()
            is_expired = resolution_date < TODAY
        except ValueError:
            pass

    kalshi_status = kalshi_result.get("status", "")
    if is_expired:
        return True, f"valid_closed:kalshi_status={kalshi_status},expired={resolution_date_str}"

    return True, f"valid_open:kalshi_status={kalshi_status}"


async def validate_batch(entries: list, start_idx: int, poly_index: dict) -> list:
    results = []
    connector = aiohttp.TCPConnector(limit=3)
    async with aiohttp.ClientSession(connector=connector) as session:
        for i, entry in enumerate(entries):
            global_idx = start_idx + i
            ticker = entry["kalshi"]
            slug = entry["polymarket"]

            # Stagger Kalshi requests to avoid 429
            if i > 0:
                await asyncio.sleep(0.3)

            kalshi_result = await check_kalshi(session, ticker)
            poly_result = check_polymarket_cache(slug, poly_index)

            is_valid, reason = determine_validity(entry, kalshi_result, poly_result)

            result = {
                "index": global_idx,
                "canonical_id": entry["canonical_id"],
                "description": entry["description"],
                "kalshi": ticker,
                "polymarket": slug,
                "resolution_date": entry.get("resolution_date", ""),
                "valid": is_valid,
                "reason": reason,
                "kalshi_check": kalshi_result,
                "polymarket_check": poly_result,
            }
            results.append(result)

            icon = "✓" if is_valid else "✗"
            print(f"[{global_idx:3d}] {icon} {ticker:<35} {reason}", flush=True)

    return results


async def main():
    print("Loading Polymarket cache...", flush=True)
    poly_index = build_poly_slug_index(POLY_CACHE)
    print(f"  {len(poly_index):,} Polymarket markets indexed")

    with open(SEEDS_PATH) as f:
        all_entries = json.load(f)

    batch = all_entries[150:251]
    print(f"\nValidating {len(batch)} entries (indices 150–250)...")
    print(f"Today: {TODAY}")
    print("=" * 80)

    results = await validate_batch(batch, start_idx=150, poly_index=poly_index)

    valid = [r for r in results if r["valid"]]
    invalid = [r for r in results if not r["valid"]]

    print("\n" + "=" * 80)
    print(f"RESULTS: {len(valid)} valid / {len(invalid)} invalid / {len(results)} total")

    reasons: dict[str, int] = {}
    for r in invalid:
        key = r["reason"].split(":")[0]
        reasons[key] = reasons.get(key, 0) + 1
    if reasons:
        print("Invalid reasons:", json.dumps(reasons, indent=2))

    valid_by_reason: dict[str, int] = {}
    for r in valid:
        key = r["reason"].split(":")[0]
        valid_by_reason[key] = valid_by_reason.get(key, 0) + 1
    if valid_by_reason:
        print("Valid breakdown:", json.dumps(valid_by_reason, indent=2))

    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "batch": "3",
        "indices": "150-250",
        "total": len(results),
        "valid_count": len(valid),
        "invalid_count": len(invalid),
        "polymarket_source": "cache (closed markets not in live API)",
        "valid_canonical_ids": [r["canonical_id"] for r in valid],
        "invalid_canonical_ids": [r["canonical_id"] for r in invalid],
        "details": results,
    }
    with open(REPORT_PATH, "w") as f:
        json.dump(report, f, indent=2)
    print(f"\nReport written to: {REPORT_PATH}")

    return valid, invalid, results


if __name__ == "__main__":
    asyncio.run(main())
