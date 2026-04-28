#!/usr/bin/env python3
"""
Validate market mappings batch 5 (indices 375-500).
Checks each Kalshi ticker and Polymarket slug against live APIs.
"""
import asyncio
import base64
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import aiohttp
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

SEEDS_PATH = Path(
    os.getenv("SEEDS_PATH_OVERRIDE") or
    str(Path(__file__).parent.parent / "arbiter/mapping/fixtures/market_seeds_auto.json")
)
KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2"
POLY_BASE = "https://gateway.polymarket.us"

BATCH_START = 375
BATCH_END = 501  # exclusive, so indices 375-500


# --- Kalshi auth ---

class KalshiAuth:
    def __init__(self):
        self.api_key_id = os.getenv("KALSHI_API_KEY_ID", "")
        key_path = os.getenv("KALSHI_PRIVATE_KEY_PATH", "./keys/kalshi_private.pem")
        if not os.path.isabs(key_path):
            key_path = str(Path(__file__).parent.parent / key_path)
        self._private_key = None
        try:
            with open(key_path, "rb") as f:
                self._private_key = serialization.load_pem_private_key(f.read(), password=None)
        except Exception as e:
            print(f"[WARN] Could not load Kalshi key: {e}")

    def get_headers(self, method: str, path: str) -> dict:
        ts = int(time.time() * 1000)
        headers = {"KALSHI-ACCESS-KEY": self.api_key_id, "KALSHI-ACCESS-TIMESTAMP": str(ts)}
        if self._private_key:
            msg = f"{ts}{method}{path}".encode()
            sig = self._private_key.sign(msg, padding.PKCS1v15(), hashes.SHA256())
            headers["KALSHI-ACCESS-SIGNATURE"] = base64.b64encode(sig).decode()
        return headers


kalshi_auth = KalshiAuth()


async def check_kalshi_market(session: aiohttp.ClientSession, ticker: str, retries: int = 4) -> dict:
    """Check if a Kalshi market ticker exists. Retries on 429 with exponential backoff."""
    path = f"/trade-api/v2/markets/{ticker}"
    url = f"{KALSHI_BASE}/markets/{ticker}"
    delay = 1.0
    for attempt in range(retries + 1):
        headers = kalshi_auth.get_headers("GET", path)
        try:
            async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    market = data.get("market", data)
                    return {
                        "exists": True,
                        "status": market.get("status", ""),
                        "close_time": market.get("close_time", ""),
                        "event_ticker": market.get("event_ticker", ""),
                        "title": market.get("title", ""),
                        "http_status": 200,
                    }
                elif resp.status == 404:
                    return {"exists": False, "http_status": 404, "error": "not found"}
                elif resp.status == 429:
                    if attempt < retries:
                        await asyncio.sleep(delay)
                        delay *= 2
                        continue
                    text = await resp.text()
                    return {"exists": False, "http_status": 429, "error": f"rate limited after {retries} retries: {text[:80]}"}
                else:
                    text = await resp.text()
                    return {"exists": False, "http_status": resp.status, "error": text[:100]}
        except Exception as e:
            if attempt < retries:
                await asyncio.sleep(delay)
                delay *= 2
                continue
            return {"exists": False, "http_status": 0, "error": str(e)}
    return {"exists": False, "http_status": 0, "error": "exhausted retries"}


async def check_polymarket_market(session: aiohttp.ClientSession, slug: str) -> dict:
    """Check if a Polymarket market slug exists. Returns dict with exists, data, error."""
    url = f"{POLY_BASE}/v1/market/slug/{slug}"
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status == 200:
                data = await resp.json()
                # Response may be {"market": {...}} or direct dict
                if isinstance(data, dict) and "market" in data:
                    market = data["market"]
                elif isinstance(data, list):
                    if len(data) == 0:
                        return {"exists": False, "http_status": 200, "error": "empty list"}
                    market = data[0]
                else:
                    market = data
                return {
                    "exists": True,
                    "active": market.get("active", True),
                    "closed": market.get("closed", False),
                    "ep3_status": market.get("ep3Status", ""),
                    "end_date": market.get("endDate", market.get("end_date_iso", "")),
                    "question": market.get("question", ""),
                    "category": market.get("category", ""),
                    "http_status": 200,
                }
            elif resp.status == 404:
                return {"exists": False, "http_status": 404, "error": "not found"}
            else:
                text = await resp.text()
                return {"exists": False, "http_status": resp.status, "error": text[:100]}
    except Exception as e:
        return {"exists": False, "http_status": 0, "error": str(e)}


def validate_mapping(mapping: dict, kalshi_result: dict, poly_result: dict) -> tuple[bool, list[str]]:
    """
    Returns (is_valid, list_of_issues).
    A mapping is valid if both markets exist. Expired markets can still be valid
    (they were real markets that matched).
    """
    issues = []

    if not kalshi_result["exists"]:
        issues.append(f"Kalshi market {mapping['kalshi']} not found (HTTP {kalshi_result['http_status']}): {kalshi_result.get('error', '')}")

    if not poly_result["exists"]:
        issues.append(f"Polymarket slug {mapping['polymarket']} not found (HTTP {poly_result['http_status']}): {poly_result.get('error', '')}")

    # If both exist, check event alignment via event_ticker vs poly question (best-effort)
    if kalshi_result.get("exists") and poly_result.get("exists"):
        kalshi_event = kalshi_result.get("event_ticker", "")
        # Extract event part from ticker (e.g. KXPGAR2LEAD-MAST26 from KXPGAR2LEAD-MAST26-MMCN)
        expected_event = mapping["kalshi"].rsplit("-", 1)[0]
        if kalshi_event and not kalshi_event.startswith(expected_event.split("-")[0]):
            issues.append(f"Event ticker mismatch: expected ~{expected_event}, got {kalshi_event}")

    is_valid = len(issues) == 0
    return is_valid, issues


async def run_validation():
    with open(SEEDS_PATH) as f:
        all_mappings = json.load(f)

    batch = all_mappings[BATCH_START:BATCH_END]
    print(f"Validating {len(batch)} mappings (indices {BATCH_START}-{BATCH_END-1})")
    print(f"Today: {datetime.now(timezone.utc).date()}")
    print()

    results = []
    confirmed_indices = []
    invalid_indices = []

    # Kalshi rate limits aggressively; keep concurrency low
    CONCURRENCY = 3

    connector = aiohttp.TCPConnector(limit=CONCURRENCY)
    async with aiohttp.ClientSession(connector=connector) as session:
        semaphore = asyncio.Semaphore(CONCURRENCY)

        async def validate_one(i: int, mapping: dict):
            async with semaphore:
                kalshi_r = await check_kalshi_market(session, mapping["kalshi"])
                poly_r = await check_polymarket_market(session, mapping["polymarket"])
                is_valid, issues = validate_mapping(mapping, kalshi_r, poly_r)
                return i, mapping, kalshi_r, poly_r, is_valid, issues

        tasks = [validate_one(BATCH_START + i, m) for i, m in enumerate(batch)]
        done = await asyncio.gather(*tasks)

    for (abs_idx, mapping, kalshi_r, poly_r, is_valid, issues) in sorted(done, key=lambda x: x[0]):
        result = {
            "index": abs_idx,
            "canonical_id": mapping["canonical_id"],
            "kalshi": mapping["kalshi"],
            "polymarket": mapping["polymarket"],
            "resolution_date": mapping.get("resolution_date", ""),
            "kalshi_exists": kalshi_r["exists"],
            "kalshi_status": kalshi_r.get("status", ""),
            "poly_exists": poly_r["exists"],
            "poly_active": poly_r.get("active", ""),
            "is_valid": is_valid,
            "issues": issues,
        }
        results.append(result)
        if is_valid:
            confirmed_indices.append(abs_idx)
        else:
            invalid_indices.append(abs_idx)

    # --- Summary ---
    print(f"=== VALIDATION RESULTS ===")
    print(f"Total validated: {len(results)}")
    print(f"Valid (to confirm): {len(confirmed_indices)}")
    print(f"Invalid: {len(invalid_indices)}")
    print()

    if invalid_indices:
        print("=== INVALID MAPPINGS ===")
        for r in results:
            if not r["is_valid"]:
                print(f"  [{r['index']}] {r['canonical_id']}")
                print(f"       kalshi={r['kalshi']} exists={r['kalshi_exists']}")
                print(f"       poly={r['polymarket']} exists={r['poly_exists']}")
                for issue in r["issues"]:
                    print(f"       ISSUE: {issue}")
        print()

    # --- Update statuses ---
    updated_count = 0
    for r in results:
        if r["is_valid"]:
            all_mappings[r["index"]]["status"] = "confirmed"
            updated_count += 1

    print(f"Updating {updated_count} mappings to 'confirmed'...")
    with open(SEEDS_PATH, "w") as f:
        json.dump(all_mappings, f, indent=2)
    print(f"Saved {SEEDS_PATH}")

    # --- Write report ---
    report = {
        "batch": f"{BATCH_START}-{BATCH_END-1}",
        "validated_at": datetime.now(timezone.utc).isoformat(),
        "total": len(results),
        "confirmed": len(confirmed_indices),
        "invalid": len(invalid_indices),
        "confirmed_indices": confirmed_indices,
        "invalid_details": [r for r in results if not r["is_valid"]],
        "all_results": results,
    }
    report_path = SEEDS_PATH.parent.parent.parent / "data/validation_report_batch5.json"
    report_path.parent.mkdir(exist_ok=True)
    with open(report_path, "w") as f:
        json.dump(report, f, indent=2)
    print(f"Report saved to {report_path}")

    return report


if __name__ == "__main__":
    asyncio.run(run_validation())
