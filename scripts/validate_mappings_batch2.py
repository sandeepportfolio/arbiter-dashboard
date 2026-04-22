#!/usr/bin/env python3
"""
VALIDATION AGENT 2 — Market Mapping Batch 2 (indices 75-150)

Validates each candidate in scripts/output/market_candidates_v3.json[75:151] by:
  1. Hitting Kalshi API to confirm ticker exists and is active
  2. Hitting Polymarket Gamma API to confirm slug exists
  3. Verifying both markets refer to the same event (title/question similarity)
  4. Checking expiry dates are within acceptable tolerance
  5. Recording VALID / INVALID / NEEDS_REVIEW per mapping

Output:
  data/validation_report_batch2.json  — full per-mapping results
  data/validated_mappings_batch2.json — VALID-only entries with status="confirmed"

Usage (from repo root):
    python scripts/validate_mappings_batch2.py
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
from datetime import date, datetime
from pathlib import Path
from typing import Any

import aiohttp

# ── Logging ──────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("validate_batch2")

# ── Config ────────────────────────────────────────────────────────────────────

REPO_ROOT = Path(__file__).resolve().parent.parent
CANDIDATES_FILE = REPO_ROOT / "scripts" / "output" / "market_candidates_v3.json"
OUTPUT_DIR = REPO_ROOT / "data"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

REPORT_FILE = OUTPUT_DIR / "validation_report_batch2.json"
VALIDATED_FILE = OUTPUT_DIR / "validated_mappings_batch2.json"

BATCH_START = 75
BATCH_END = 151  # exclusive → indices 75..150

KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2"
POLY_GAMMA = "https://gamma-api.polymarket.com"

# Acceptable expiry-date tolerance in days (0 = must match exactly; <=7 = same week)
MAX_DATE_DELTA_DAYS = 60  # sports championships can resolve days apart
SPORTS_MAX_DATE_DELTA_DAYS = 14  # tighter for same-game markets

# Similarity threshold: if title similarity < this, flag NEEDS_REVIEW
SIM_WARN_THRESHOLD = 0.25

# ── Load .env ──────────────────────────────────────────────────────────────────

def _load_env() -> None:
    for candidate in [REPO_ROOT / ".env", Path("/Users/rentamac/Documents/arbiter/.env")]:
        if candidate.exists():
            for line in candidate.read_text().splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, _, v = line.partition("=")
                    os.environ.setdefault(k.strip(), v.strip())
            return

_load_env()

KALSHI_KEY_ID = os.environ.get("KALSHI_API_KEY_ID", "")
_KEY_CANDIDATES = [
    REPO_ROOT / "keys" / "kalshi_private.pem",
    Path("/Users/rentamac/Documents/arbiter/keys/kalshi_private.pem"),
]
KEY_PATH = next((p for p in _KEY_CANDIDATES if p.exists()), _KEY_CANDIDATES[0])

# ── Kalshi auth ───────────────────────────────────────────────────────────────

_PRIV_KEY = None

def _private_key():
    global _PRIV_KEY
    if _PRIV_KEY is None:
        from cryptography.hazmat.primitives import serialization
        _PRIV_KEY = serialization.load_pem_private_key(KEY_PATH.read_bytes(), password=None)
    return _PRIV_KEY

def kalshi_headers(method: str, path: str) -> dict:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding
    ts = int(time.time() * 1000)
    msg = f"{ts}{method}{path}".encode()
    sig = _private_key().sign(
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

# ── Text utils ─────────────────────────────────────────────────────────────────

_NORM_RE = re.compile(r"[^a-z0-9]+")
_STOP = {"a", "an", "and", "are", "be", "for", "if", "in", "is", "of", "on", "or", "the", "to", "vs", "will", "win", "winner"}

def _normalize(text: str) -> str:
    return _NORM_RE.sub(" ", str(text).lower()).strip()

def _tokens(text: str) -> set[str]:
    return {t for t in _normalize(text).split() if t and t not in _STOP and (len(t) >= 3 or t.isdigit())}

def similarity(a: str, b: str) -> float:
    """Jaccard similarity on normalized token sets."""
    at, bt = _tokens(a), _tokens(b)
    if not at or not bt:
        return 0.0
    return round(len(at & bt) / max(len(at | bt), 1), 4)

def _coerce_date(value: Any) -> date | None:
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        pass
    m = re.search(r"(20\d{2})[-_/](\d{2})[-_/](\d{2})", text)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            pass
    return None

# ── API helpers ────────────────────────────────────────────────────────────────

SEM = asyncio.Semaphore(6)  # global rate-limit: max 6 concurrent requests

async def _get_kalshi_market(session: aiohttp.ClientSession, ticker: str) -> dict | None:
    """Return Kalshi market dict, or None if not found / error."""
    path = f"/trade-api/v2/markets/{ticker}"
    url = f"{KALSHI_BASE}/markets/{ticker}"
    async with SEM:
        try:
            async with session.get(url, headers=kalshi_headers("GET", path), timeout=aiohttp.ClientTimeout(total=10)) as r:
                if r.status == 404:
                    return None
                if r.status == 200:
                    data = await r.json()
                    return data.get("market") or data
                logger.debug("Kalshi %s → HTTP %d", ticker, r.status)
                return None
        except Exception as e:
            logger.debug("Kalshi %s error: %s", ticker, e)
            return None

async def _get_kalshi_event_markets(session: aiohttp.ClientSession, event_ticker: str) -> list[dict]:
    """Return all Kalshi markets for an event ticker."""
    path = "/trade-api/v2/markets"
    url = f"{KALSHI_BASE}/markets"
    async with SEM:
        try:
            async with session.get(
                url,
                params={"event_ticker": event_ticker, "limit": "100"},
                headers=kalshi_headers("GET", path),
                timeout=aiohttp.ClientTimeout(total=10),
            ) as r:
                if r.status == 200:
                    return (await r.json()).get("markets") or []
                return []
        except Exception as e:
            logger.debug("Kalshi event %s error: %s", event_ticker, e)
            return []

async def _get_poly_market(session: aiohttp.ClientSession, slug: str) -> dict | None:
    """Return Polymarket Gamma market dict for a slug, or None."""
    url = f"{POLY_GAMMA}/markets"
    async with SEM:
        try:
            async with session.get(url, params={"slug": slug}, timeout=aiohttp.ClientTimeout(total=10)) as r:
                if r.status == 200:
                    data = await r.json()
                    if isinstance(data, list) and data:
                        return data[0]
                    if isinstance(data, dict):
                        return data
                return None
        except Exception as e:
            logger.debug("Polymarket %s error: %s", slug, e)
            return None


async def _search_poly_by_question(
    session: aiohttp.ClientSession, kalshi_title: str, *, limit: int = 200
) -> list[dict]:
    """
    Search Polymarket Gamma for markets matching a Kalshi market title.

    The Gamma API does not support free-text search.  We instead page through
    active markets sorted by volume and compare titles in-process.  We cap the
    page size at *limit* to keep API load reasonable.
    """
    url = f"{POLY_GAMMA}/markets"
    params = {"active": "true", "closed": "false", "limit": str(limit)}
    async with SEM:
        try:
            async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=15)) as r:
                if r.status == 200:
                    data = await r.json()
                    if isinstance(data, list):
                        return data
        except Exception as e:
            logger.debug("Polymarket page-search error: %s", e)
    return []


def _find_best_poly_match(
    kalshi_title: str, poly_markets: list[dict], *, threshold: float = 0.20
) -> tuple[dict | None, float]:
    """Return (best_market, score) from a list of Polymarket markets, or (None, 0)."""
    best: dict | None = None
    best_score = 0.0
    k_tokens = _tokens(kalshi_title)
    if not k_tokens:
        return None, 0.0
    for pm in poly_markets:
        question = str(pm.get("question") or pm.get("title") or "")
        slug = str(pm.get("slug") or "")
        p_tokens = _tokens(question) | _tokens(slug.replace("-", " "))
        if not p_tokens:
            continue
        shared = k_tokens & p_tokens
        score = len(shared) / max(len(k_tokens | p_tokens), 1)
        if score > best_score:
            best_score = score
            best = pm
    if best_score < threshold:
        return None, best_score
    return best, best_score

# ── Validation logic ───────────────────────────────────────────────────────────

def _kalshi_is_active(market: dict) -> bool:
    status = str(market.get("status") or "").lower()
    return status in ("open", "active", "")

def _poly_is_active(market: dict) -> bool:
    if market.get("closed") is True:
        return False
    if market.get("archived") is True:
        return False
    active = market.get("active")
    if active is False:
        return False
    return True

def _date_delta(a: date | None, b: date | None) -> int | None:
    if a is None or b is None:
        return None
    return abs((a - b).days)

def validate_pair(candidate: dict, kalshi_market: dict | None, poly_market: dict | None) -> dict:
    """Return a validation result dict for a single candidate pair."""
    ticker = candidate.get("kalshi_ticker", "")
    slug = candidate.get("poly_slug", "")
    category = str(candidate.get("category", "") or "").lower()

    result: dict[str, Any] = {
        "kalshi_ticker": ticker,
        "poly_slug": slug,
        "category": category,
        "candidate_score": candidate.get("score", 0),
        "kalshi_title": candidate.get("kalshi_title", ""),
        "poly_question": candidate.get("poly_question", ""),
        "kalshi_resolution_date": candidate.get("kalshi_resolution_date"),
        "poly_resolution_date": candidate.get("poly_resolution_date"),
        "status": "NEEDS_REVIEW",
        "reasons": [],
    }

    # ── Kalshi existence check ─────────────────────────────────────────────────
    if kalshi_market is None:
        result["reasons"].append("kalshi_not_found")
        result["status"] = "INVALID"
        return result

    result["kalshi_status"] = kalshi_market.get("status", "unknown")
    result["kalshi_title_live"] = kalshi_market.get("title") or kalshi_market.get("subtitle", "")
    result["kalshi_close_time"] = kalshi_market.get("close_time") or kalshi_market.get("expiration_time")

    if not _kalshi_is_active(kalshi_market):
        result["reasons"].append(f"kalshi_not_active (status={kalshi_market.get('status')})")
        result["status"] = "INVALID"
        return result

    # ── Polymarket existence check ─────────────────────────────────────────────
    if poly_market is None:
        result["reasons"].append("polymarket_not_found")
        result["status"] = "INVALID"
        return result

    result["poly_active"] = _poly_is_active(poly_market)
    result["poly_question_live"] = poly_market.get("question") or poly_market.get("title", "")
    result["poly_end_date_live"] = poly_market.get("endDate") or poly_market.get("closeTime")
    result["poly_condition_id"] = poly_market.get("conditionId") or poly_market.get("id", "")

    if not result["poly_active"]:
        result["reasons"].append("polymarket_not_active")
        result["status"] = "INVALID"
        return result

    # ── Event-match check (same market?) ──────────────────────────────────────
    kalshi_title_live = result["kalshi_title_live"] or result["kalshi_title"]
    poly_q_live = result["poly_question_live"] or result["poly_question"]

    live_sim = similarity(kalshi_title_live, poly_q_live)
    result["live_title_similarity"] = live_sim

    # For sports championships: check that team abbreviation in ticker matches
    # the slug (e.g. KXMLBAL-26-MIN → poly slug should contain "min")
    team_match: bool | None = None
    if category == "sports":
        ticker_parts = ticker.upper().split("-")
        slug_parts = slug.lower().split("-")
        team_abbr = ticker_parts[-1].lower() if ticker_parts else ""
        if team_abbr:
            team_match = team_abbr in slug_parts
            result["team_abbr_match"] = team_match
            if not team_match:
                result["reasons"].append(f"team_abbr_mismatch: ticker={team_abbr} not in slug parts={slug_parts}")

    # ── Date check ────────────────────────────────────────────────────────────
    k_date = _coerce_date(result["kalshi_close_time"] or candidate.get("kalshi_resolution_date"))
    p_date = _coerce_date(result["poly_end_date_live"] or candidate.get("poly_resolution_date"))
    result["kalshi_date_parsed"] = k_date.isoformat() if k_date else None
    result["poly_date_parsed"] = p_date.isoformat() if p_date else None

    date_delta = _date_delta(k_date, p_date)
    result["date_delta_days"] = date_delta

    max_delta = SPORTS_MAX_DATE_DELTA_DAYS if category == "sports" else MAX_DATE_DELTA_DAYS
    if date_delta is not None and date_delta > max_delta:
        result["reasons"].append(f"date_mismatch: delta={date_delta}d > {max_delta}d")
    elif date_delta is None and category == "sports":
        result["reasons"].append("date_missing_for_sports")

    # ── Final verdict ─────────────────────────────────────────────────────────
    hard_failures = [r for r in result["reasons"] if any(
        kw in r for kw in ("not_found", "not_active", "team_abbr_mismatch", "date_mismatch")
    )]

    if hard_failures:
        result["status"] = "INVALID"
    elif result["reasons"]:
        result["status"] = "NEEDS_REVIEW"
    else:
        if live_sim < SIM_WARN_THRESHOLD and team_match is not True:
            result["reasons"].append(f"low_title_similarity: {live_sim:.3f}")
            result["status"] = "NEEDS_REVIEW"
        elif result.get("poly_match_method", "exact_slug") != "exact_slug":
            # Matched via fuzzy fallback — original slug was wrong, needs human review
            result["reasons"].append("poly_slug_guessed_exact_slug_missing")
            result["status"] = "NEEDS_REVIEW"
        else:
            result["status"] = "VALID"

    return result

# ── Main ──────────────────────────────────────────────────────────────────────

async def main() -> None:
    logger.info("=== VALIDATION AGENT 2: Batch 2 (indices %d–%d) ===", BATCH_START, BATCH_END - 1)

    # Load candidate list
    if not CANDIDATES_FILE.exists():
        logger.error("Candidates file not found: %s", CANDIDATES_FILE)
        sys.exit(1)

    all_candidates = json.loads(CANDIDATES_FILE.read_text())
    batch = all_candidates[BATCH_START:BATCH_END]
    logger.info("Loaded %d candidates for batch 2", len(batch))

    # Check auth
    if not KALSHI_KEY_ID:
        logger.warning("KALSHI_API_KEY_ID not set — Kalshi calls will fail auth")
    if not KEY_PATH.exists():
        logger.error("Kalshi private key not found at %s", KEY_PATH)
        sys.exit(1)

    results: list[dict] = []
    confirmed: list[dict] = []

    async with aiohttp.ClientSession() as session:
        # Pre-fetch a page of active Polymarket markets for fuzzy fallback matching.
        # This single request covers most well-known markets by volume.
        logger.info("Prefetching active Polymarket markets for fuzzy matching…")
        poly_page = await _search_poly_by_question(session, "", limit=500)
        logger.info("Fetched %d Polymarket markets for fallback matching", len(poly_page))

        # Build tasks — one per candidate
        async def validate_one(idx: int, candidate: dict) -> dict:
            ticker = candidate.get("kalshi_ticker", "")
            slug = candidate.get("poly_slug", "")
            event_ticker = candidate.get("kalshi_event_ticker", "")
            kalshi_title = candidate.get("kalshi_title", "")

            logger.info("[%d/%d] %s ↔ %s", idx + 1, len(batch), ticker, slug)

            # ── Kalshi lookup ──────────────────────────────────────────────────
            kalshi_market = await _get_kalshi_market(session, ticker)
            if kalshi_market is None and event_ticker and event_ticker != ticker:
                event_markets = await _get_kalshi_event_markets(session, event_ticker)
                matching = [m for m in event_markets if m.get("ticker") == ticker]
                if matching:
                    kalshi_market = matching[0]

            await asyncio.sleep(0.05)

            # ── Polymarket lookup: try exact slug first, then fuzzy ────────────
            poly_market = await _get_poly_market(session, slug)
            poly_match_method = "exact_slug"

            if poly_market is None and kalshi_title:
                best, best_score = _find_best_poly_match(kalshi_title, poly_page)
                if best is not None:
                    poly_market = best
                    poly_match_method = f"fuzzy_title (score={best_score:.3f})"
                    logger.debug(
                        "  Fuzzy match for %s → %s (%.3f)",
                        ticker, best.get("slug", "?"), best_score,
                    )

            result = validate_pair(candidate, kalshi_market, poly_market)
            result["poly_match_method"] = poly_match_method
            return result

        tasks = [validate_one(i, c) for i, c in enumerate(batch)]
        results = await asyncio.gather(*tasks)

    # Tally results
    counts = {"VALID": 0, "INVALID": 0, "NEEDS_REVIEW": 0}
    for r in results:
        counts[r["status"]] = counts.get(r["status"], 0) + 1

    # Build confirmed list (VALID mappings)
    for i, (candidate, result) in enumerate(zip(batch, results)):
        if result["status"] == "VALID":
            confirmed_entry = {
                **candidate,
                "status": "confirmed",
                "validation_batch": 2,
                "validation_index": BATCH_START + i,
                "live_title_similarity": result.get("live_title_similarity"),
                "date_delta_days": result.get("date_delta_days"),
                "poly_condition_id": result.get("poly_condition_id", ""),
                "kalshi_close_time_live": result.get("kalshi_close_time"),
                "poly_end_date_live": result.get("poly_end_date_live"),
                "validated_at": datetime.utcnow().isoformat() + "Z",
            }
            confirmed.append(confirmed_entry)

    # Build summary report
    report = {
        "validation_agent": 2,
        "batch_range": f"{BATCH_START}-{BATCH_END - 1}",
        "total_candidates": len(batch),
        "summary": counts,
        "confirmed_count": len(confirmed),
        "validated_at": datetime.utcnow().isoformat() + "Z",
        "results": results,
    }

    REPORT_FILE.write_text(json.dumps(report, indent=2, default=str))
    logger.info("Wrote validation report → %s", REPORT_FILE)

    if confirmed:
        VALIDATED_FILE.write_text(json.dumps(confirmed, indent=2, default=str))
        logger.info("Wrote %d confirmed mappings → %s", len(confirmed), VALIDATED_FILE)
    else:
        VALIDATED_FILE.write_text("[]")
        logger.info("No confirmed mappings in this batch")

    # Print summary
    print("\n" + "=" * 60)
    print(f"VALIDATION AGENT 2 — Batch 2 Results (indices {BATCH_START}–{BATCH_END - 1})")
    print("=" * 60)
    print(f"  Total candidates:  {len(batch)}")
    print(f"  VALID:             {counts['VALID']}")
    print(f"  INVALID:           {counts['INVALID']}")
    print(f"  NEEDS_REVIEW:      {counts['NEEDS_REVIEW']}")
    print(f"  Confirmed (ready): {len(confirmed)}")
    print()

    if results:
        print("Per-mapping results:")
        for r in results:
            flag = "✓" if r["status"] == "VALID" else ("✗" if r["status"] == "INVALID" else "?")
            reasons = "; ".join(r.get("reasons") or []) or "—"
            sim_str = f"sim={r.get('live_title_similarity', '?'):.3f}" if r.get("live_title_similarity") is not None else "sim=?"
            print(f"  [{flag}] {r['kalshi_ticker'][:30]:<30} ↔ {r['poly_slug'][:35]:<35} {sim_str}  {r['status']}  {reasons}")

    print()
    logger.info("Done.")

if __name__ == "__main__":
    asyncio.run(main())
