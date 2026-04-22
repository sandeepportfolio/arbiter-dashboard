"""
Convert discovered_markets.json → arbiter/config/market_seeds_ext.py

Usage:
    python scripts/generate_seeds.py /tmp/discovered_markets.json
    # Writes: arbiter/config/market_seeds_ext.py
"""
from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _q(s: str) -> str:
    return "'" + str(s).replace("\\", "\\\\").replace("'", "\\'") + "'"


def _tuple_str(items: list[str]) -> str:
    if not items:
        return "()"
    parts = ", ".join(_q(s) for s in items)
    return f"({parts},)" if len(items) == 1 else f"({parts})"


def _derive_aliases(m: dict) -> list[str]:
    aliases: list[str] = []
    for text in [m.get("kalshi_title",""), m.get("polymarket_question",""), m.get("description","")]:
        if text:
            clean = re.sub(r"[^\w\s]", " ", text.lower())
            clean = re.sub(r"\s+", " ", clean).strip()[:120]
            if clean and clean not in aliases and len(clean) > 5:
                aliases.append(clean)
    seen: set[str] = set()
    out: list[str] = []
    for a in aliases:
        if a not in seen:
            seen.add(a)
            out.append(a)
    return out[:3]


def _derive_tags(m: dict) -> list[str]:
    cat = m.get("category", "")
    tags = list(m.get("tags", []) or [])
    if cat and cat not in tags:
        tags.insert(0, cat)

    text = (
        (m.get("kalshi_title","") or "") + " " +
        (m.get("polymarket_question","") or "")
    ).lower()

    tag_map = {
        "bitcoin": ["bitcoin", "btc"],
        "ethereum": ["ethereum", "eth"],
        "solana": ["solana", "sol"],
        "ripple": ["ripple", "xrp"],
        "sp500": ["s&p", "spy", "spx", "sp 500"],
        "nasdaq": ["nasdaq", "qqq", "ndx"],
        "federal_reserve": ["federal reserve", "fed rate", "fomc"],
        "inflation": ["cpi", "inflation", "pce"],
        "gold": ["gold"],
        "oil": ["crude oil", "wti", "oil"],
        "recession": ["recession"],
        "earnings": ["earnings", "eps"],
    }
    for tag, keywords in tag_map.items():
        if tag not in tags and any(kw in text for kw in keywords):
            tags.append(tag)

    return list(dict.fromkeys(tags))[:6]


def _resolution_source(m: dict) -> str:
    text = (
        (m.get("kalshi_title","") or "") + " " +
        (m.get("polymarket_question","") or "")
    ).lower()

    if any(k in text for k in ["btc","bitcoin","eth","ethereum","sol","xrp","crypto"]):
        return "Coinmarketcap / major exchange OHLCV"
    if any(k in text for k in ["s&p","spy","spx","sp500"]):
        return "S&P Dow Jones Indices"
    if any(k in text for k in ["nasdaq","qqq","ndx"]):
        return "Nasdaq"
    if any(k in text for k in ["dow","djia"]):
        return "S&P Dow Jones Indices"
    if any(k in text for k in ["cpi","inflation"]):
        return "Bureau of Labor Statistics"
    if any(k in text for k in ["gdp","pce"]):
        return "Bureau of Economic Analysis"
    if any(k in text for k in ["fed","fomc","interest rate"]):
        return "Federal Reserve"
    if any(k in text for k in ["jobs","nonfarm","unemployment"]):
        return "Bureau of Labor Statistics"
    if any(k in text for k in ["gold"]):
        return "LBMA / CME"
    if any(k in text for k in ["oil","wti","crude"]):
        return "EIA / CME"
    return "Market operator / exchange data"


def record_to_python(m: dict) -> str:
    canonical_id = m.get("canonical_id", "UNKNOWN")
    description  = m.get("description", "") or m.get("kalshi_title", "")
    kalshi       = m.get("kalshi_ticker", "") or m.get("kalshi_event_ticker", "")
    polymarket   = m.get("polymarket_slug", "")
    poly_q       = m.get("polymarket_question", "")
    score        = m.get("score", 0.0)
    k_expiry     = m.get("kalshi_expiry", "") or ""
    p_expiry     = m.get("polymarket_expiry", "") or ""

    aliases = _derive_aliases(m)
    tags    = _derive_tags(m)
    source  = _resolution_source(m)

    criteria = {
        "kalshi":     {"source": source, "rule": m.get("kalshi_title","")[:200], "settlement_date": k_expiry},
        "polymarket": {"source": source, "rule": poly_q[:200], "settlement_date": p_expiry},
        "criteria_match": "similar",
        "operator_note": (
            f"Auto-discovered {datetime.now(tz=timezone.utc).strftime('%Y-%m-%d')}, "
            f"score={score:.3f}. Needs operator review before live trading."
        ),
    }

    note_parts = [
        f"Auto-discovered score={score:.3f}.",
        f"Kalshi expiry: {k_expiry}." if k_expiry else "",
        f"Polymarket expiry: {p_expiry}." if p_expiry else "",
        "Status: candidate — operator review required before live trading.",
    ]
    notes = " ".join(p for p in note_parts if p)

    lines = [
        f"    MarketMappingRecord(",
        f"        canonical_id={_q(canonical_id)},",
        f"        description={_q(description[:200])},",
        f"        status='candidate',",
        f"        allow_auto_trade=False,",
        f"        aliases={_tuple_str(aliases)},",
        f"        tags={_tuple_str(tags)},",
        f"        kalshi={_q(kalshi)},",
        f"        polymarket={_q(polymarket)},",
        f"        polymarket_question={_q(poly_q[:500])},",
        f"        notes={_q(notes)},",
        f"        resolution_criteria={repr(criteria)},",
        f"        resolution_match_status='similar',",
        f"    ),",
    ]
    return "\n".join(lines)


def main():
    input_file = sys.argv[1] if len(sys.argv) > 1 else "/tmp/discovered_markets.json"
    output_file = sys.argv[2] if len(sys.argv) > 2 else str(
        Path(__file__).parent.parent / "arbiter" / "config" / "market_seeds_ext.py"
    )

    with open(input_file) as f:
        data = json.load(f)

    matches = data.get("matches", [])
    generated_at = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
    k_count = data.get("kalshi_relevant_count", "?")
    p_count = data.get("poly_relevant_count", "?")
    m_count = len(matches)

    # Group by category
    by_cat: dict[str, list[dict]] = {}
    for m in matches:
        cat = m.get("category", "other")
        by_cat.setdefault(cat, []).append(m)

    lines = [
        '"""',
        f'Extended market seed records — crypto/finance discovery.',
        f'Generated: {generated_at}',
        f'Source: {input_file}',
        f'Kalshi relevant markets scanned: {k_count}',
        f'Polymarket relevant markets scanned: {p_count}',
        f'Total matches: {m_count}',
        '',
    ]
    for cat, items in sorted(by_cat.items()):
        lines.append(f'  {cat}: {len(items)} matches')
    lines += [
        '',
        'All records have status="candidate" and allow_auto_trade=False.',
        'Operator must review each record before enabling live trading.',
        '"""',
        'from __future__ import annotations',
        '',
        'from typing import Tuple',
        '',
        'from arbiter.config.settings import MarketMappingRecord',
        '',
        '',
        f'# {m_count} auto-discovered crypto/finance market mappings',
        f'CRYPTO_FINANCE_SEEDS: Tuple[MarketMappingRecord, ...] = (',
    ]

    for cat in ["crypto", "finance", "economics", "other"]:
        items = by_cat.get(cat, [])
        if not items:
            continue
        lines.append(f"    # ── {cat.upper()} ({len(items)} markets) ──────────────────────────────────────")
        for m in items:
            lines.append(record_to_python(m))
            lines.append("")

    lines.append(")")
    lines.append("")

    content = "\n".join(lines)

    Path(output_file).write_text(content, encoding="utf-8")
    print(f"Wrote {m_count} records to {output_file}")

    # Print summary table
    print(f"\n{'Category':<15} {'Count':>6}")
    print("-" * 23)
    for cat, items in sorted(by_cat.items()):
        print(f"{cat:<15} {len(items):>6}")
    print(f"{'TOTAL':<15} {m_count:>6}")


if __name__ == "__main__":
    main()
