#!/usr/bin/env python3
"""Validate market mappings against live Kalshi and Polymarket APIs."""

import json
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2/markets"
POLYMARKET_BASE = "https://gamma-api.polymarket.com/markets"

def fetch_json(url, retries=3, delay=1.0):
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "arbiter-validator/1.0"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                return json.loads(resp.read()), None
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None, f"HTTP 404 Not Found"
            elif e.code == 429:
                time.sleep(delay * (attempt + 1) * 2)
                continue
            return None, f"HTTP {e.code}"
        except Exception as e:
            if attempt < retries - 1:
                time.sleep(delay)
                continue
            return None, str(e)
    return None, "Max retries exceeded"

def validate_mapping(mapping, idx):
    """Validate a single mapping. Returns dict with result."""
    result = {
        "index": idx,
        "kalshi_ticker": mapping["kalshi_ticker"],
        "polymarket_slug": mapping["polymarket_slug"],
        "kalshi_title": mapping["kalshi_title"],
        "polymarket_question": mapping["polymarket_question"],
        "similarity_score": mapping["similarity_score"],
        "kalshi_expiry": mapping.get("kalshi_expiry"),
        "polymarket_end_date": mapping.get("polymarket_end_date"),
        "status": None,
        "reason": [],
        "kalshi_active": None,
        "kalshi_market_type": None,
        "kalshi_legs": None,
        "polymarket_active": None,
        "polymarket_closed": None,
        "date_delta_days": None,
    }

    # --- Check Kalshi ---
    kalshi_url = f"{KALSHI_BASE}/{mapping['kalshi_ticker']}"
    kalshi_data, kalshi_err = fetch_json(kalshi_url)

    if kalshi_err or kalshi_data is None:
        result["reason"].append(f"Kalshi API error: {kalshi_err}")
        result["status"] = "INVALID"
        return result

    market = kalshi_data.get("market", {})
    kalshi_status = market.get("status", "unknown")
    is_active = kalshi_status in ("open", "active") or market.get("can_close_early") is not None

    # Detect if it's a multi-variate/parlay market
    mve_legs = market.get("mve_selected_legs", [])
    market_type = market.get("market_type", "unknown")
    ticker = mapping["kalshi_ticker"]
    is_parlay = "KXMVESPORTSMULTIGAMEEXTENDED" in ticker or "KXMVECROSSCATEGORY" in ticker

    result["kalshi_active"] = market.get("can_close_early") is not None or is_active
    result["kalshi_market_type"] = "parlay/MVE" if is_parlay else market_type
    result["kalshi_legs"] = len(mve_legs)
    result["kalshi_status"] = kalshi_status
    result["kalshi_close_time"] = market.get("close_time")

    if is_parlay:
        leg_descriptions = []
        for leg in mve_legs:
            leg_descriptions.append(f"{leg.get('market_ticker','?')} (side={leg.get('side','?')})")
        result["reason"].append(
            f"Kalshi is a {('MVE parlay' if 'KXMVESPORTSMULTIGAMEEXTENDED' in ticker else 'cross-category MVE')} "
            f"with {len(mve_legs)} legs: {'; '.join(leg_descriptions[:3])}"
        )

    # --- Check Polymarket ---
    poly_url = f"{POLYMARKET_BASE}?slug={mapping['polymarket_slug']}"
    poly_data, poly_err = fetch_json(poly_url)

    if poly_err or poly_data is None:
        result["reason"].append(f"Polymarket API error: {poly_err}")
        result["status"] = "INVALID"
        return result

    if not isinstance(poly_data, list) or len(poly_data) == 0:
        result["reason"].append("Polymarket: market not found (empty response)")
        result["status"] = "INVALID"
        return result

    poly_market = poly_data[0]
    result["polymarket_active"] = poly_market.get("active", False)
    result["polymarket_closed"] = poly_market.get("closed", True)
    result["polymarket_question_verified"] = poly_market.get("question", "")
    result["polymarket_condition_id"] = poly_market.get("conditionId", "")
    result["polymarket_end_date_verified"] = poly_market.get("endDate", "")

    # --- Date comparison ---
    try:
        kalshi_close = market.get("expected_expiration_time") or market.get("close_time")
        poly_end = poly_market.get("endDate")
        if kalshi_close and poly_end:
            k_dt = datetime.fromisoformat(kalshi_close.replace("Z", "+00:00"))
            p_dt = datetime.fromisoformat(poly_end.replace("Z", "+00:00"))
            delta = abs((k_dt - p_dt).days)
            result["date_delta_days"] = delta
            if delta > 7:
                result["reason"].append(
                    f"Date mismatch: Kalshi closes {kalshi_close[:10]}, Polymarket ends {poly_end[:10]} ({delta} days apart)"
                )
    except Exception as e:
        result["reason"].append(f"Date parse error: {e}")

    # --- Determine status ---
    if is_parlay:
        # Parlay vs. single market = structurally incompatible for arbitrage
        result["status"] = "INVALID"
        if not result["reason"] or all("MVE parlay" in r or "cross-category" in r for r in result["reason"]):
            result["reason"].insert(0,
                "STRUCTURAL MISMATCH: Kalshi is a multi-leg parlay market but Polymarket is a single binary market. "
                "Cannot arbitrage a combined parlay against one independent market."
            )
        else:
            result["reason"].insert(0,
                "STRUCTURAL MISMATCH: Kalshi is a multi-leg parlay market but Polymarket is a single binary market."
            )
    elif not result["polymarket_active"] or result["polymarket_closed"]:
        result["status"] = "INVALID"
        result["reason"].append("Polymarket market is not active or is closed")
    elif result.get("date_delta_days") is not None and result["date_delta_days"] > 7:
        result["status"] = "NEEDS_REVIEW"
    else:
        result["status"] = "VALID"

    if not result["reason"]:
        result["reason"].append("All checks passed")

    return result


def main():
    data_path = Path("data/discovered_mappings.json")
    data = json.load(open(data_path))

    total = len(data)
    print(f"Validating {total} mappings (all of them, max requested was 75)...")
    print("=" * 70)

    results = []
    stats = {"VALID": 0, "INVALID": 0, "NEEDS_REVIEW": 0}

    for i, mapping in enumerate(data):
        print(f"[{i+1:02d}/{total}] {mapping['kalshi_ticker'][:50]}...", end="", flush=True)
        result = validate_mapping(mapping, i)
        results.append(result)
        stats[result["status"]] = stats.get(result["status"], 0) + 1

        status_emoji = {"VALID": "✓", "INVALID": "✗", "NEEDS_REVIEW": "?"}.get(result["status"], "?")
        print(f" [{status_emoji}] {result['status']}")
        for r in result["reason"][:1]:
            print(f"      {r[:90]}")

        time.sleep(0.2)  # rate limiting

    print()
    print("=" * 70)
    print(f"VALIDATION SUMMARY")
    print(f"  VALID:        {stats.get('VALID', 0)}")
    print(f"  INVALID:      {stats.get('INVALID', 0)}")
    print(f"  NEEDS_REVIEW: {stats.get('NEEDS_REVIEW', 0)}")
    print(f"  Total:        {total}")
    print()

    # Save full report
    report_path = Path("data/validation_report.json")
    with open(report_path, "w") as f:
        json.dump({
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "summary": stats,
            "total": total,
            "results": results
        }, f, indent=2)
    print(f"Full report saved to {report_path}")

    # Update original mappings with validation status
    for i, mapping in enumerate(data):
        result = results[i]
        mapping["validation_status"] = result["status"]
        mapping["validation_reason"] = result["reason"]
        mapping["validation_date"] = datetime.now(timezone.utc).date().isoformat()

    with open(data_path, "w") as f:
        json.dump(data, f, indent=2)
    print(f"Updated {data_path} with validation statuses")

    return results, stats


if __name__ == "__main__":
    main()
