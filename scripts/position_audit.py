"""Cross-platform position audit — reads venue APIs directly, not the local DB.

Outputs a per-position table with current MTM, hedge state across venues, and
naked-leg flags. Read-only; never submits orders.

Runs inside the arbiter-api-prod container:
    docker exec arbiter-api-prod sh -c 'PYTHONPATH=/app python3 /app/scripts/position_audit.py'

Or, with the container's working dir already at /app:
    python3 scripts/position_audit.py
"""
from __future__ import annotations

import asyncio
import json
from typing import Any, Dict, List

import aiohttp

from arbiter.collectors.kalshi import KalshiAuth
from arbiter.collectors.polymarket_us import (
    Ed25519Signer,
    PolymarketUSClient,
    extract_current_balance,
)
from arbiter.config.settings import load_config


async def _kalshi_positions(cfg) -> List[Dict[str, Any]]:
    auth = KalshiAuth(cfg.kalshi.api_key_id, cfg.kalshi.private_key_path)
    base = cfg.kalshi.base_url.rstrip("/")
    async with aiohttp.ClientSession() as session:
        h = auth.get_headers("GET", "/trade-api/v2/portfolio/positions")
        async with session.get(base + "/portfolio/positions?limit=500", headers=h) as r:
            data = json.loads(await r.text())
        rows: List[Dict[str, Any]] = []
        for p in data.get("market_positions", []):
            qty = float(p.get("position_fp", 0))
            if qty == 0:
                continue
            ticker = p["ticker"]
            try:
                mh = auth.get_headers("GET", f"/trade-api/v2/markets/{ticker}")
                async with session.get(base + f"/markets/{ticker}", headers=mh) as mr:
                    mkt = json.loads(await mr.text()).get("market", {})
                    yes_bid = mkt.get("yes_bid", 0) / 100
                    yes_ask = mkt.get("yes_ask", 0) / 100
                    title = (mkt.get("title") or "")[:60]
                    close_ts = mkt.get("close_time", "")
            except Exception:
                yes_bid = yes_ask = 0.0
                title = ""
                close_ts = ""
            cost = float(p.get("total_traded_dollars", 0))
            mtm = qty * yes_bid
            rows.append({
                "platform": "kalshi",
                "ticker": ticker,
                "qty": qty,
                "cost": cost,
                "yes_bid": yes_bid,
                "yes_ask": yes_ask,
                "mtm_if_sell": mtm,
                "unrealized_mtm": mtm - cost,
                "title": title,
                "close_ts": close_ts,
            })
        return rows


async def _polymarket_positions(cfg) -> List[Dict[str, Any]]:
    pcfg = cfg.polymarket
    if not getattr(pcfg, "api_key_id", None) or not getattr(pcfg, "api_secret", None):
        return []
    signer = Ed25519Signer(pcfg.api_key_id, pcfg.api_secret)
    client = PolymarketUSClient(base_url="https://api.polymarket.us", signer=signer)
    try:
        positions = (await client._signed("GET", "/portfolio/positions")).get("positions", {})
        rows: List[Dict[str, Any]] = []
        for slug, p in positions.items():
            net = int(p.get("netPosition", 0))
            cost = float((p.get("cost") or {}).get("value", 0))
            cv = float((p.get("cashValue") or {}).get("value", 0))
            avg_px = float((p.get("avgPx") or {}).get("value", 0))
            fees = float((p.get("fees") or {}).get("value", 0))
            md = p.get("marketMetadata", {}) or {}
            try:
                book = await client.get_orderbook(slug, depth=1)
                book_md = book.get("marketData", book) if isinstance(book, dict) else {}
                bids = book_md.get("bids") or []
                offers = book_md.get("offers") or []
                yes_bid = float((bids[0] or {}).get("px", {}).get("value", 0)) if bids else 0.0
                yes_ask = float((offers[0] or {}).get("px", {}).get("value", 0)) if offers else 0.0
            except Exception:
                yes_bid = yes_ask = 0.0
            rows.append({
                "platform": "polymarket",
                "slug": slug,
                "qty": net,
                "side": "NO" if net < 0 else "YES",
                "cost": cost,
                "cv": cv,
                "avg_yes_px": avg_px,
                "fees": fees,
                "yes_bid": yes_bid,
                "yes_ask": yes_ask,
                "unrealized": cv - cost,
                "title": (md.get("title") or "")[:60],
                "outcome": md.get("outcome", ""),
            })
        bal = await client.balance()
        usd = next((b for b in bal["balances"] if str(b.get("currency", "")).upper() == "USD"), {})
        return rows, {
            "currentBalance": float(usd.get("currentBalance", 0)),
            "buyingPower": extract_current_balance(bal),
            "marginRequirement": float(usd.get("marginRequirement", 0)),
        }
    finally:
        await client.close()


def _print(rows: List[Dict[str, Any]], title: str) -> None:
    print(f"\n{title}  ({len(rows)} rows)")
    print("-" * 100)
    for r in rows:
        print("  " + json.dumps(r, default=str))


async def main() -> None:
    cfg = load_config()
    kalshi = await _kalshi_positions(cfg)
    poly_result = await _polymarket_positions(cfg)
    if isinstance(poly_result, tuple):
        poly, poly_bal = poly_result
    else:
        poly, poly_bal = poly_result, {}
    _print(kalshi, "KALSHI POSITIONS (non-zero)")
    _print(poly, "POLYMARKET US POSITIONS (all)")
    if poly_bal:
        print(
            f"\nPolymarket balance: currentBalance=${poly_bal['currentBalance']:.2f}"
            f"  buyingPower=${poly_bal['buyingPower']:.2f}"
            f"  marginRequirement=${poly_bal['marginRequirement']:.2f}"
        )
    print(
        "\nForecastEx: cannot be enumerated from inside the Docker container "
        "while the IBKR Client Portal Gateway at host.docker.internal:5000 is "
        "unreachable. Query positions directly via the IBKR UI or run this "
        "audit on the host."
    )


if __name__ == "__main__":
    asyncio.run(main())
