#!/usr/bin/env python3
"""
Bulk market discovery — fetches non-sports Kalshi markets (via events API) and
ALL Polymarket gamma markets, matches them by title similarity, and writes
confirmed-candidate mappings to data/bulk_mappings.json and data/bulk_mappings.csv.

Strategy: use Kalshi's events endpoint to enumerate only relevant categories
(Elections, Politics, Economics, Crypto, Financials, etc.) then fetch their
individual markets.  This skips the ~280K sports multi-leg markets that make
up 99% of the raw Kalshi market catalog.

Usage:
    cd /Users/rentamac/Documents/arbiter
    python scripts/discovery/bulk_discover.py [--min-score 0.55] [--output-dir data]
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import csv
import json
import logging
import os
import re
import sys
import time
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path
from typing import Any

import aiohttp
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from dotenv import load_dotenv

load_dotenv(Path(__file__).parents[2] / ".env")

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("bulk_discover")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2"
POLY_BASE   = "https://gamma-api.polymarket.com"

POLY_PAGE_SIZE = 500
REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=30)
RATE_SLEEP = 0.20

STOPWORDS = {
    "a", "an", "and", "are", "be", "for", "if", "in", "is", "of", "on", "or",
    "the", "to", "vs", "will", "win", "winner", "yes", "no", "that", "this",
    "at", "by", "do", "from", "has", "have", "it", "its", "new", "not", "was",
    "were", "with", "who", "what", "when", "where", "which",
}

# ---------------------------------------------------------------------------
# Kalshi RSA-PSS Auth
# ---------------------------------------------------------------------------
class KalshiAuth:
    def __init__(self) -> None:
        self.api_key_id = os.environ.get("KALSHI_API_KEY_ID", "")
        key_path = os.environ.get("KALSHI_PRIVATE_KEY_PATH", "")
        self._private_key = None
        if key_path:
            try:
                p = Path(key_path) if Path(key_path).is_absolute() else Path(__file__).parents[2] / key_path
                with open(p, "rb") as f:
                    self._private_key = serialization.load_pem_private_key(f.read(), password=None)
                log.info("Kalshi RSA key loaded (key_id=%s…)", self.api_key_id[:8])
            except Exception as e:
                log.warning("Could not load Kalshi private key: %s — running unauthenticated", e)

    @property
    def authenticated(self) -> bool:
        return bool(self._private_key and self.api_key_id)

    def headers(self, method: str, path: str) -> dict[str, str]:
        if not self.authenticated:
            return {}
        ts = int(time.time() * 1000)
        msg = f"{ts}{method}{path}".encode()
        sig = self._private_key.sign(  # type: ignore[union-attr]
            msg,
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
            hashes.SHA256(),
        )
        return {
            "KALSHI-ACCESS-KEY": self.api_key_id,
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode(),
            "KALSHI-ACCESS-TIMESTAMP": str(ts),
            "Content-Type": "application/json",
            "User-Agent": "arbiter-bulk-discovery/1.0",
        }


_kalshi_auth = KalshiAuth()


# ---------------------------------------------------------------------------
# Text helpers
# ---------------------------------------------------------------------------
_TEXT_NORM_RE = re.compile(r"[^a-z0-9]+")
_DATE_RE = re.compile(r"(20\d{2})[-_/](\d{2})[-_/](\d{2})")

# Morphological normalisation for political/economic terms so that
# "democrats"=="democratic"=="democrat", "republicans"=="republican", etc.
_STEM_MAP: dict[str, str] = {
    "democrats": "democrat",
    "democratic": "democrat",
    "republicans": "republican",
    "midterms": "midterm",
    "elections": "election",
    "nominees": "nominee",
    "nominations": "nomination",
    "candidates": "candidate",
    "senators": "senator",
    "representatives": "representative",
    "governors": "governor",
    "governorships": "governor",
    "rates": "rate",
    "cuts": "cut",
    "prices": "price",
    "bitcoins": "bitcoin",
    "ethereum": "eth",
    "controls": "control",
    "wins": "win",
    "leads": "lead",
    "loses": "lose",
    "losses": "loss",
    "gains": "gain",
}


def _normalize(text: str) -> str:
    return _TEXT_NORM_RE.sub(" ", text.lower()).strip()


def _stem(word: str) -> str:
    return _STEM_MAP.get(word, word)


def _tokens(text: str) -> set[str]:
    """Discriminative tokens for inverted-index lookup (stemmed, stopwords stripped, len≥3)."""
    return {
        _stem(tok) for tok in _normalize(text).split()
        if tok and (len(tok) >= 3 or tok.isdigit()) and tok not in STOPWORDS
    }


def _all_words(text: str) -> set[str]:
    """Full word bag with stemming applied. Filters 1-char tokens (e.g. 'U.S.' → 'u s' → skip)."""
    return {_stem(tok) for tok in _normalize(text).split() if tok and len(tok) >= 2}


def _score(k_all: set[str], p_all: set[str], k_tokens: set[str], p_tokens: set[str]) -> float:
    """Lexical Jaccard (stemmed full word bag) + token-Jaccard bonus.

    Takes precomputed all_words sets to avoid recomputing them in the inner loop.
    """
    union_lex = k_all | p_all
    lex = len(k_all & p_all) / len(union_lex) if union_lex else 0.0

    union_tok = k_tokens | p_tokens
    tok = len(k_tokens & p_tokens) / len(union_tok) if union_tok else 0.0

    blend = 0.70 * lex + 0.30 * tok
    return round(max(lex, blend), 4)


def _coerce_date(value: Any) -> date | None:
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
            return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def _date_str(d: date | None) -> str:
    return d.isoformat() if d else ""


# ---------------------------------------------------------------------------
# Kalshi fetcher — paginate markets directly, skip sports tickers client-side
# ---------------------------------------------------------------------------

# Sports multi-leg ticker prefixes to skip (cover >99% of the 280K sports markets)
_SPORTS_PREFIXES = ("KXMVECROSS", "KXMVESPORTS", "KXMVEMULTI")


def _is_sports_market(ticker: str) -> bool:
    t = ticker.upper()
    return any(t.startswith(p) for p in _SPORTS_PREFIXES)


async def fetch_kalshi_markets(session: aiohttp.ClientSession) -> list[dict]:
    """Paginate the Kalshi markets API and return non-sports markets only.

    The markets API returns ~282K markets; almost all of them are sports multi-leg
    bets starting with KXMVECROSS / KXMVESPORTS.  We skip those client-side and keep
    everything else — elections, politics, economics, crypto, etc.
    """
    markets: list[dict] = []
    cursor: str | None = None
    page = 0

    while True:
        params: dict[str, Any] = {"status": "open", "limit": 1000}
        if cursor:
            params["cursor"] = cursor
        hdrs = _kalshi_auth.headers("GET", "/trade-api/v2/markets")
        async with session.get(f"{KALSHI_BASE}/markets", params=params, headers=hdrs) as resp:
            if resp.status == 429:
                log.warning("Kalshi rate-limited on markets page %d, sleeping 10s …", page)
                await asyncio.sleep(10)
                continue
            resp.raise_for_status()
            data = await resp.json()

        batch = data.get("markets") or []
        kept = [m for m in batch if not _is_sports_market(str(m.get("ticker") or ""))]
        markets.extend(kept)
        page += 1

        if page % 25 == 0:
            log.info("  Kalshi markets page %d: +%d kept / %d raw (total kept: %d)",
                     page, len(kept), len(batch), len(markets))

        cursor = data.get("cursor") or ""
        if not cursor or len(batch) < 1000:
            break
        await asyncio.sleep(RATE_SLEEP)

    log.info("Kalshi: %d non-sports markets from %d pages", len(markets), page)
    return markets


# ---------------------------------------------------------------------------
# Polymarket fetcher
# ---------------------------------------------------------------------------
async def fetch_polymarket_markets(session: aiohttp.ClientSession) -> list[dict]:
    markets: list[dict] = []
    offset = 0
    while True:
        params: dict[str, Any] = {
            "closed": "false",
            "limit": POLY_PAGE_SIZE,
            "offset": offset,
            "active": "true",
        }
        async with session.get(f"{POLY_BASE}/markets", params=params) as resp:
            if resp.status == 429:
                log.warning("Polymarket rate-limited, sleeping 5s …")
                await asyncio.sleep(5)
                continue
            resp.raise_for_status()
            data = await resp.json()

        batch = data if isinstance(data, list) else data.get("data") or data.get("markets") or []
        markets.extend(batch)
        log.info("Polymarket offset %d → +%d markets (total %d)", offset, len(batch), len(markets))

        if len(batch) < POLY_PAGE_SIZE:
            break
        offset += POLY_PAGE_SIZE
        await asyncio.sleep(RATE_SLEEP)

    log.info("Polymarket: fetched %d total markets", len(markets))
    return markets


# ---------------------------------------------------------------------------
# Text extraction per platform
# ---------------------------------------------------------------------------
def _kalshi_text(m: dict) -> str:
    parts: list[str] = []
    for field in ("title", "subtitle", "yes_sub_title"):
        v = str(m.get(field) or "").strip()
        if v:
            parts.append(v)
    if not parts:
        parts.append(str(m.get("ticker") or ""))
    return " ".join(dict.fromkeys(parts))


def _poly_text(m: dict) -> str:
    parts: list[str] = []
    for field in ("question", "title", "description"):
        v = str(m.get(field) or "").strip()
        if v:
            parts.append(v)
    if not parts:
        parts.append(str(m.get("slug") or ""))
    # limit description to first 200 chars to avoid score dilution
    result = " ".join(dict.fromkeys(parts))
    return result[:300]


# ---------------------------------------------------------------------------
# Category helpers
# ---------------------------------------------------------------------------
_CAT_CANONICAL = {
    "elections": "politics", "election": "politics",
    "world": "geopolitics", "international": "geopolitics",
    "sport": "sports",
    "econ": "economics", "macro": "economics",
    "fin": "finance", "financial": "finance",
    "financials": "finance",
    "crypto": "crypto",
    "companies": "tech",
    "science": "tech",
    "technology": "tech",
    "climate": "weather",
}
_CAT_LABELS = {"politics", "sports", "economics", "finance", "crypto",
               "geopolitics", "tech", "weather", "culture"}


def _normalize_category(raw: Any) -> str:
    text = _normalize(str(raw or ""))
    if not text:
        return ""
    for tok in text.split():
        c = _CAT_CANONICAL.get(tok, tok)
        if c in _CAT_LABELS:
            return c
    first = text.split()[0]
    return _CAT_CANONICAL.get(first, first)


# ---------------------------------------------------------------------------
# Matcher
# ---------------------------------------------------------------------------

def match_markets(
    kalshi_markets: list[dict],
    poly_markets: list[dict],
    min_score: float = 0.55,
) -> list[dict]:
    log.info("Building Polymarket inverted index …")
    poly_entries: list[dict] = []
    poly_index: dict[str, list[int]] = defaultdict(list)

    for pm in poly_markets:
        text = _poly_text(pm)
        if not text:
            continue
        toks = _tokens(text)
        if not toks:
            continue
        cat = _normalize_category(pm.get("category") or pm.get("groupItemTitle") or "")
        d = _coerce_date(pm.get("closeTime") or pm.get("endDate"))
        entry = {
            "market": pm,
            "text": text,
            "tokens": toks,
            "all_words": _all_words(text),  # precomputed for scoring
            "category": cat,
            "date": d,
            "condition_id": str(pm.get("conditionId") or pm.get("id") or pm.get("slug") or ""),
            "slug": str(pm.get("slug") or ""),
            "question": str(pm.get("question") or pm.get("title") or ""),
        }
        idx = len(poly_entries)
        poly_entries.append(entry)
        for tok in toks:
            poly_index[tok].append(idx)

    # Lookup freq limit: skip very common tokens in the candidate-finding pass.
    # They generate huge hit_count dicts without helping discriminate.
    # Scoring still uses full _all_words (not the index) so high-freq tokens still
    # contribute to the final similarity score.
    _max_lookup_freq = max(1, len(poly_entries) // 10)  # 10% of markets
    log.info("Poly index: %d unique tokens (lookup skip >%d entries) across %d entries",
             len(poly_index), _max_lookup_freq, len(poly_entries))
    log.info("Matching %d Kalshi markets vs %d Polymarket markets …",
             len(kalshi_markets), len(poly_entries))

    candidates: list[dict] = []
    scored = 0
    skipped_multileg = 0
    progress_every = 500

    for i, km in enumerate(kalshi_markets, start=1):
        if i % progress_every == 0:
            log.info("  … %d / %d Kalshi markets (%d candidates so far)",
                     i, len(kalshi_markets), len(candidates))

        k_text = _kalshi_text(km)
        if not k_text:
            continue
        k_toks = _tokens(k_text)
        if not k_toks:
            continue
        k_all = _all_words(k_text)

        ticker = str(km.get("ticker") or "").upper()
        if any(ticker.startswith(p) for p in ("KXMVECROSS", "KXMVESPORTS")):
            skipped_multileg += 1
            continue

        k_cat = _normalize_category(km.get("category") or "")
        k_date = _coerce_date(km.get("close_time") or km.get("expiration_time"))

        # Use inverted index — skip tokens that appear in >10% of Polymarket markets
        # (they create huge candidate lists without discriminating power)
        min_shared = 1 if len(k_toks) < 4 else 2
        hit_count: dict[int, int] = {}
        for tok in k_toks:
            idxs = poly_index.get(tok, ())
            if len(idxs) <= _max_lookup_freq:
                for idx in idxs:
                    hit_count[idx] = hit_count.get(idx, 0) + 1

        top_candidates = [idx for idx, cnt in hit_count.items() if cnt >= min_shared]
        if not top_candidates:
            continue

        # Sort by hit count, cap at 400
        if len(top_candidates) > 400:
            top_candidates.sort(key=lambda idx: -hit_count[idx])
            top_candidates = top_candidates[:400]

        best_score = 0.0
        best_entry: dict | None = None

        for idx in top_candidates:
            pe = poly_entries[idx]
            # Category gate
            if k_cat and pe["category"] and k_cat != pe["category"]:
                continue
            # Date gate: >60-day gap
            if k_date and pe["date"]:
                if abs((k_date - pe["date"]).days) > 60:
                    continue
            sc = _score(k_all, pe["all_words"], k_toks, pe["tokens"])
            scored += 1
            if sc > best_score:
                best_score = sc
                best_entry = pe

        if best_entry is None or best_score < min_score:
            continue

        pm = best_entry["market"]
        shared = sorted(k_toks & best_entry["tokens"])
        poly_cid = best_entry["condition_id"]
        slug = best_entry["slug"]

        candidates.append({
            "kalshi_ticker": str(km.get("ticker") or ""),
            "kalshi_title": str(km.get("title") or k_text),
            "polymarket_condition_id": poly_cid,
            "polymarket_slug": slug,
            "polymarket_question": best_entry["question"],
            "similarity_score": best_score,
            "category": k_cat or best_entry["category"],
            "kalshi_category": k_cat,
            "polymarket_category": best_entry["category"],
            "expiry_date": _date_str(k_date or best_entry["date"]),
            "kalshi_expiry": _date_str(k_date),
            "polymarket_expiry": _date_str(best_entry["date"]),
            "shared_tokens": shared,
            "status": "confirmed",
            "allow_auto_trade": False,
            "resolution_match_status": "pending_operator_review",
        })

    # Deduplicate: best score per kalshi ticker and per poly id
    candidates.sort(key=lambda c: c["similarity_score"], reverse=True)
    seen_k: set[str] = set()
    seen_p: set[str] = set()
    deduped: list[dict] = []
    for c in candidates:
        k = c["kalshi_ticker"]
        p = c["polymarket_condition_id"] or c["polymarket_slug"]
        if k in seen_k or p in seen_p:
            continue
        deduped.append(c)
        seen_k.add(k)
        seen_p.add(p)

    log.info(
        "Done — %d scored, %d multi-leg skipped, %d matches (≥%.2f) after dedup",
        scored, skipped_multileg, len(deduped), min_score,
    )
    return deduped


# ---------------------------------------------------------------------------
# Output writers
# ---------------------------------------------------------------------------
def _write_json(path: Path, matches: list[dict], meta: dict) -> None:
    payload = {"meta": meta, "matches": matches}
    path.write_text(json.dumps(payload, indent=2, default=str))
    log.info("JSON → %s  (%d matches)", path, len(matches))


def _write_csv(path: Path, matches: list[dict]) -> None:
    if not matches:
        path.write_text("")
        return
    fields = [
        "kalshi_ticker", "kalshi_title",
        "polymarket_condition_id", "polymarket_slug", "polymarket_question",
        "similarity_score", "category", "kalshi_category", "polymarket_category",
        "expiry_date", "kalshi_expiry", "polymarket_expiry",
        "status", "allow_auto_trade", "resolution_match_status",
    ]
    with path.open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(matches)
    log.info("CSV  → %s  (%d rows)", path, len(matches))


# ---------------------------------------------------------------------------
# MARKET_SEEDS file
# ---------------------------------------------------------------------------
def _build_seed_entry(c: dict) -> str:
    cid = c["kalshi_ticker"]
    canonical = re.sub(r"[^A-Z0-9]+", "_", cid.upper()).strip("_")
    description = (c["kalshi_title"] or c["polymarket_question"] or canonical).replace('"', '\\"').replace("\n", " ")[:120]
    category = c.get("category") or "general"
    expiry = c.get("expiry_date") or ""
    poly_id = c["polymarket_condition_id"] or c["polymarket_slug"]
    poly_q = c["polymarket_question"].replace('"', '\\"').replace("\n", " ")[:120]
    score = c["similarity_score"]
    return (
        f'    MarketMappingRecord(\n'
        f'        canonical_id="{canonical}",\n'
        f'        description="{description}",\n'
        f'        tags=("{category}",),\n'
        f'        kalshi="{cid}",\n'
        f'        polymarket="{poly_id}",\n'
        f'        polymarket_question="{poly_q}",\n'
        f'        status="confirmed",\n'
        f'        allow_auto_trade=False,\n'
        f'        resolution_match_status="pending_operator_review",\n'
        f'        notes="Bulk-discovered {expiry}; similarity={score:.3f}. Pending manual review before live trading.",\n'
        f'    ),\n'
    )


def write_seed_file(matches: list[dict], output_dir: Path) -> Path:
    entries = "".join(_build_seed_entry(c) for c in matches)
    content = (
        '"""\n'
        'Auto-generated by scripts/discovery/bulk_discover.py\n'
        'DO NOT edit manually — re-run the discovery script to regenerate.\n\n'
        'To load into the live system:\n'
        '    from data.discovered_seeds import DISCOVERED_SEEDS\n'
        '    MARKET_SEEDS = MARKET_SEEDS + DISCOVERED_SEEDS\n'
        '"""\n'
        'from __future__ import annotations\n'
        'from arbiter.config.settings import MarketMappingRecord\n\n'
        'DISCOVERED_SEEDS = (\n'
        f'{entries}'
        ')\n'
    )
    path = output_dir / "discovered_seeds.py"
    path.write_text(content)
    log.info("Seeds → %s  (%d entries)", path, len(matches))
    return path


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
async def run(min_score: float, output_dir: Path) -> int:
    output_dir.mkdir(parents=True, exist_ok=True)

    connector = aiohttp.TCPConnector(limit=20)
    async with aiohttp.ClientSession(
        timeout=REQUEST_TIMEOUT,
        connector=connector,
        headers={"User-Agent": "arbiter-bulk-discovery/1.0"},
    ) as session:
        log.info("Fetching Kalshi markets (paginate all, skip sports) …")
        kalshi_markets = await fetch_kalshi_markets(session)

        log.info("Fetching Polymarket markets …")
        poly_markets = await fetch_polymarket_markets(session)

    t0 = time.monotonic()
    matches = match_markets(kalshi_markets, poly_markets, min_score=min_score)
    log.info("Matching completed in %.2fs", time.monotonic() - t0)

    meta = {
        "generated_at": datetime.now().isoformat() + "Z",
        "kalshi_markets_fetched": len(kalshi_markets),
        "polymarket_markets_fetched": len(poly_markets),
        "matches_found": len(matches),
        "min_score_threshold": min_score,
    }

    _write_json(output_dir / "bulk_mappings.json", matches, meta)
    _write_csv(output_dir / "bulk_mappings.csv", matches)
    write_seed_file(matches, output_dir)

    by_cat: dict[str, int] = defaultdict(int)
    for m in list(matches):
        by_cat[m.get("category") or "unknown"] += 1
    log.info("Category breakdown: %s", dict(sorted(by_cat.items(), key=lambda x: -x[1])))

    high = sum(1 for m in matches if m["similarity_score"] >= 0.70)
    med  = sum(1 for m in matches if 0.55 <= m["similarity_score"] < 0.70)
    low  = sum(1 for m in matches if m["similarity_score"] < 0.55)
    log.info("Score bands — high(≥0.70): %d  med(0.55-0.70): %d  low(<0.55): %d", high, med, low)

    print(f"\n{'='*60}")
    print(f"  DISCOVERY COMPLETE")
    print(f"{'='*60}")
    print(f"  Kalshi markets:      {len(kalshi_markets):>6}")
    print(f"  Polymarket markets:  {len(poly_markets):>6}")
    print(f"  Matches (≥{min_score:.2f}):   {len(matches):>6}")
    print(f"  High-confidence:     {high:>6}  (score ≥ 0.70)")
    print(f"  Medium-confidence:   {med:>6}  (0.55 – 0.69)")
    print(f"  Low-confidence:      {low:>6}  (0.40 – 0.54)")
    print(f"{'='*60}")
    print(f"  Output dir:  {output_dir.resolve()}")
    print(f"{'='*60}\n")

    if matches:
        print("Top 30 matches:")
        print(f"{'Score':>6}  {'Category':<12}  {'Expiry':<12}  {'Kalshi ticker':<35}  Polymarket question")
        print("-" * 115)
        for m in matches[:30]:
            print(
                f"{m['similarity_score']:>6.3f}  "
                f"{(m.get('category') or ''):.<12}  "
                f"{m['expiry_date']:<12}  "
                f"{m['kalshi_ticker']:<35}  "
                f"{m['polymarket_question'][:50]}"
            )

    return len(matches)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--min-score", type=float, default=0.40,
                        help="Minimum similarity score (default: 0.40)")
    parser.add_argument("--output-dir", type=Path, default=Path("data"),
                        help="Directory for output files (default: data/)")
    args = parser.parse_args()

    try:
        n = asyncio.run(run(args.min_score, args.output_dir))
        sys.exit(0 if n >= 0 else 1)
    except KeyboardInterrupt:
        print("\nInterrupted.")
        sys.exit(1)


if __name__ == "__main__":
    main()
