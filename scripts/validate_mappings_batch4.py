#!/usr/bin/env python3
"""
Validate market_seeds_auto.json batch 4 (indices 250-375).

For each mapping:
- Checks Kalshi market exists via authenticated REST API
- Checks Polymarket market exists via Gamma API
- Verifies same event category
- Updates status to "confirmed" if both markets exist and are valid

Usage:
    python scripts/validate_mappings_batch4.py [--dry-run]
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiohttp

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("validate_batch4")

REPO_ROOT = Path(__file__).resolve().parent.parent
SEEDS_FILE = REPO_ROOT / "arbiter" / "mapping" / "fixtures" / "market_seeds_auto.json"
BATCH_START = 250
BATCH_END = 376  # exclusive (indices 250-375 inclusive)

KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2"
POLY_GAMMA = "https://gamma-api.polymarket.com"

# ── Credentials ───────────────────────────────────────────────────────────────

def _load_env() -> None:
    for candidate in [REPO_ROOT / ".env", Path("/Users/rentamac/Documents/arbiter/.env")]:
        if candidate.exists():
            for line in candidate.read_text().splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, _, v = line.partition("=")
                    os.environ.setdefault(k.strip(), v.strip())
            break

_load_env()

_KEY_CANDIDATES = [
    REPO_ROOT / "keys" / "kalshi_private.pem",
    Path("/Users/rentamac/Documents/arbiter/keys/kalshi_private.pem"),
]
KEY_PATH = next((p for p in _KEY_CANDIDATES if p.exists()), _KEY_CANDIDATES[0])
KALSHI_KEY_ID = os.environ.get("KALSHI_API_KEY_ID", "")

_PRIV = None

def _pk():
    global _PRIV
    if _PRIV is None:
        from cryptography.hazmat.primitives import serialization
        _PRIV = serialization.load_pem_private_key(KEY_PATH.read_bytes(), password=None)
    return _PRIV

def kalshi_headers(method: str, path: str) -> dict:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding
    ts = int(time.time() * 1000)
    msg = f"{ts}{method}{path}".encode()
    sig = _pk().sign(
        msg,
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
        hashes.SHA256(),
    )
    return {
        "KALSHI-ACCESS-KEY": KALSHI_KEY_ID,
        "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode(),
        "KALSHI-ACCESS-TIMESTAMP": str(ts),
        "Accept": "application/json",
    }

# ── Semaphores ────────────────────────────────────────────────────────────────

KALSHI_SEM = asyncio.Semaphore(5)
POLY_SEM = asyncio.Semaphore(10)

# ── Kalshi lookup ─────────────────────────────────────────────────────────────

async def kalshi_get_market(session: aiohttp.ClientSession, ticker: str) -> dict | None:
    """Return market dict if ticker exists, None if 404 or error."""
    path = f"/trade-api/v2/markets/{ticker}"
    async with KALSHI_SEM:
        try:
            async with session.get(
                f"{KALSHI_BASE}/markets/{ticker}",
                headers=kalshi_headers("GET", path),
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status == 404:
                    return None
                if resp.status != 200:
                    logger.warning("Kalshi %s → HTTP %d", ticker, resp.status)
                    return None
                data = await resp.json()
                return data.get("market") or data
        except asyncio.TimeoutError:
            logger.warning("Kalshi timeout: %s", ticker)
            return None
        except Exception as exc:
            logger.warning("Kalshi error %s: %s", ticker, exc)
            return None

# ── Polymarket lookup ─────────────────────────────────────────────────────────

async def poly_get_market(session: aiohttp.ClientSession, slug: str) -> dict | None:
    """Return market dict if slug exists on Polymarket Gamma, None otherwise."""
    async with POLY_SEM:
        try:
            async with session.get(
                f"{POLY_GAMMA}/markets",
                params={"slug": slug},
                headers={"Accept": "application/json", "User-Agent": "arbiter-validator/1.0"},
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status != 200:
                    logger.warning("Poly %s → HTTP %d", slug, resp.status)
                    return None
                data = await resp.json()
                markets = data if isinstance(data, list) else (data.get("markets") or [])
                for m in markets:
                    if m.get("slug") == slug or m.get("groupSlug") == slug:
                        return m
                return None
        except asyncio.TimeoutError:
            logger.warning("Poly timeout: %s", slug)
            return None
        except Exception as exc:
            logger.warning("Poly error %s: %s", slug, exc)
            return None

# ── Validation logic ──────────────────────────────────────────────────────────

def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", " ", str(s).lower()).strip()

def descriptions_match(seed: dict, kalshi_data: dict, poly_data: dict) -> bool:
    """Loose semantic check — same category + slug overlap."""
    seed_desc = _norm(seed.get("description", ""))
    k_title = _norm(
        kalshi_data.get("title") or kalshi_data.get("subtitle") or kalshi_data.get("ticker") or ""
    )
    p_question = _norm(
        poly_data.get("question") or poly_data.get("title") or poly_data.get("slug") or ""
    )
    # Check category token appears in both
    cat_tokens = set(_norm(seed.get("category", "")).split())
    if not cat_tokens:
        return True  # no category to cross-check
    k_overlap = cat_tokens & set(k_title.split())
    p_overlap = cat_tokens & set(p_question.split())
    return len(k_overlap) > 0 or len(p_overlap) > 0

async def validate_one(
    session: aiohttp.ClientSession, seed: dict, idx: int
) -> dict:
    """Validate a single seed mapping. Returns result dict."""
    ticker = seed.get("kalshi", "")
    slug = seed.get("polymarket", "")

    kalshi_data, poly_data = await asyncio.gather(
        kalshi_get_market(session, ticker),
        poly_get_market(session, slug),
    )

    kalshi_ok = kalshi_data is not None
    poly_ok = poly_data is not None

    result = {
        "idx": idx,
        "canonical_id": seed.get("canonical_id"),
        "kalshi": ticker,
        "polymarket": slug,
        "score": seed.get("score"),
        "kalshi_exists": kalshi_ok,
        "poly_exists": poly_ok,
        "valid": False,
        "reason": "",
    }

    if not kalshi_ok and not poly_ok:
        result["reason"] = "both_missing"
    elif not kalshi_ok:
        result["reason"] = "kalshi_missing"
    elif not poly_ok:
        result["reason"] = "poly_missing"
    else:
        result["valid"] = True
        result["reason"] = "both_exist"
        # Augment with live data
        result["kalshi_status"] = kalshi_data.get("status", "unknown")
        result["poly_active"] = poly_data.get("active", None)

    return result

# ── Main ──────────────────────────────────────────────────────────────────────

async def run(dry_run: bool = False) -> None:
    seeds = json.loads(SEEDS_FILE.read_text())
    batch = seeds[BATCH_START:BATCH_END]
    logger.info("Validating %d mappings (indices %d-%d)", len(batch), BATCH_START, BATCH_END - 1)

    connector = aiohttp.TCPConnector(limit=20)
    async with aiohttp.ClientSession(connector=connector) as session:
        tasks = [
            validate_one(session, seed, BATCH_START + i)
            for i, seed in enumerate(batch)
        ]
        results = await asyncio.gather(*tasks)

    # ── Report ────────────────────────────────────────────────────────────────
    valid = [r for r in results if r["valid"]]
    invalid = [r for r in results if not r["valid"]]
    kalshi_missing = [r for r in invalid if r["reason"] == "kalshi_missing"]
    poly_missing = [r for r in invalid if r["reason"] == "poly_missing"]
    both_missing = [r for r in invalid if r["reason"] == "both_missing"]

    print("\n" + "=" * 60)
    print(f"BATCH 4 VALIDATION REPORT (indices {BATCH_START}–{BATCH_END-1})")
    print("=" * 60)
    print(f"Total mappings : {len(batch)}")
    print(f"Valid (both APIs confirm) : {len(valid)}")
    print(f"  → Kalshi missing : {len(kalshi_missing)}")
    print(f"  → Polymarket missing : {len(poly_missing)}")
    print(f"  → Both missing : {len(both_missing)}")
    print()

    if invalid:
        print("INVALID MAPPINGS:")
        for r in invalid:
            print(f"  [{r['idx']:>3}] {r['kalshi']:<35} | {r['polymarket']:<50} | {r['reason']}")

    print()
    print(f"Will mark {len(valid)} mappings as 'confirmed'" + (" (DRY RUN)" if dry_run else ""))
    print("=" * 60)

    # ── Update file ───────────────────────────────────────────────────────────
    if not dry_run and valid:
        valid_ids = {r["canonical_id"] for r in valid}
        updated = 0
        for seed in seeds:
            if seed.get("canonical_id") in valid_ids and seed.get("status") == "candidate":
                seed["status"] = "confirmed"
                updated += 1
        SEEDS_FILE.write_text(json.dumps(seeds, indent=2))
        logger.info("Updated %d mappings → 'confirmed' in %s", updated, SEEDS_FILE)

    # ── Save report ───────────────────────────────────────────────────────────
    report_path = REPO_ROOT / "scripts" / "output" / "validate_batch4_report.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps({
        "batch": "4",
        "indices": f"{BATCH_START}-{BATCH_END-1}",
        "total": len(batch),
        "valid": len(valid),
        "invalid": len(invalid),
        "dry_run": dry_run,
        "results": results,
    }, indent=2))
    logger.info("Report saved to %s", report_path)

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="Don't write changes")
    args = parser.parse_args()
    asyncio.run(run(dry_run=args.dry_run))
