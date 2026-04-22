#!/usr/bin/env python3
"""
V2 market mapping discovery using Kalshi EVENTS endpoint.

This is more targeted than v1 — uses event-level text for matching to avoid
the 30k multi-leg prop market noise, then fetches actual market tickers for
high-confidence matches.

Focus categories: Sports, Entertainment, Climate and Weather, Science and Technology
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
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

import aiohttp

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
logger = logging.getLogger("discover_v2")

REPO_ROOT = Path(__file__).resolve().parent.parent
OUTPUT_DIR = REPO_ROOT / "scripts" / "output"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# Load env — check worktree, then main repo root
def _load_env() -> None:
    for candidate in [
        REPO_ROOT / ".env",
        Path("/Users/rentamac/Documents/arbiter/.env"),
    ]:
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
BASE = "https://api.elections.kalshi.com/trade-api/v2"

TARGET_KALSHI_CATS = {"Sports", "Entertainment", "Climate and Weather", "Science and Technology", "Social", "World"}

# ─── Auth ─────────────────────────────────────────────────────────────────────

_PRIVATE_KEY = None

def private_key():
    global _PRIVATE_KEY
    if _PRIVATE_KEY is None:
        from cryptography.hazmat.primitives import serialization
        _PRIVATE_KEY = serialization.load_pem_private_key(KEY_PATH.read_bytes(), password=None)
    return _PRIVATE_KEY


def kalshi_headers(method: str, path: str) -> dict:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding
    ts = int(time.time() * 1000)
    msg = f"{ts}{method}{path}".encode()
    sig = private_key().sign(msg, padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH), hashes.SHA256())
    return {
        "KALSHI-ACCESS-KEY": KALSHI_KEY_ID,
        "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode(),
        "KALSHI-ACCESS-TIMESTAMP": str(ts),
        "Accept": "application/json",
    }


# ─── Text utils ───────────────────────────────────────────────────────────────

_NORM = re.compile(r"[^a-z0-9]+")
_DATE_RE = re.compile(r"(20\d{2})[-_/](\d{2})[-_/](\d{2})")
_STOP = {"a","an","and","are","be","for","if","in","is","of","on","or","the","to","vs","will","win","winner","yes","no","who","what","when","where","which"}


def norm(text: str) -> str:
    return _NORM.sub(" ", str(text).lower()).strip()


def tokens(text: str) -> set[str]:
    return {t for t in norm(text).split() if t and (len(t) >= 3 or t.isdigit()) and t not in _STOP}


def coerce_date(v: Any) -> date | None:
    if not v:
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    s = str(v).strip()
    m = _DATE_RE.search(s)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            pass
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def sim(a: str, b: str) -> float:
    a_n, b_n = norm(a), norm(b)
    if not a_n or not b_n:
        return 0.0
    if a_n == b_n:
        return 1.0
    at, bt = set(a_n.split()), set(b_n.split())
    if not at or not bt:
        return 0.0
    j = len(at & bt) / max(len(at | bt), 1)
    lr = min(len(a_n), len(b_n)) / max(len(a_n), len(b_n))
    return round(0.7 * j + 0.3 * lr, 4)


_CAT_MAP = {
    "entertainment": "culture",
    "pop culture": "culture",
    "sci/tech": "science",
    "science and technology": "science",
    "climate and weather": "climate",
    "weather": "climate",
    "social": "culture",
    "world": "science",
}


def norm_cat(v: Any) -> str:
    t = norm(str(v or ""))
    for key, val in _CAT_MAP.items():
        if key in t:
            return val
    for word in t.split():
        if word in {"sports", "culture", "science", "climate", "politics", "economics", "finance", "crypto"}:
            return word
    return t.split()[0] if t.split() else ""


def score_pair(
    k_text: str, p_text: str,
    k_cat: str, p_cat: str,
    k_date: date | None, p_date: date | None,
) -> float:
    if k_cat and p_cat and k_cat != p_cat:
        return 0.0
    kt, pt = tokens(k_text), tokens(p_text)
    shared = kt & pt
    if not shared:
        return 0.0
    overlap = len(shared) / max(len(kt | pt), 1)
    lexical = sim(k_text, p_text)
    score = 0.65 * lexical + 0.35 * overlap

    if k_date and p_date:
        delta = abs((k_date - p_date).days)
        if k_cat == "sports" and delta > 1:
            return 0.0
        if delta > 31:
            return 0.0
        score += 0.20 if delta == 0 else 0.10 if delta <= 1 else 0.04 if delta <= 7 else 0.0
    elif k_cat == "sports" and p_cat == "sports":
        score *= 0.5

    if k_cat and p_cat and k_cat == p_cat:
        score += 0.05

    return round(min(score, 0.9999), 4)


# ─── Kalshi events loader ─────────────────────────────────────────────────────

def event_text(e: dict) -> str:
    parts = []
    seen: set[str] = set()
    for f in ("title", "sub_title", "series_ticker", "event_ticker"):
        v = str(e.get(f, "") or "").strip()
        n = norm(v)
        if v and n not in seen:
            parts.append(v)
            seen.add(n)
    return " ".join(parts)


# ─── Kalshi market fetcher ────────────────────────────────────────────────────

_SEMAPHORE: asyncio.Semaphore | None = None


async def fetch_markets_for_event(session: aiohttp.ClientSession, event_ticker: str) -> list[dict]:
    global _SEMAPHORE
    if _SEMAPHORE is None:
        _SEMAPHORE = asyncio.Semaphore(6)
    async with _SEMAPHORE:
        path = "/trade-api/v2/markets"
        headers = kalshi_headers("GET", path)
        try:
            async with session.get(
                f"{BASE}/markets",
                params={"event_ticker": event_ticker, "limit": "100"},
                headers=headers,
            ) as r:
                if r.status != 200:
                    return []
                data = await r.json()
                return list(data.get("markets") or [])
        except Exception as e:
            logger.debug("Error fetching markets for %s: %s", event_ticker, e)
            return []


# ─── Polymarket text extractor ────────────────────────────────────────────────

def poly_text(m: dict) -> str:
    parts: list[str] = []
    seen: set[str] = set()
    def add(v: str) -> None:
        n = norm(v)
        if v and n not in seen:
            parts.append(v)
            seen.add(n)
    for f in ("question", "title", "description"):
        add(str(m.get(f, "") or ""))
    subj = m.get("subject") or {}
    if isinstance(subj, dict):
        add(str(subj.get("name", "") or ""))
    for side in m.get("marketSides") or []:
        if not isinstance(side, dict):
            continue
        add(str(side.get("description", "") or ""))
        team = side.get("team") or {}
        if isinstance(team, dict):
            for f2 in ("name", "alias", "displayAbbreviation", "league"):
                add(str(team.get(f2, "") or ""))
    if not parts:
        add(str(m.get("slug", "") or ""))
    return " ".join(parts)


# ─── Main ─────────────────────────────────────────────────────────────────────

async def main() -> list[dict]:
    logger.info("=== V2 Events-Based Discovery ===")

    # ── Load Kalshi events ────────────────────────────────────────────────────
    events_cache = Path("/tmp/kalshi_events.json")
    if events_cache.exists():
        all_events = json.loads(events_cache.read_text())
        logger.info("Loaded %d events from cache", len(all_events))
    else:
        logger.error("Events cache not found. Run fetch-events first.")
        return []

    target_events = [e for e in all_events if e.get("category", "") in TARGET_KALSHI_CATS]
    logger.info("Target category events: %d", len(target_events))

    cats = Counter(e.get("category", "") for e in target_events)
    logger.info("Target cat breakdown: %s", dict(cats.most_common()))

    # ── Load Polymarket markets ───────────────────────────────────────────────
    poly_path = OUTPUT_DIR / "polymarket_raw.json"
    if not poly_path.exists():
        logger.error("polymarket_raw.json not found. Run discover_market_mappings.py first.")
        return []

    poly_all = json.loads(poly_path.read_text())
    logger.info("Loaded %d Polymarket markets", len(poly_all))

    # Build poly index
    poly_entries: list[dict] = []
    poly_index: dict[str, set[int]] = defaultdict(set)
    for pm in poly_all:
        txt = poly_text(pm)
        if not txt:
            continue
        t = tokens(txt)
        entry = {
            "market": pm,
            "text": txt,
            "tokens": t,
            "category": norm_cat(pm.get("category") or pm.get("groupItemTitle")),
            "date": coerce_date(pm.get("closeTime") or pm.get("endDate")),
            "slug": str(pm.get("slug", "") or ""),
        }
        idx = len(poly_entries)
        poly_entries.append(entry)
        for tok in t:
            poly_index[tok].add(idx)

    # ── Phase 1: Match events against Polymarket ──────────────────────────────
    logger.info("Phase 1: matching %d events against %d Polymarket markets ...", len(target_events), len(poly_entries))
    event_matches: list[tuple[float, dict, dict]] = []  # (score, event, poly_entry)

    for i, ev in enumerate(target_events):
        if i % 500 == 0 and i > 0:
            logger.info("  ... %d/%d events processed", i, len(target_events))

        e_text = event_text(ev)
        if not e_text:
            continue
        e_tokens = tokens(e_text)
        if not e_tokens:
            continue
        e_cat = norm_cat(ev.get("category"))
        e_date = coerce_date(ev.get("sub_title"))  # sometimes has date in subtitle

        # Get candidate poly indexes via token overlap
        cand_idxs: set[int] = set()
        for tok in e_tokens:
            cand_idxs.update(poly_index.get(tok, ()))

        for idx in cand_idxs:
            pm_entry = poly_entries[idx]
            s = score_pair(e_text, pm_entry["text"], e_cat, pm_entry["category"], e_date, pm_entry["date"])
            if s >= 0.22:
                event_matches.append((s, ev, pm_entry))

    event_matches.sort(key=lambda x: x[0], reverse=True)
    logger.info("Phase 1 done: %d event-poly matches at score >= 0.22", len(event_matches))

    # Dedupe: keep best score per event + per poly slug
    used_events: set[str] = set()
    used_slugs: set[str] = set()
    top_event_matches: list[tuple[float, dict, dict]] = []
    for s, ev, pm_entry in event_matches:
        et = ev.get("event_ticker", "")
        sl = pm_entry["slug"]
        if et in used_events or sl in used_slugs:
            continue
        top_event_matches.append((s, ev, pm_entry))
        used_events.add(et)
        used_slugs.add(sl)

    logger.info("After dedup: %d unique event-poly pairs to fetch markets for", len(top_event_matches))

    # ── Phase 2: Fetch markets for matched events ─────────────────────────────
    # Only fetch for events with score >= 0.30 to minimize API calls
    high_conf_matches = [(s, ev, pm_entry) for s, ev, pm_entry in top_event_matches if s >= 0.28]
    low_conf_matches = [(s, ev, pm_entry) for s, ev, pm_entry in top_event_matches if s < 0.28]
    logger.info("Phase 2: fetching markets for %d high-conf events (>=0.28 score)", len(high_conf_matches))

    timeout = aiohttp.ClientTimeout(total=15, connect=5)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async def fetch_with_rate_limit(ev: dict) -> tuple[dict, list[dict]]:
            await asyncio.sleep(0.12)  # ~8 rps
            markets = await fetch_markets_for_event(session, ev.get("event_ticker", ""))
            return ev, markets

        tasks = [fetch_with_rate_limit(ev) for _, ev, _ in high_conf_matches]
        results = await asyncio.gather(*tasks, return_exceptions=True)

    event_to_markets: dict[str, list[dict]] = {}
    for i, result in enumerate(results):
        if isinstance(result, Exception):
            ev = high_conf_matches[i][1]
            logger.debug("Error fetching markets for %s: %s", ev.get("event_ticker"), result)
            event_to_markets[ev.get("event_ticker", "")] = []
        else:
            ev, markets = result
            event_to_markets[ev.get("event_ticker", "")] = markets
            if i % 50 == 0 and i > 0:
                logger.info("  ... fetched markets for %d/%d events", i, len(high_conf_matches))

    logger.info("Phase 2 done: fetched markets for %d events", len(event_to_markets))

    # ── Phase 3: Build candidates ─────────────────────────────────────────────
    candidates: list[dict] = []
    used_kalshi_tickers: set[str] = set()
    used_poly_slugs_final: set[str] = set()

    # High-confidence: use real market tickers
    for s, ev, pm_entry in high_conf_matches:
        et = ev.get("event_ticker", "")
        markets = event_to_markets.get(et, [])

        # Filter out multi-leg markets
        single_markets = [
            m for m in markets
            if not str(m.get("ticker", "")).startswith(("KXMVE", "KXCROSS"))
        ]

        pm = pm_entry["market"]
        slug = pm_entry["slug"]

        if single_markets:
            # Use the best-matching market under this event
            best_market = single_markets[0]
            for m in single_markets:
                m_text = " ".join(filter(None, [m.get("title"), m.get("subtitle"), m.get("yes_sub_title")]))
                m_score = score_pair(
                    m_text, pm_entry["text"],
                    norm_cat(ev.get("category")), pm_entry["category"],
                    coerce_date(m.get("close_time") or m.get("expiration_time")),
                    pm_entry["date"],
                )
                if m_score > s:
                    best_market = m

            ticker = best_market.get("ticker", "")
        else:
            # Use event_ticker as proxy (can be refined later)
            ticker = et

        if not ticker or not slug:
            continue
        if ticker in used_kalshi_tickers or slug in used_poly_slugs_final:
            continue

        k_cat = norm_cat(ev.get("category"))
        p_cat = pm_entry["category"]
        k_date = coerce_date(ev.get("sub_title"))
        p_date = pm_entry["date"]

        candidates.append({
            "kalshi_ticker": ticker,
            "kalshi_event_ticker": et,
            "kalshi_title": ev.get("title", ""),
            "kalshi_subtitle": ev.get("sub_title", ""),
            "kalshi_category": ev.get("category", ""),
            "kalshi_resolution_date": k_date.isoformat() if k_date else None,
            "poly_slug": slug,
            "poly_question": str(pm.get("question", "") or pm.get("title", "") or pm_entry["text"]),
            "poly_category": str(pm.get("category", "") or ""),
            "poly_resolution_date": p_date.isoformat() if p_date else None,
            "poly_resolution_source": pm.get("resolutionSource"),
            "score": s,
            "shared_tokens": sorted(tokens(event_text(ev)) & pm_entry["tokens"]),
            "description": ev.get("title", "") or pm_entry["text"],
            "category": k_cat or p_cat,
            "resolution_date": (k_date or p_date).isoformat() if (k_date or p_date) else None,
            "phase": "high_conf",
        })
        used_kalshi_tickers.add(ticker)
        used_poly_slugs_final.add(slug)

    # Low-confidence: use event_ticker as placeholder (for review, not trading)
    for s, ev, pm_entry in low_conf_matches[:300]:
        et = ev.get("event_ticker", "")
        slug = pm_entry["slug"]
        if et in used_kalshi_tickers or slug in used_poly_slugs_final:
            continue

        pm = pm_entry["market"]
        k_date = coerce_date(ev.get("sub_title"))
        p_date = pm_entry["date"]
        k_cat = norm_cat(ev.get("category"))

        candidates.append({
            "kalshi_ticker": et,
            "kalshi_event_ticker": et,
            "kalshi_title": ev.get("title", ""),
            "kalshi_subtitle": ev.get("sub_title", ""),
            "kalshi_category": ev.get("category", ""),
            "kalshi_resolution_date": k_date.isoformat() if k_date else None,
            "poly_slug": slug,
            "poly_question": str(pm.get("question", "") or pm.get("title", "") or pm_entry["text"]),
            "poly_category": str(pm.get("category", "") or ""),
            "poly_resolution_date": p_date.isoformat() if p_date else None,
            "poly_resolution_source": pm.get("resolutionSource"),
            "score": s,
            "shared_tokens": sorted(tokens(event_text(ev)) & pm_entry["tokens"]),
            "description": ev.get("title", "") or pm_entry["text"],
            "category": k_cat or pm_entry["category"],
            "resolution_date": (k_date or p_date).isoformat() if (k_date or p_date) else None,
            "phase": "low_conf",
        })
        used_kalshi_tickers.add(et)
        used_poly_slugs_final.add(slug)

    logger.info("Total candidates: %d", len(candidates))

    # ── Stats ─────────────────────────────────────────────────────────────────
    by_cat: dict[str, int] = Counter(c.get("category", "?") for c in candidates)
    logger.info("By category: %s", dict(by_cat.most_common()))

    buckets = {"0.8+": 0, "0.6-0.8": 0, "0.4-0.6": 0, "0.2-0.4": 0}
    for c in candidates:
        s2 = c["score"]
        if s2 >= 0.8:
            buckets["0.8+"] += 1
        elif s2 >= 0.6:
            buckets["0.6-0.8"] += 1
        elif s2 >= 0.4:
            buckets["0.4-0.6"] += 1
        else:
            buckets["0.2-0.4"] += 1
    logger.info("Scores: %s", buckets)

    # ── Write outputs ─────────────────────────────────────────────────────────
    out = OUTPUT_DIR / "market_candidates_v2.json"
    out.write_text(json.dumps(candidates, indent=2, default=str))
    logger.info("Wrote %d candidates to %s", len(candidates), out)

    # Summary
    print("\n=== TOP 50 CANDIDATES ===")
    for c in candidates[:50]:
        print(f"[{c['score']:.3f}] {c['kalshi_ticker']:45s} <-> {c['poly_slug']}")
        print(f"       K: {c.get('kalshi_title','')[:80]}")
        print(f"       P: {c.get('poly_question','')[:80]}")
        print()

    return candidates


if __name__ == "__main__":
    sys.path.insert(0, str(REPO_ROOT))
    candidates = asyncio.run(main())
