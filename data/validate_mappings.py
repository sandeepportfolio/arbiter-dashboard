#!/usr/bin/env python3
"""
Validate all market mappings against live Kalshi and Polymarket APIs.
Sources: crypto (27), sports (730), embeddings (62) = ~819 total.
"""
from __future__ import annotations

import asyncio
import json
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import aiohttp

MAIN_REPO = Path("/Users/rentamac/Documents/arbiter")
WORKTREE = Path("/Users/rentamac/Documents/arbiter/.claude/worktrees/suspicious-shamir-013002")
OUT_FILE = WORKTREE / "data" / "validation_results.json"

KALSHI_API = "https://api.elections.kalshi.com/trade-api/v2/markets/{ticker}"
POLY_API = "https://gamma-api.polymarket.com/markets?slug={slug}"

BATCH_SIZE = 20
BATCH_DELAY = 1.0  # seconds between batches
CONCURRENCY = 10   # max simultaneous API calls per platform
RETRY_MAX = 3
RETRY_DELAY = 0.5  # seconds between retries


# ── Data extraction ───────────────────────────────────────────────────────────

def load_crypto_seeds() -> list[dict]:
    """Extract 27 crypto/finance pairs from git branch."""
    content = subprocess.check_output(
        ["git", "show", "origin/claude/suspicious-aryabhata-b9c84d:arbiter/config/market_seeds_ext.py"],
        cwd=MAIN_REPO, text=True
    )
    kalshi_tickers = re.findall(r"kalshi='([^']+)'", content)
    poly_slugs = re.findall(r"polymarket='([^']+)'", content)
    descriptions = re.findall(r"description='([^']+)'", content)
    records = []
    for k, p, d in zip(kalshi_tickers, poly_slugs, descriptions):
        records.append({
            "kalshi_ticker": k,
            "polymarket_slug": p,
            "description": d,
            "source": "crypto",
        })
    print(f"  Loaded {len(records)} crypto/finance seeds")
    return records


def load_sports_seeds() -> list[dict]:
    """Extract 730 sports pairs from git branch."""
    raw = subprocess.check_output(
        ["git", "show", "origin/claude/busy-ptolemy-f0ad15:arbiter/mapping/fixtures/market_seeds_auto.json"],
        cwd=MAIN_REPO, text=True
    )
    data = json.loads(raw)
    records = []
    for item in data:
        records.append({
            "kalshi_ticker": item.get("kalshi", ""),
            "polymarket_slug": item.get("polymarket", ""),
            "description": item.get("description", ""),
            "source": "sports",
        })
    print(f"  Loaded {len(records)} sports seeds")
    return records


def load_embedding_seeds() -> list[dict]:
    """Load 62 embedding-discovered pairs from data/discovered_mappings.json."""
    with open(MAIN_REPO / "data" / "discovered_mappings.json") as f:
        data = json.load(f)
    records = []
    for item in data:
        records.append({
            "kalshi_ticker": item.get("kalshi_ticker", ""),
            "polymarket_slug": item.get("polymarket_slug", ""),
            "description": item.get("polymarket_question", item.get("kalshi_title", "")),
            "source": "embeddings",
        })
    print(f"  Loaded {len(records)} embedding seeds")
    return records


# ── API validation ────────────────────────────────────────────────────────────

_kalshi_sem: asyncio.Semaphore
_poly_sem: asyncio.Semaphore


async def check_kalshi(session: aiohttp.ClientSession, ticker: str) -> dict[str, Any]:
    if not ticker:
        return {"status": "not_found", "close_time": None, "error": "empty ticker"}
    url = KALSHI_API.format(ticker=ticker)
    async with _kalshi_sem:
        for attempt in range(RETRY_MAX):
            try:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    if resp.status == 404:
                        return {"status": "not_found", "close_time": None}
                    if resp.status == 429:
                        await asyncio.sleep(RETRY_DELAY * (attempt + 1))
                        continue
                    if resp.status != 200:
                        if attempt < RETRY_MAX - 1:
                            await asyncio.sleep(RETRY_DELAY)
                            continue
                        return {"status": "error", "close_time": None, "error": f"HTTP {resp.status}"}
                    body = await resp.json()
                    market = body.get("market", {})
                    status = market.get("status", "unknown")
                    close_time = market.get("close_time")
                    if status == "active":
                        return {"status": "active", "close_time": close_time}
                    elif status in ("closed", "settled", "finalized"):
                        return {"status": "closed", "close_time": close_time}
                    else:
                        return {"status": status, "close_time": close_time}
            except asyncio.TimeoutError:
                if attempt < RETRY_MAX - 1:
                    await asyncio.sleep(RETRY_DELAY)
                    continue
                return {"status": "error", "close_time": None, "error": "timeout"}
            except Exception as e:
                if attempt < RETRY_MAX - 1:
                    await asyncio.sleep(RETRY_DELAY)
                    continue
                return {"status": "error", "close_time": None, "error": str(e)[:100]}
    return {"status": "error", "close_time": None, "error": "max retries exceeded"}


async def check_polymarket(session: aiohttp.ClientSession, slug: str) -> dict[str, Any]:
    if not slug:
        return {"status": "not_found", "end_date": None, "error": "empty slug"}
    url = POLY_API.format(slug=slug)
    async with _poly_sem:
        for attempt in range(RETRY_MAX):
            try:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    if resp.status == 429:
                        await asyncio.sleep(RETRY_DELAY * (attempt + 1))
                        continue
                    if resp.status != 200:
                        if attempt < RETRY_MAX - 1:
                            await asyncio.sleep(RETRY_DELAY)
                            continue
                        return {"status": "error", "end_date": None, "error": f"HTTP {resp.status}"}
                    body = await resp.json()
                    if not body:
                        return {"status": "not_found", "end_date": None}
                    market = body[0]
                    active = market.get("active", False)
                    end_date = market.get("endDate") or market.get("end_date_iso")
                    if active:
                        return {"status": "active", "end_date": end_date}
                    else:
                        return {"status": "closed", "end_date": end_date}
            except asyncio.TimeoutError:
                if attempt < RETRY_MAX - 1:
                    await asyncio.sleep(RETRY_DELAY)
                    continue
                return {"status": "error", "end_date": None, "error": "timeout"}
            except Exception as e:
                if attempt < RETRY_MAX - 1:
                    await asyncio.sleep(RETRY_DELAY)
                    continue
                return {"status": "error", "end_date": None, "error": str(e)[:100]}
    return {"status": "error", "end_date": None, "error": "max retries exceeded"}


def classify_validation(k_status: str, p_status: str) -> tuple[str, str]:
    """Return (validation, reason)."""
    if k_status == "not_found" and p_status == "not_found":
        return "INVALID", "Both sides not found"
    if k_status == "not_found":
        return "INVALID", f"Kalshi ticker not found; Polymarket={p_status}"
    if p_status == "not_found":
        return "INVALID", f"Polymarket slug not found; Kalshi={k_status}"
    if k_status in ("closed", "settled", "finalized") or p_status == "closed":
        return "EXPIRED", f"Kalshi={k_status}, Polymarket={p_status}"
    if k_status == "active" and p_status == "active":
        return "VALID", "Both sides active"
    if k_status == "error" or p_status == "error":
        return "INVALID", f"API error — Kalshi={k_status}, Polymarket={p_status}"
    return "INVALID", f"Unexpected statuses: Kalshi={k_status}, Polymarket={p_status}"


async def validate_batch(
    session: aiohttp.ClientSession,
    records: list[dict],
) -> list[dict]:
    tasks = [
        asyncio.gather(
            check_kalshi(session, r["kalshi_ticker"]),
            check_polymarket(session, r["polymarket_slug"]),
        )
        for r in records
    ]
    results = await asyncio.gather(*tasks)
    output = []
    for record, (k_result, p_result) in zip(records, results):
        validation, reason = classify_validation(k_result["status"], p_result["status"])
        output.append({
            "kalshi_ticker": record["kalshi_ticker"],
            "polymarket_slug": record["polymarket_slug"],
            "description": record["description"],
            "kalshi_status": k_result["status"],
            "polymarket_status": p_result["status"],
            "kalshi_close_time": k_result.get("close_time"),
            "polymarket_end_date": p_result.get("end_date"),
            "validation": validation,
            "reason": reason,
            "source": record["source"],
        })
    return output


# ── Main ──────────────────────────────────────────────────────────────────────

async def main() -> None:
    global _kalshi_sem, _poly_sem
    _kalshi_sem = asyncio.Semaphore(CONCURRENCY)
    _poly_sem = asyncio.Semaphore(CONCURRENCY)

    print("Loading mappings from all sources...")
    all_records = []
    all_records.extend(load_crypto_seeds())
    all_records.extend(load_sports_seeds())
    all_records.extend(load_embedding_seeds())
    print(f"Total mappings to validate: {len(all_records)}")

    results: list[dict] = []
    connector = aiohttp.TCPConnector(limit=20)
    headers = {"User-Agent": "arbiter-validator/1.0"}

    async with aiohttp.ClientSession(connector=connector, headers=headers) as session:
        batches = [all_records[i:i+BATCH_SIZE] for i in range(0, len(all_records), BATCH_SIZE)]
        total_batches = len(batches)
        for idx, batch in enumerate(batches):
            print(f"  Batch {idx+1}/{total_batches} ({len(batch)} records)...", end=" ", flush=True)
            batch_results = await validate_batch(session, batch)
            results.extend(batch_results)
            valid = sum(1 for r in batch_results if r["validation"] == "VALID")
            expired = sum(1 for r in batch_results if r["validation"] == "EXPIRED")
            invalid = sum(1 for r in batch_results if r["validation"] == "INVALID")
            print(f"VALID={valid} EXPIRED={expired} INVALID={invalid}")
            if idx < total_batches - 1:
                await asyncio.sleep(BATCH_DELAY)

    OUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(OUT_FILE, "w") as f:
        json.dump(results, f, indent=2)
    print(f"\nResults written to {OUT_FILE}")

    total = len(results)
    valid = sum(1 for r in results if r["validation"] == "VALID")
    expired = sum(1 for r in results if r["validation"] == "EXPIRED")
    invalid = sum(1 for r in results if r["validation"] == "INVALID")

    print(f"\n{'='*50}")
    print(f"SUMMARY")
    print(f"{'='*50}")
    print(f"Total validated : {total}")
    print(f"VALID           : {valid}")
    print(f"EXPIRED         : {expired}")
    print(f"INVALID         : {invalid}")
    print(f"{'='*50}")

    # breakdown by source
    for source in ["crypto", "sports", "embeddings"]:
        src_results = [r for r in results if r["source"] == source]
        src_valid = sum(1 for r in src_results if r["validation"] == "VALID")
        src_expired = sum(1 for r in src_results if r["validation"] == "EXPIRED")
        src_invalid = sum(1 for r in src_results if r["validation"] == "INVALID")
        print(f"  {source:12}: {len(src_results):4} total | {src_valid:4} valid | {src_expired:4} expired | {src_invalid:4} invalid")


if __name__ == "__main__":
    asyncio.run(main())
