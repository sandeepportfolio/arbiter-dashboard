"""
Market discovery script — fetches Kalshi + Polymarket crypto/finance markets,
matches them using an inverted-index approach (fast), and outputs JSON.

Usage:
    cd /path/to/arbiter
    python scripts/discover_markets.py 2>>/tmp/disc.log | tee /tmp/discovered_markets.json
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import aiohttp
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s", handlers=[logging.StreamHandler()])
logger = logging.getLogger("discover")

KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2"
POLY_GAMMA  = "https://gamma-api.polymarket.com"

KALSHI_SERIES = [
    # Crypto
    "KXBTC", "KXBTCD", "KXBTCR", "KXETH", "KXETHD", "KXSOL", "KXXRP", "KXDOGE",
    "KXBTCETF", "KXETHETF",
    # Indices
    "KXSPY", "KXNQ", "KXDJ", "KXINX", "KXNDX",
    # Commodities
    "KXGOLD", "KXOIL", "KXWTI",
    # Macro
    "KXFED", "KXCPI", "KXPCE", "KXJOBS", "KXGDP", "RECESSION",
    # Companies
    "KXTSLA", "KXAAPL", "KXNVDA", "KXMSFT", "KXGOOG",
]

POLY_TAGS = [
    "crypto", "bitcoin", "ethereum", "finance", "economics", "stocks",
    "etf", "interest-rates", "commodities", "recession",
]

CRYPTO_KW  = {"btc","bitcoin","eth","ethereum","sol","solana","xrp","ripple","doge",
               "dogecoin","crypto","defi","blockchain","etf","sec","coinbase","binance"}
FINANCE_KW = {"s&p","spy","spx","sp500","nasdaq","qqq","ndx","dow","djia","russell",
               "fed","fomc","cpi","inflation","gdp","recession","unemployment","treasury",
               "yield","gold","xau","oil","wti","crude","tesla","apple","nvidia","microsoft",
               "google","amazon","earnings","ipo","merger","rate","interest"}
ALL_KW = CRYPTO_KW | FINANCE_KW


# ── Auth ─────────────────────────────────────────────────────────────────────

def _load_key(path: str):
    for candidate in [Path(path), Path(__file__).parent.parent / path]:
        if candidate.exists():
            with open(candidate, "rb") as f:
                return serialization.load_pem_private_key(f.read(), password=None)
    return None


def _kalshi_headers(api_key_id: str, private_key, method: str, path: str) -> dict:
    ts = int(time.time() * 1000)
    sig = ""
    if private_key and api_key_id:
        msg = f"{ts}{method}{path}".encode()
        raw = private_key.sign(
            msg,
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
            hashes.SHA256(),
        )
        sig = base64.b64encode(raw).decode()
    return {
        "KALSHI-ACCESS-KEY": api_key_id or "",
        "KALSHI-ACCESS-SIGNATURE": sig,
        "KALSHI-ACCESS-TIMESTAMP": str(ts),
        "Accept": "application/json",
    }


# ── Kalshi fetch ──────────────────────────────────────────────────────────────

async def _kalshi_page(session, api_key_id, private_key, params: dict) -> dict:
    path = "/trade-api/v2/markets"
    url = f"{KALSHI_BASE}/markets"
    headers = _kalshi_headers(api_key_id, private_key, "GET", path)
    for attempt in range(3):
        try:
            async with session.get(url, params=params, headers=headers,
                                   timeout=aiohttp.ClientTimeout(total=20)) as resp:
                if resp.status == 429:
                    wait = float(resp.headers.get("Retry-After", "5") or "5")
                    logger.warning("Kalshi 429, sleeping %.0fs", wait)
                    await asyncio.sleep(wait)
                    continue
                resp.raise_for_status()
                return await resp.json()
        except Exception as e:
            if attempt == 2:
                logger.error("Kalshi page failed: %s", e)
                return {}
            await asyncio.sleep(1 + attempt)
    return {}


async def fetch_kalshi_markets(session, api_key_id: str, private_key) -> list[dict]:
    seen: set[str] = set()
    all_markets: list[dict] = []

    async def _collect(extra: dict):
        cursor = None
        for _ in range(100):
            params = {"limit": "1000", "status": "open", **extra}
            if cursor:
                params["cursor"] = cursor
            data = await _kalshi_page(session, api_key_id, private_key, params)
            markets = data.get("markets") or []
            added = 0
            for m in markets:
                t = m.get("ticker", "")
                if t and t not in seen:
                    seen.add(t)
                    all_markets.append(m)
                    added += 1
            cursor = data.get("cursor") or None
            if not cursor or not markets:
                break
            await asyncio.sleep(0.12)
        return added

    # By category
    for cat in ["crypto", "finance", "economics"]:
        n = await _collect({"category": cat})
        logger.info("Kalshi category=%s: +%d (total %d)", cat, n, len(all_markets))

    # By series ticker
    for series in KALSHI_SERIES:
        n = await _collect({"series_ticker": series})
        if n:
            logger.info("Kalshi series=%s: +%d (total %d)", series, n, len(all_markets))
        await asyncio.sleep(0.12)

    return all_markets


# ── Polymarket fetch ──────────────────────────────────────────────────────────

async def fetch_polymarket_markets(session) -> list[dict]:
    seen: set[str] = set()
    all_markets: list[dict] = []

    for tag in POLY_TAGS:
        offset, limit = 0, 100
        zero_streak = 0
        for page in range(200):
            params = {"limit": limit, "offset": offset, "active": "true",
                      "closed": "false", "tag": tag}
            try:
                async with session.get(f"{POLY_GAMMA}/markets", params=params,
                                       timeout=aiohttp.ClientTimeout(total=20)) as resp:
                    if resp.status in {404, 422, 400}:
                        break
                    resp.raise_for_status()
                    markets = await resp.json()
            except Exception as e:
                logger.warning("Polymarket tag=%s page=%d: %s", tag, page, e)
                break

            if not markets:
                break

            new = 0
            for m in markets:
                mid = m.get("id") or m.get("conditionId") or m.get("slug", "")
                if mid and mid not in seen:
                    seen.add(str(mid))
                    all_markets.append(m)
                    new += 1

            if new > 0:
                logger.info("Polymarket tag=%s page=%d: +%d (total %d)", tag, page, new, len(all_markets))
                zero_streak = 0
            else:
                zero_streak += 1

            # Stop pagination if no new markets for 3 consecutive pages
            if len(markets) < limit or zero_streak >= 3:
                break
            offset += limit
            await asyncio.sleep(0.3)

    return all_markets


# ── Normalisation + tokenisation ─────────────────────────────────────────────

_STOP = {
    "a","an","and","are","as","at","be","by","for","from","has","have","if","in","is",
    "it","its","of","on","or","that","the","this","to","was","were","will","with",
    "yes","no","above","below","end","close","open","does","get","what",
}
_ALIASES = {
    "btc":"bitcoin","eth":"ethereum","sol":"solana","xrp":"ripple","doge":"dogecoin",
    "sp500":"sp500","s&p500":"sp500","s&p":"sp500","spx":"sp500","spy":"sp500",
    "nasdaq":"nasdaq100","qqq":"nasdaq100","ndx":"nasdaq100",
    "djia":"dowjones","dow":"dowjones",
    "tsla":"tesla","aapl":"apple","nvda":"nvidia","msft":"microsoft",
    "goog":"google","googl":"google","amzn":"amazon","fb":"meta",
    "fed":"federal_reserve","fomc":"federal_reserve",
    "xau":"gold","wti":"crude_oil",
}


def _tokenize(text: str) -> frozenset[str]:
    t = re.sub(r"[^\w\s&.$%]", " ", text.lower())
    t = re.sub(r"\s+", " ", t).strip()
    tokens: set[str] = set()
    for raw in t.split():
        tok = _ALIASES.get(raw, raw)
        if tok not in _STOP and len(tok) >= 2:
            tokens.add(tok)
    return frozenset(tokens)


def _price_tokens(text: str) -> frozenset[str]:
    found: set[str] = set()
    for m in re.finditer(r"\$?\s*(\d[\d,]*\.?\d*)\s*([kKmM]?)", text.lower()):
        num_str = m.group(1).replace(",", "")
        suffix = m.group(2).lower()
        try:
            val = float(num_str)
            if suffix == "k":
                val *= 1000
            elif suffix == "m":
                val *= 1_000_000
            if val >= 1:
                found.add(str(int(val)))
        except ValueError:
            pass
    return frozenset(found)


def _year_tokens(text: str) -> frozenset[str]:
    return frozenset(re.findall(r"\b(202[3-9]|203\d)\b", text))


def is_relevant(text: str) -> bool:
    t = text.lower()
    return any(kw in t for kw in ALL_KW)


def similarity(
    k_tok: frozenset, k_price: frozenset, k_year: frozenset,
    p_tok: frozenset, p_price: frozenset, p_year: frozenset,
) -> float:
    # Price mismatch → 0
    if k_price and p_price and not (k_price & p_price):
        return 0.0
    # Year mismatch → 0.01
    if k_year and p_year and not (k_year & p_year):
        return 0.01

    all_tok = k_tok | p_tok | k_price | p_price
    if not all_tok:
        return 0.0

    overlap = (k_tok & p_tok) | (k_price & p_price)
    base = len(overlap) / len(all_tok)

    # Price-level match boost
    if k_price and p_price and (k_price & p_price):
        base = min(base * 1.6, 0.99)

    return round(base, 4)


# ── Safe date helper ──────────────────────────────────────────────────────────

def _safe_date(val) -> str:
    if not val:
        return ""
    try:
        if isinstance(val, (int, float)) and val > 1e9:
            return datetime.fromtimestamp(val, tz=timezone.utc).strftime("%Y-%m-%d")
        s = str(val)
        return datetime.fromisoformat(s.replace("Z", "+00:00")).strftime("%Y-%m-%d")
    except Exception:
        return str(val)[:10]


def _canonical_id(ticker: str, cat: str) -> str:
    t = ticker.upper()
    for pfx in ("KXBTC", "KXETH", "KXSOL", "KXSPY", "KXNQ", "KXDJ", "KXGOLD", "KX"):
        if t.startswith(pfx):
            t = t[len(pfx):]
            break
    clean = re.sub(r"[^A-Z0-9]", "_", t).strip("_")
    cid = f"{cat[:5].upper()}_{clean}"
    return cid[:60]


# ── Main ─────────────────────────────────────────────────────────────────────

@dataclass
class Match:
    canonical_id: str
    description: str
    kalshi_ticker: str
    kalshi_event_ticker: str
    kalshi_title: str
    kalshi_expiry: str
    polymarket_slug: str
    polymarket_question: str
    polymarket_expiry: str
    score: float
    category: str
    tags: list[str]


async def main():
    try:
        from dotenv import load_dotenv
        env = Path(__file__).parent.parent / ".env"
        if env.exists():
            load_dotenv(env, override=False)
    except ImportError:
        pass

    api_key_id = os.getenv("KALSHI_API_KEY_ID", "").strip()
    key_path   = os.getenv("KALSHI_PRIVATE_KEY_PATH", "./keys/kalshi_private.pem").strip()
    private_key = _load_key(key_path) if key_path else None
    logger.info("Kalshi key_id=%s key_loaded=%s", (api_key_id[:8] + "...") if api_key_id else "MISSING", private_key is not None)

    connector = aiohttp.TCPConnector(limit=20)
    async with aiohttp.ClientSession(connector=connector) as session:

        # ── 1. Fetch ──────────────────────────────────────────────────────────
        logger.info("=== Fetching Kalshi ===")
        kalshi_raw = await fetch_kalshi_markets(session, api_key_id, private_key)
        logger.info("Kalshi total unique: %d", len(kalshi_raw))

        logger.info("=== Fetching Polymarket ===")
        poly_raw = await fetch_polymarket_markets(session)
        logger.info("Polymarket total: %d", len(poly_raw))

    # ── 2. Filter relevant ────────────────────────────────────────────────────
    kalshi_relevant = [
        m for m in kalshi_raw
        if is_relevant(
            (m.get("title") or "") + " " +
            (m.get("event_ticker") or "") + " " +
            (m.get("series_ticker") or "") + " " +
            (m.get("category") or "")
        )
    ]
    logger.info("Kalshi relevant: %d", len(kalshi_relevant))

    poly_relevant = [
        m for m in poly_raw
        if is_relevant(
            (m.get("question") or "") + " " +
            (m.get("title") or "") + " " +
            " ".join(
                str(t.get("label", t) if isinstance(t, dict) else t)
                for t in (m.get("tags") or [])
            ) + " " +
            (m.get("description") or "")[:200]
        )
    ]
    logger.info("Polymarket relevant: %d", len(poly_relevant))

    # ── 3. Pre-tokenize Polymarket once ───────────────────────────────────────
    logger.info("Pre-tokenizing Polymarket…")

    @dataclass
    class PolyEntry:
        slug: str
        question: str
        expiry: str
        tok: frozenset
        price: frozenset
        year: frozenset

    poly_entries: list[PolyEntry] = []
    for m in poly_relevant:
        q = m.get("question") or m.get("title") or ""
        slug = m.get("slug") or m.get("id") or ""
        expiry = _safe_date(
            m.get("endDate") or m.get("endDateIso") or
            m.get("expirationDate") or m.get("resolveBy") or ""
        )
        poly_entries.append(PolyEntry(
            slug=str(slug),
            question=q,
            expiry=expiry,
            tok=_tokenize(q),
            price=_price_tokens(q),
            year=_year_tokens(q),
        ))

    # Build inverted index: token → list of PolyEntry indices
    inv_index: dict[str, list[int]] = {}
    for idx, pe in enumerate(poly_entries):
        for tok in pe.tok:
            inv_index.setdefault(tok, []).append(idx)
        for p in pe.price:
            inv_index.setdefault(p, []).append(idx)

    logger.info("Inverted index: %d tokens → %d poly entries", len(inv_index), len(poly_entries))

    # ── 4. Match ──────────────────────────────────────────────────────────────
    logger.info("=== Matching ===")
    matches: list[Match] = []

    for k in kalshi_relevant:
        k_title = k.get("title") or k.get("yes_sub_title") or ""
        k_ticker = k.get("ticker") or ""
        k_event = k.get("event_ticker") or ""
        k_series = k.get("series_ticker") or ""
        k_cat = (k.get("category") or "").lower()
        k_expiry = _safe_date(k.get("expiration_time") or k.get("close_time"))

        k_tok   = _tokenize(k_title)
        k_price = _price_tokens(k_title)
        k_year  = _year_tokens(k_title)

        # Find candidate poly indices via inverted index (only score matches sharing ≥1 token)
        candidate_idxs: set[int] = set()
        for tok in k_tok | k_price:
            candidate_idxs.update(inv_index.get(tok, []))

        if not candidate_idxs:
            continue

        best_score = 0.0
        best_pe: Optional[PolyEntry] = None

        for idx in candidate_idxs:
            pe = poly_entries[idx]
            sc = similarity(k_tok, k_price, k_year, pe.tok, pe.price, pe.year)
            if sc > best_score:
                best_score = sc
                best_pe = pe

        if best_pe is None or best_score < 0.25:
            continue

        # Determine category
        combined = (k_title + " " + k_event + " " + k_series).lower()
        if any(kw in combined for kw in ["btc","bitcoin","eth","ethereum","sol","xrp","crypto"]):
            cat = "crypto"
        elif any(kw in combined for kw in ["spy","nasdaq","s&p","dow","sp500","qqq","ndx","stock"]):
            cat = "finance"
        elif any(kw in combined for kw in ["fed","rate","cpi","inflation","gdp","recession","pce","jobs"]):
            cat = "economics"
        else:
            cat = k_cat or "finance"

        tags = list({cat})
        for kw, tag in [
            (["btc","bitcoin"], "bitcoin"), (["eth","ethereum"], "ethereum"),
            (["sol","solana"], "solana"), (["xrp","ripple"], "ripple"),
            (["s&p","spy","spx","sp500"], "sp500"),
            (["nasdaq","qqq","ndx"], "nasdaq"), (["gold","xau"], "gold"),
            (["oil","wti","crude"], "oil"),
            (["fed","fomc","interest rate"], "federal_reserve"),
            (["cpi","inflation"], "inflation"),
            (["tesla","tsla"], "tesla"), (["nvidia","nvda"], "nvidia"),
        ]:
            if any(k in combined for k in kw):
                tags.append(tag)

        canonical_id = _canonical_id(k_ticker or k_event, cat)

        matches.append(Match(
            canonical_id=canonical_id,
            description=k_title or best_pe.question,
            kalshi_ticker=k_ticker,
            kalshi_event_ticker=k_event,
            kalshi_title=k_title,
            kalshi_expiry=k_expiry,
            polymarket_slug=best_pe.slug,
            polymarket_question=best_pe.question,
            polymarket_expiry=best_pe.expiry,
            score=best_score,
            category=cat,
            tags=list(dict.fromkeys(tags)),
        ))

    # Sort by score desc
    matches.sort(key=lambda m: m.score, reverse=True)
    logger.info("Matches (score>=0.25): %d", len(matches))

    # Deduplicate (one Kalshi ticker → one match, one Poly slug → one match)
    seen_k: set[str] = set()
    seen_p: set[str] = set()
    deduped: list[Match] = []
    for m in matches:
        k_key = m.kalshi_ticker or m.kalshi_event_ticker
        if k_key in seen_k or (m.polymarket_slug and m.polymarket_slug in seen_p):
            continue
        seen_k.add(k_key)
        if m.polymarket_slug:
            seen_p.add(m.polymarket_slug)
        deduped.append(m)

    logger.info("Unique matches after dedup: %d", len(deduped))

    output = {
        "generated_at": datetime.now(tz=timezone.utc).isoformat(),
        "kalshi_relevant_count": len(kalshi_relevant),
        "poly_relevant_count": len(poly_relevant),
        "match_count": len(deduped),
        "matches": [asdict(m) for m in deduped],
    }
    print(json.dumps(output, indent=2, default=str))


if __name__ == "__main__":
    asyncio.run(main())
