#!/usr/bin/env python3
"""
Retry validation for batch 5 rate-limited Kalshi markets.
Re-checks only the indices that previously got HTTP 429.
"""
import asyncio
import base64
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import aiohttp
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

SEEDS_PATH = Path(
    os.getenv("SEEDS_PATH_OVERRIDE") or
    str(Path(__file__).parent.parent / "arbiter/mapping/fixtures/market_seeds_auto.json")
)
REPORT_PATH = SEEDS_PATH.parent.parent.parent / "data/validation_report_batch5.json"
KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2"
POLY_BASE = "https://gateway.polymarket.us"


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


async def check_kalshi_market(session: aiohttp.ClientSession, ticker: str, retries: int = 5) -> dict:
    path = f"/trade-api/v2/markets/{ticker}"
    url = f"{KALSHI_BASE}/markets/{ticker}"
    delay = 2.0
    for attempt in range(retries + 1):
        headers = kalshi_auth.get_headers("GET", path)
        try:
            async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=20)) as resp:
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
                        wait = delay * (2 ** attempt)
                        print(f"  [429] {ticker} rate-limited, waiting {wait:.1f}s (attempt {attempt+1}/{retries})")
                        await asyncio.sleep(wait)
                        continue
                    text = await resp.text()
                    return {"exists": False, "http_status": 429, "error": f"still rate-limited: {text[:80]}"}
                else:
                    text = await resp.text()
                    return {"exists": False, "http_status": resp.status, "error": text[:100]}
        except Exception as e:
            if attempt < retries:
                await asyncio.sleep(delay)
                continue
            return {"exists": False, "http_status": 0, "error": str(e)}
    return {"exists": False, "http_status": 0, "error": "exhausted retries"}


async def run_retry():
    with open(SEEDS_PATH) as f:
        all_mappings = json.load(f)

    with open(REPORT_PATH) as f:
        report = json.load(f)

    # Find indices still invalid (429 errors)
    retry_indices = [
        r["index"] for r in report["invalid_details"]
        if r["issues"] and "429" in str(r["issues"])
    ]
    print(f"Retrying {len(retry_indices)} rate-limited Kalshi checks...")

    # Use very low concurrency + sequential to avoid 429s
    CONCURRENCY = 2
    connector = aiohttp.TCPConnector(limit=CONCURRENCY)
    newly_confirmed = []
    still_invalid = []

    async with aiohttp.ClientSession(connector=connector) as session:
        sem = asyncio.Semaphore(CONCURRENCY)

        async def retry_one(abs_idx: int):
            async with sem:
                # Add jitter between requests
                await asyncio.sleep(0.5 + abs_idx % 3 * 0.3)
                m = all_mappings[abs_idx]
                result = await check_kalshi_market(session, m["kalshi"])
                return abs_idx, m, result

        tasks = [retry_one(i) for i in retry_indices]
        done = await asyncio.gather(*tasks)

    confirmed_count = 0
    for abs_idx, mapping, kalshi_r in sorted(done, key=lambda x: x[0]):
        if kalshi_r["exists"]:
            all_mappings[abs_idx]["status"] = "confirmed"
            newly_confirmed.append(abs_idx)
            confirmed_count += 1
            print(f"  [CONFIRMED] [{abs_idx}] {mapping['kalshi']} -> exists, status={kalshi_r.get('status','')}")
        else:
            still_invalid.append({
                "index": abs_idx,
                "kalshi": mapping["kalshi"],
                "polymarket": mapping["polymarket"],
                "error": kalshi_r.get("error", ""),
                "http_status": kalshi_r.get("http_status", 0),
            })
            print(f"  [INVALID]   [{abs_idx}] {mapping['kalshi']} -> {kalshi_r.get('error', '')[:80]}")

    print(f"\nRetry results: {confirmed_count} newly confirmed, {len(still_invalid)} still invalid")

    # Update the seeds file
    with open(SEEDS_PATH, "w") as f:
        json.dump(all_mappings, f, indent=2)
    print(f"Saved {SEEDS_PATH}")

    # Update the report
    report["retry_at"] = datetime.now(timezone.utc).isoformat()
    report["newly_confirmed_on_retry"] = newly_confirmed
    report["still_invalid_after_retry"] = still_invalid
    report["final_confirmed"] = report["confirmed"] + len(newly_confirmed)
    report["final_invalid"] = len(still_invalid)

    with open(REPORT_PATH, "w") as f:
        json.dump(report, f, indent=2)
    print(f"Updated report at {REPORT_PATH}")

    return report


if __name__ == "__main__":
    asyncio.run(run_retry())
