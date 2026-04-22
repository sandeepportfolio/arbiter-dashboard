#!/usr/bin/env python3
"""
Aggressive market mapping discovery for SPORTS, CULTURE, SCIENCE, and CLIMATE categories.

Pulls ALL active markets from both Kalshi and Polymarket, matches them using
the same scoring logic as auto_discovery.py, and writes:
  - scripts/output/market_candidates.json  — all candidate pairs with scores
  - scripts/output/market_seeds.py         — MARKET_SEEDS Python code to append to settings.py

Usage (from repo root):
    python scripts/discover_market_mappings.py

Requires: aiohttp, cryptography, python-dotenv (all in requirements.txt)
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import re
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, AsyncIterator

import aiohttp

# ─── Logging ─────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("discover")

# ─── Config ───────────────────────────────────────────────────────────────────

REPO_ROOT = Path(__file__).resolve().parent.parent
OUTPUT_DIR = REPO_ROOT / "scripts" / "output"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# Load .env from repo root
_env_path = REPO_ROOT / ".env"
if _env_path.exists():
    for line in _env_path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())

KALSHI_BASE_URL = os.environ.get(
    "KALSHI_BASE_URL", "https://api.elections.kalshi.com/trade-api/v2"
)
KALSHI_API_KEY_ID = os.environ.get("KALSHI_API_KEY_ID", "")
KALSHI_PRIVATE_KEY_PATH = os.environ.get(
    "KALSHI_PRIVATE_KEY_PATH", str(REPO_ROOT / "keys" / "kalshi_private.pem")
)
# Resolve relative key paths against repo root
if KALSHI_PRIVATE_KEY_PATH and not Path(KALSHI_PRIVATE_KEY_PATH).is_absolute():
    KALSHI_PRIVATE_KEY_PATH = str(REPO_ROOT / KALSHI_PRIVATE_KEY_PATH)

POLYMARKET_GATEWAY = "https://gateway.polymarket.us"

# Categories we care about (broad matching)
TARGET_KALSHI_CATEGORIES = {
    "sports", "culture", "science", "climate", "weather",
    "entertainment", "technology", "pop culture", "science and technology",
    "climate and weather",
}
TARGET_POLY_CATEGORIES = {
    "sports", "culture", "science", "climate", "weather",
    "entertainment", "technology", "pop culture",
}

# Minimum score to consider a pair a candidate
MIN_SCORE = 0.18
# Maximum candidates to output
MAX_CANDIDATES = 2000

# ─── Kalshi Auth ──────────────────────────────────────────────────────────────

def _load_kalshi_private_key():
    try:
        from cryptography.hazmat.primitives import serialization
        key_path = Path(KALSHI_PRIVATE_KEY_PATH)
        if not key_path.exists():
            logger.warning("Kalshi private key not found at %s — unauthenticated", key_path)
            return None
        with open(key_path, "rb") as f:
            return serialization.load_pem_private_key(f.read(), password=None)
    except Exception as e:
        logger.warning("Could not load Kalshi private key: %s", e)
        return None


_KALSHI_PRIVATE_KEY = None


def get_kalshi_auth_headers(method: str, path: str) -> dict:
    global _KALSHI_PRIVATE_KEY
    if _KALSHI_PRIVATE_KEY is None:
        _KALSHI_PRIVATE_KEY = _load_kalshi_private_key()
    if _KALSHI_PRIVATE_KEY is None or not KALSHI_API_KEY_ID:
        return {"Accept": "application/json"}
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding
    ts = int(time.time() * 1000)
    message = f"{ts}{method}{path}".encode("utf-8")
    sig = _KALSHI_PRIVATE_KEY.sign(
        message,
        padding.PSS(
            mgf=padding.MGF1(hashes.SHA256()),
            salt_length=padding.PSS.DIGEST_LENGTH,
        ),
        hashes.SHA256(),
    )
    return {
        "KALSHI-ACCESS-KEY": KALSHI_API_KEY_ID,
        "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode("utf-8"),
        "KALSHI-ACCESS-TIMESTAMP": str(ts),
        "Accept": "application/json",
    }


# ─── Text normalization (mirrors auto_discovery.py) ───────────────────────────

_TEXT_NORMALIZER = re.compile(r"[^a-z0-9]+")
_DATE_RE = re.compile(r"(20\d{2})[-_/](\d{2})[-_/](\d{2})")
_COMMON_TOKENS = {
    "a", "an", "and", "are", "be", "for", "if", "in", "is", "of", "on", "or",
    "the", "to", "vs", "will", "win", "winner", "yes", "no",
}


def normalize_text(text: str) -> str:
    return _TEXT_NORMALIZER.sub(" ", str(text).lower()).strip()


def market_tokens(text: str) -> set[str]:
    return {
        t for t in normalize_text(text).split()
        if t and (len(t) >= 3 or t.isdigit()) and t not in _COMMON_TOKENS
    }


def similarity_score(a: str, b: str) -> float:
    a_norm = normalize_text(a)
    b_norm = normalize_text(b)
    if not a_norm or not b_norm:
        return 0.0
    if a_norm == b_norm:
        return 1.0
    a_tokens = set(a_norm.split())
    b_tokens = set(b_norm.split())
    if not a_tokens or not b_tokens:
        return 0.0
    intersection = len(a_tokens & b_tokens)
    union = len(a_tokens | b_tokens)
    jaccard = intersection / union if union > 0 else 0.0
    longer = max(len(a_norm), len(b_norm))
    shorter = min(len(a_norm), len(b_norm))
    length_ratio = shorter / longer if longer > 0 else 0.0
    return round((0.7 * jaccard) + (0.3 * length_ratio), 4)


def coerce_date(value: Any) -> date | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    m = _DATE_RE.search(text)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            pass
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        return None


_CATEGORY_ALIASES = {
    "elections": "politics",
    "election": "politics",
    "world": "geopolitics",
    "international": "geopolitics",
    "sport": "sports",
    "science and technology": "science",
    "climate and weather": "climate",
    "pop culture": "culture",
    "entertainment": "culture",
    "sci/tech": "science",
    "weather": "climate",
}


def normalize_category(value: Any) -> str:
    text = normalize_text(str(value or ""))
    if not text:
        return ""
    # Try multi-word match first
    for alias, canonical in _CATEGORY_ALIASES.items():
        if alias in text:
            return canonical
    words = text.split()
    for word in words:
        if word in _CATEGORY_ALIASES:
            return _CATEGORY_ALIASES[word]
        if word in {"sports", "culture", "science", "climate", "politics", "economics",
                    "finance", "crypto", "geopolitics", "tech", "weather"}:
            return word
    return words[0] if words else ""


def candidate_score(
    *,
    kalshi_text: str,
    poly_text: str,
    kalshi_tokens: set[str],
    poly_tokens: set[str],
    kalshi_category: str,
    poly_category: str,
    kalshi_date: date | None,
    poly_date: date | None,
) -> float:
    # Hard block: confirmed category mismatch
    if kalshi_category and poly_category and kalshi_category != poly_category:
        return 0.0

    shared_tokens = kalshi_tokens & poly_tokens
    if not shared_tokens:
        return 0.0

    token_overlap = len(shared_tokens) / max(len(kalshi_tokens | poly_tokens), 1)
    lexical = similarity_score(kalshi_text, poly_text)
    score = (0.65 * lexical) + (0.35 * token_overlap)

    if kalshi_date is not None and poly_date is not None:
        day_delta = abs((kalshi_date - poly_date).days)
        if kalshi_category == "sports" and day_delta > 1:
            return 0.0
        if day_delta > 31:
            return 0.0
        if day_delta == 0:
            score += 0.20
        elif day_delta <= 1:
            score += 0.10
        elif day_delta <= 7:
            score += 0.04
    elif kalshi_category == "sports" and poly_category == "sports":
        # Sports without dates are unreliable
        score *= 0.5

    if kalshi_category and poly_category and kalshi_category == poly_category:
        score += 0.05

    return round(min(score, 0.9999), 4)


# ─── Kalshi text extraction ───────────────────────────────────────────────────

def kalshi_market_text(m: dict) -> str:
    parts = []
    seen: set[str] = set()
    for field_name in ("title", "subtitle", "yes_sub_title", "no_sub_title"):
        val = str(m.get(field_name, "") or "").strip()
        norm = normalize_text(val)
        if val and norm not in seen:
            parts.append(val)
            seen.add(norm)
    if not parts:
        ticker = str(m.get("ticker", "") or "").strip()
        if ticker:
            parts.append(ticker)
    return " ".join(parts)


def is_target_kalshi_category(m: dict) -> bool:
    cat_raw = str(m.get("category", "") or "").lower()
    cat = normalize_category(cat_raw)
    return cat in TARGET_KALSHI_CATEGORIES or any(
        keyword in cat_raw
        for keyword in ("sport", "culture", "science", "climate", "weather", "entertainment")
    )


def is_target_poly_category(m: dict) -> bool:
    cat_raw = str(m.get("category", "") or m.get("groupItemTitle", "") or "").lower()
    cat = normalize_category(cat_raw)
    return cat in TARGET_POLY_CATEGORIES or any(
        keyword in cat_raw
        for keyword in ("sport", "culture", "science", "climate", "weather", "entertainment")
    )


# ─── Kalshi API ───────────────────────────────────────────────────────────────

async def fetch_kalshi_markets(session: aiohttp.ClientSession) -> list[dict]:
    """Fetch all active Kalshi markets with cursor pagination."""
    all_markets: list[dict] = []
    cursor: str | None = None
    page_size = 1000
    max_pages = 30

    for page in range(max_pages):
        path = "/trade-api/v2/markets"
        headers = get_kalshi_auth_headers("GET", path)
        params: dict = {"limit": str(page_size), "status": "open"}
        if cursor:
            params["cursor"] = cursor

        url = f"{KALSHI_BASE_URL}/markets"
        try:
            async with session.get(url, params=params, headers=headers) as resp:
                if resp.status == 401:
                    logger.warning("Kalshi 401 — retrying without auth headers")
                    async with session.get(url, params=params, headers={"Accept": "application/json"}) as resp2:
                        resp2.raise_for_status()
                        data = await resp2.json()
                elif resp.status != 200:
                    text = await resp.text()
                    logger.error("Kalshi markets page %d status %d: %s", page+1, resp.status, text[:300])
                    break
                else:
                    data = await resp.json()
        except Exception as e:
            logger.error("Kalshi markets fetch error page %d: %s", page+1, e)
            break

        markets = data.get("markets", [])
        all_markets.extend(markets)
        logger.info("Kalshi page %d: +%d markets (total %d)", page+1, len(markets), len(all_markets))

        cursor = data.get("cursor") or None
        if not cursor or not markets:
            break

        await asyncio.sleep(0.3)

    return all_markets


async def fetch_kalshi_events(session: aiohttp.ClientSession) -> list[dict]:
    """Fetch all active Kalshi events."""
    all_events: list[dict] = []
    cursor: str | None = None
    page_size = 200
    max_pages = 30

    for page in range(max_pages):
        path = "/trade-api/v2/events"
        headers = get_kalshi_auth_headers("GET", path)
        params: dict = {"limit": str(page_size), "status": "open"}
        if cursor:
            params["cursor"] = cursor

        url = f"{KALSHI_BASE_URL}/events"
        try:
            async with session.get(url, params=params, headers=headers) as resp:
                if resp.status in (401, 403):
                    logger.info("Kalshi events endpoint requires auth or unavailable — skipping")
                    return []
                if resp.status != 200:
                    logger.warning("Kalshi events page %d status %d — stopping", page+1, resp.status)
                    break
                data = await resp.json()
        except Exception as e:
            logger.warning("Kalshi events fetch error page %d: %s", page+1, e)
            break

        events = data.get("events", [])
        all_events.extend(events)
        logger.info("Kalshi events page %d: +%d events (total %d)", page+1, len(events), len(all_events))

        cursor = data.get("cursor") or None
        if not cursor or not events:
            break

        await asyncio.sleep(0.3)

    return all_events


# ─── Polymarket API ───────────────────────────────────────────────────────────

async def fetch_polymarket_markets(session: aiohttp.ClientSession) -> list[dict]:
    """Fetch all active Polymarket markets using public gateway API."""
    all_markets: list[dict] = []
    page_size = 100
    offset = 0
    max_pages = 200

    for page in range(max_pages):
        url = f"{POLYMARKET_GATEWAY}/v1/markets"
        params = {
            "limit": str(page_size),
            "offset": str(offset),
            "active": "true",
        }
        try:
            async with session.get(url, params=params) as resp:
                if resp.status != 200:
                    logger.warning("Polymarket page %d status %d — stopping", page+1, resp.status)
                    break
                data = await resp.json()
        except Exception as e:
            logger.error("Polymarket markets fetch error page %d: %s", page+1, e)
            break

        markets = data.get("markets", [])
        if not markets:
            break

        all_markets.extend(markets)
        logger.info("Polymarket page %d: +%d markets (total %d)", page+1, len(markets), len(all_markets))

        has_more = data.get("hasMore")
        if has_more is False:
            break

        offset += len(markets)
        await asyncio.sleep(0.5)

    return all_markets


# ─── Poly text extraction ─────────────────────────────────────────────────────

def poly_market_text(m: dict) -> str:
    parts: list[str] = []
    seen: set[str] = set()

    def _add(val: str) -> None:
        norm = normalize_text(val)
        if val and norm not in seen:
            parts.append(val)
            seen.add(norm)

    for fname in ("question", "title", "description"):
        _add(str(m.get(fname, "") or ""))

    subject = m.get("subject") or {}
    if isinstance(subject, dict):
        _add(str(subject.get("name", "") or ""))

    for side in m.get("marketSides") or []:
        if not isinstance(side, dict):
            continue
        _add(str(side.get("description", "") or ""))
        team = side.get("team") or {}
        if isinstance(team, dict):
            for f2 in ("name", "alias", "displayAbbreviation", "league"):
                _add(str(team.get(f2, "") or ""))

    if not parts:
        _add(str(m.get("slug", "") or ""))

    return " ".join(parts)


# ─── Matching ─────────────────────────────────────────────────────────────────

def build_poly_index(
    poly_markets: list[dict],
) -> tuple[list[dict], dict[str, set[int]]]:
    entries: list[dict] = []
    index: dict[str, set[int]] = defaultdict(set)

    for pm in poly_markets:
        text = poly_market_text(pm)
        if not text:
            continue
        tokens = market_tokens(text)
        entry = {
            "market": pm,
            "text": text,
            "tokens": tokens,
            "category": normalize_category(pm.get("category") or pm.get("groupItemTitle")),
            "date": coerce_date(pm.get("closeTime") or pm.get("endDate") or pm.get("slug")),
        }
        idx = len(entries)
        entries.append(entry)
        for token in tokens:
            index[token].add(idx)

    return entries, index


def match_markets(
    kalshi_markets: list[dict],
    poly_entries: list[dict],
    poly_index: dict[str, set[int]],
    *,
    min_score: float = MIN_SCORE,
) -> list[dict]:
    candidates: list[dict] = []
    scored = 0

    for km in kalshi_markets:
        k_text = kalshi_market_text(km)
        if not k_text:
            continue
        k_tokens = market_tokens(k_text)
        if not k_tokens:
            continue

        k_cat = normalize_category(km.get("category"))
        k_date = coerce_date(km.get("close_time") or km.get("expiration_time"))

        # Get candidate poly markets via shared tokens
        candidate_idxs: set[int] = set()
        for token in k_tokens:
            candidate_idxs.update(poly_index.get(token, ()))

        for idx in candidate_idxs:
            pm_entry = poly_entries[idx]
            score = candidate_score(
                kalshi_text=k_text,
                poly_text=pm_entry["text"],
                kalshi_tokens=k_tokens,
                poly_tokens=pm_entry["tokens"],
                kalshi_category=k_cat,
                poly_category=pm_entry["category"],
                kalshi_date=k_date,
                poly_date=pm_entry["date"],
            )
            scored += 1
            if score < min_score:
                continue

            pm = pm_entry["market"]
            poly_source = pm.get("resolutionSource")
            kalshi_source = km.get("settlement_source")
            k_date_str = k_date.isoformat() if k_date else None
            pm_date_str = pm_entry["date"].isoformat() if pm_entry["date"] else None

            candidates.append({
                "kalshi_ticker": km.get("ticker", ""),
                "kalshi_title": str(km.get("title", "") or k_text),
                "kalshi_event_ticker": km.get("event_ticker", ""),
                "kalshi_category": k_cat or km.get("category", ""),
                "kalshi_resolution_date": k_date_str,
                "kalshi_resolution_source": kalshi_source,
                "kalshi_status": km.get("status", ""),
                "poly_slug": pm.get("slug", ""),
                "poly_question": str(pm.get("question", "") or pm.get("title", "") or pm_entry["text"]),
                "poly_category": pm_entry["category"] or pm.get("category", ""),
                "poly_resolution_date": pm_date_str,
                "poly_resolution_source": poly_source,
                "score": score,
                "shared_tokens": sorted(k_tokens & pm_entry["tokens"]),
                "description": str(pm.get("question", "") or pm.get("title", "") or k_text),
                "resolution_date": k_date_str or pm_date_str,
                "category": k_cat or pm_entry["category"] or "",
            })

    logger.info("Scored %d pairs, found %d above %.2f threshold", scored, len(candidates), min_score)
    return candidates


def deduplicate_candidates(candidates: list[dict]) -> list[dict]:
    """Keep best-scoring pair for each kalshi ticker and poly slug."""
    candidates.sort(key=lambda c: c["score"], reverse=True)
    used_kalshi: set[str] = set()
    used_poly: set[str] = set()
    result: list[dict] = []

    for c in candidates:
        kt = c["kalshi_ticker"]
        ps = c["poly_slug"]
        if not kt or not ps:
            continue
        if kt in used_kalshi or ps in used_poly:
            continue
        result.append(c)
        used_kalshi.add(kt)
        used_poly.add(ps)

    return result


# ─── Output generators ────────────────────────────────────────────────────────

def canonical_id_from_candidate(c: dict) -> str:
    kt = str(c.get("kalshi_ticker", "") or "").upper()
    ps = str(c.get("poly_slug", "") or "").lower().replace("-", "_")
    digest = hashlib.sha1(f"{kt}|{ps}".encode()).hexdigest()[:8]
    # Make a readable canonical id
    base = re.sub(r"[^A-Z0-9_]", "", kt[:24])
    return f"AUTO_{base}_{digest}".upper()


def generate_seed_python(candidates: list[dict]) -> str:
    """Generate Python MarketMappingRecord entries for the best candidates."""
    lines = [
        "# ─── Auto-discovered market mappings (sports/culture/science/climate) ──────────────",
        "# Generated by scripts/discover_market_mappings.py",
        f"# Date: {date.today().isoformat()}",
        f"# Total candidates: {len(candidates)}",
        "#",
        "# To integrate: append these MarketMappingRecord entries to MARKET_SEEDS in",
        "# arbiter/config/settings.py",
        "",
    ]

    for c in candidates:
        canonical_id = canonical_id_from_candidate(c)
        kt = c.get("kalshi_ticker", "")
        ps = c.get("poly_slug", "")
        desc = c.get("description", "") or c.get("poly_question", "")[:120]
        # Escape quotes
        desc = desc.replace('"', '\\"').replace("'", "\\'")
        score = c.get("score", 0.0)
        category = c.get("category", "")
        res_date = c.get("resolution_date", "")
        poly_q = str(c.get("poly_question", "") or "").replace('"', '\\"').replace("'", "\\'")
        poly_q = poly_q[:200]
        k_cat = c.get("kalshi_category", "")
        p_cat = c.get("poly_category", "")
        k_res = c.get("kalshi_resolution_date", "")
        p_res = c.get("poly_resolution_date", "")
        k_src = c.get("kalshi_resolution_source", "") or ""
        p_src = c.get("poly_resolution_source", "") or ""

        lines.append(f"    MarketMappingRecord(")
        lines.append(f"        canonical_id={canonical_id!r},")
        lines.append(f"        description={desc!r},")
        lines.append(f"        tags=({category!r}, {k_cat!r}),")
        lines.append(f"        kalshi={kt!r},")
        lines.append(f"        polymarket={ps!r},")
        lines.append(f"        polymarket_question={poly_q!r},")
        lines.append(f"        notes=f'Auto-discovered score={score:.4f} kalshi_cat={k_cat!r} poly_cat={p_cat!r} kalshi_res={k_res!r} poly_res={p_res!r}',")
        lines.append(f"        resolution_criteria={{")
        lines.append(f"            'kalshi': {{'source': {k_src!r}, 'settlement_date': {k_res!r}}},")
        lines.append(f"            'polymarket': {{'source': {p_src!r}, 'settlement_date': {p_res!r}}},")
        lines.append(f"            'criteria_match': 'pending_operator_review',")
        lines.append(f"            'operator_note': 'Auto-discovered {date.today().isoformat()} — operator review required before trading',")
        lines.append(f"        }},")
        lines.append(f"        resolution_match_status='pending_operator_review',")
        lines.append(f"        allow_auto_trade=False,")
        lines.append(f"    ),")

    return "\n".join(lines)


# ─── Main ─────────────────────────────────────────────────────────────────────

async def main() -> None:
    logger.info("=== Market Mapping Discovery ===")
    logger.info("Target categories: sports, culture, science, climate")
    logger.info("Kalshi base URL: %s", KALSHI_BASE_URL)
    logger.info("Polymarket gateway: %s", POLYMARKET_GATEWAY)

    timeout = aiohttp.ClientTimeout(total=30, connect=10)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        # ── Fetch Kalshi markets ──────────────────────────────────────────────
        logger.info("--- Fetching Kalshi markets ---")
        kalshi_all = await fetch_kalshi_markets(session)
        logger.info("Kalshi total: %d markets", len(kalshi_all))

        kalshi_target = [m for m in kalshi_all if is_target_kalshi_category(m)]
        logger.info("Kalshi target categories: %d markets", len(kalshi_target))

        # Save raw for debugging
        (OUTPUT_DIR / "kalshi_raw.json").write_text(
            json.dumps(kalshi_all, indent=2, default=str)
        )
        (OUTPUT_DIR / "kalshi_target.json").write_text(
            json.dumps(kalshi_target, indent=2, default=str)
        )

        # Show category breakdown
        cats: dict[str, int] = defaultdict(int)
        for m in kalshi_all:
            cats[str(m.get("category", "unknown") or "unknown")] += 1
        logger.info("Kalshi category breakdown: %s", dict(sorted(cats.items(), key=lambda x: -x[1])[:15]))

        # ── Fetch Kalshi events ───────────────────────────────────────────────
        logger.info("--- Fetching Kalshi events ---")
        kalshi_events = await fetch_kalshi_events(session)
        logger.info("Kalshi events: %d", len(kalshi_events))

        if kalshi_events:
            event_cats: dict[str, int] = defaultdict(int)
            for e in kalshi_events:
                event_cats[str(e.get("category", "unknown") or "unknown")] += 1
            logger.info("Event category breakdown: %s", dict(sorted(event_cats.items(), key=lambda x: -x[1])[:15]))

        # ── Fetch Polymarket markets ──────────────────────────────────────────
        logger.info("--- Fetching Polymarket markets ---")
        poly_all = await fetch_polymarket_markets(session)
        logger.info("Polymarket total: %d markets", len(poly_all))

        poly_target = [m for m in poly_all if is_target_poly_category(m)]
        logger.info("Polymarket target categories: %d markets", len(poly_target))

        # Save raw for debugging
        (OUTPUT_DIR / "polymarket_raw.json").write_text(
            json.dumps(poly_all, indent=2, default=str)
        )

        poly_cats: dict[str, int] = defaultdict(int)
        for m in poly_all:
            poly_cats[str(m.get("category", "unknown") or "unknown")] += 1
        logger.info("Polymarket category breakdown: %s", dict(sorted(poly_cats.items(), key=lambda x: -x[1])[:15]))

    # ── Match: target-category Kalshi vs ALL Polymarket ────────────────────────
    # (also try ALL Kalshi vs target-category Poly for maximum coverage)
    logger.info("--- Matching markets ---")

    poly_entries_all, poly_index_all = build_poly_index(poly_all)
    poly_entries_target, poly_index_target = build_poly_index(poly_target)

    # Round 1: target Kalshi × ALL Polymarket (best for sports/culture)
    logger.info("Round 1: %d target Kalshi vs %d Polymarket (all)", len(kalshi_target), len(poly_all))
    candidates_r1 = match_markets(
        kalshi_target, poly_entries_all, poly_index_all, min_score=MIN_SCORE
    )

    # Round 2: ALL Kalshi × target Polymarket (catches anything missed)
    logger.info("Round 2: %d Kalshi (all) vs %d target Polymarket", len(kalshi_all), len(poly_target))
    candidates_r2 = match_markets(
        kalshi_all, poly_entries_target, poly_index_target, min_score=MIN_SCORE
    )

    all_candidates = candidates_r1 + candidates_r2
    logger.info("Combined candidates before dedup: %d", len(all_candidates))

    candidates = deduplicate_candidates(all_candidates)
    candidates = candidates[:MAX_CANDIDATES]
    logger.info("After dedup and cap: %d candidates", len(candidates))

    # ── Stats ─────────────────────────────────────────────────────────────────
    by_cat: dict[str, int] = defaultdict(int)
    for c in candidates:
        by_cat[c.get("category", "unknown") or "unknown"] += 1
    logger.info("Candidate category breakdown: %s", dict(sorted(by_cat.items(), key=lambda x: -x[1])))

    score_buckets = {"0.8+": 0, "0.6-0.8": 0, "0.4-0.6": 0, "0.2-0.4": 0, "<0.2": 0}
    for c in candidates:
        s = c["score"]
        if s >= 0.8:
            score_buckets["0.8+"] += 1
        elif s >= 0.6:
            score_buckets["0.6-0.8"] += 1
        elif s >= 0.4:
            score_buckets["0.4-0.6"] += 1
        elif s >= 0.2:
            score_buckets["0.2-0.4"] += 1
        else:
            score_buckets["<0.2"] += 1
    logger.info("Score distribution: %s", score_buckets)

    # ── Write outputs ─────────────────────────────────────────────────────────
    out_json = OUTPUT_DIR / "market_candidates.json"
    out_json.write_text(json.dumps(candidates, indent=2, default=str))
    logger.info("Wrote %d candidates to %s", len(candidates), out_json)

    # High-confidence subset for seed file
    high_conf = [c for c in candidates if c["score"] >= 0.55]
    med_conf = [c for c in candidates if 0.35 <= c["score"] < 0.55]
    low_conf = [c for c in candidates if c["score"] < 0.35]

    out_seeds = OUTPUT_DIR / "market_seeds.py"
    seed_code = generate_seed_python(high_conf)
    out_seeds.write_text(seed_code)
    logger.info("Wrote %d high-confidence seed entries to %s", len(high_conf), out_seeds)

    # Also write a summary report
    summary_lines = [
        f"=== Market Mapping Discovery Summary ===",
        f"Date: {date.today().isoformat()}",
        f"",
        f"INPUT",
        f"  Kalshi total markets fetched: {len(kalshi_all)}",
        f"  Kalshi target-category markets: {len(kalshi_target)}",
        f"  Polymarket total markets fetched: {len(poly_all)}",
        f"  Polymarket target-category markets: {len(poly_target)}",
        f"",
        f"OUTPUT (after dedup)",
        f"  Total candidates: {len(candidates)}",
        f"  High confidence (score >= 0.55): {len(high_conf)}",
        f"  Medium confidence (0.35-0.55): {len(med_conf)}",
        f"  Low confidence (<0.35): {len(low_conf)}",
        f"",
        f"CATEGORY BREAKDOWN",
    ]
    for cat, count in sorted(by_cat.items(), key=lambda x: -x[1]):
        summary_lines.append(f"  {cat}: {count}")
    summary_lines += [
        f"",
        f"SCORE DISTRIBUTION",
    ]
    for bucket, count in score_buckets.items():
        summary_lines += [f"  {bucket}: {count}"]
    summary_lines += [
        f"",
        f"TOP 30 CANDIDATES",
    ]
    for c in candidates[:30]:
        summary_lines.append(
            f"  [{c['score']:.3f}] {c['kalshi_ticker']:40s} <-> {c['poly_slug']}"
        )
        summary_lines.append(
            f"         Kalshi: {c.get('kalshi_title', '')[:80]}"
        )
        summary_lines.append(
            f"         Poly:   {c.get('poly_question', '')[:80]}"
        )
        summary_lines.append("")

    out_summary = OUTPUT_DIR / "discovery_summary.txt"
    out_summary.write_text("\n".join(summary_lines))
    print("\n".join(summary_lines))

    logger.info("=== Done ===")
    logger.info("Files written to %s", OUTPUT_DIR)
    logger.info("  market_candidates.json — all %d candidates", len(candidates))
    logger.info("  market_seeds.py        — %d high-conf seed entries", len(high_conf))
    logger.info("  discovery_summary.txt  — human-readable summary")
    logger.info("  kalshi_raw.json        — raw Kalshi API response")
    logger.info("  polymarket_raw.json    — raw Polymarket API response")

    return candidates


if __name__ == "__main__":
    sys.path.insert(0, str(REPO_ROOT))
    candidates = asyncio.run(main())
