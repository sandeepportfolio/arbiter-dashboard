#!/usr/bin/env python3
"""Populate forecastex_no_contract_id for all mappings that have a YES conid
but no NO conid.

Two-phase strategy:

Phase A — OPT child conids (right=CALL):
  These are actual tradeable YES contracts. The NO sibling is at an adjacent
  IBKR conid. Scan [yes-10, yes+10] via /iserver/contract/{conid}/info
  (cheap, generous rate limit) and match parent + strike + right=PUT.

Phase B — IND parent conids (no right field):
  These are event-level conids. Need resolve_event_children to discover ALL
  children (YES=Call, NO=Put). Only 18 unique parents across 280 mappings,
  so the secdef/strikes call count is manageable (~18 × ~3 months each).
  For each parent, if children are found, update ALL mappings that reference
  that parent with the appropriate child conids (both YES and NO).

Rate limiting:
  - 0.12s between contract_info calls (Phase A)
  - 2s between resolve_event_children calls (Phase B — these hit secdef/strikes)
  - 30s backoff on 429

Usage:
    python3 /app/scripts/populate_no_conids.py [--dry-run]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.abspath(os.path.dirname(os.path.dirname(__file__))))

import asyncpg

from arbiter.collectors.forecastex import ForecastExClient
from arbiter.config.settings import ForecastExConfig

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("populate_no_conids")


async def main() -> int:
    parser = argparse.ArgumentParser(description="Populate ForecastEx NO conids")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--scan-range", type=int, default=10,
                        help="Adjacent conid scan radius for OPT children (default: 10)")
    parser.add_argument("--call-delay", type=float, default=0.12,
                        help="Seconds between contract_info calls (default: 0.12)")
    parser.add_argument("--resolve-delay", type=float, default=3.0,
                        help="Seconds between resolve_event_children calls (default: 3.0)")
    parser.add_argument("--backoff", type=float, default=30.0)
    parser.add_argument("--dsn", default=os.getenv(
        "DATABASE_URL",
        "postgresql://arbiter:arbiter_secret@arbiter-postgres-prod:5432/arbiter_live",
    ))
    args = parser.parse_args()

    conn = await asyncpg.connect(args.dsn)
    logger.info("Connected to DB")

    # ── Fetch target rows ────────────────────────────────────────────
    rows = await conn.fetch("""
        SELECT canonical_id, forecastex_contract_id
        FROM market_mappings
        WHERE forecastex_contract_id != ''
          AND forecastex_contract_id != '0'
          AND (forecastex_no_contract_id = '' OR forecastex_no_contract_id IS NULL)
          AND forecastex_not_available = FALSE
        ORDER BY canonical_id
    """)
    logger.info("Found %d mappings needing NO conid (excluding conid=0)", len(rows))

    if not rows:
        await conn.close()
        return 0

    cfg = ForecastExConfig()
    client = ForecastExClient(
        gateway_url=cfg.gateway_url,
        account_id=cfg.account_id,
        verify_ssl=cfg.verify_ssl,
        paper_trading=cfg.paper_trading,
    )
    await client._ensure_iserver_bridge()
    logger.info("Bridge ready: %s", client._bridge_ready)

    # ── Classify conids: get contract_info for each unique conid ─────
    unique_conids = sorted(set(r["forecastex_contract_id"] for r in rows))
    logger.info("Fetching contract_info for %d unique conids...", len(unique_conids))

    conid_info: dict[str, dict] = {}
    for i, conid in enumerate(unique_conids):
        try:
            info = await client.get_contract_info(conid)
            conid_info[conid] = info if isinstance(info, dict) else {}
        except Exception as exc:
            logger.warning("contract_info(%s) failed: %s", conid, exc)
            conid_info[conid] = {}
        await asyncio.sleep(args.call_delay)
        if (i + 1) % 10 == 0:
            logger.info("  contract_info progress: %d/%d", i + 1, len(unique_conids))

    opt_conids = []   # (conid, info) — OPT children with right=CALL
    ind_conids = []   # (conid, info) — IND parents
    for conid, info in conid_info.items():
        itype = str(info.get("instrument_type") or "").upper()
        right = str(info.get("right") or "").upper()
        if itype == "OPT" and right in ("C", "CALL"):
            opt_conids.append((conid, info))
        elif info:
            ind_conids.append((conid, info))

    logger.info("Classification: %d OPT children (right=CALL), %d IND parents",
                 len(opt_conids), len(ind_conids))

    # Build lookup: canonical_id -> row
    canonical_for_conid: dict[str, list[str]] = defaultdict(list)
    for r in rows:
        canonical_for_conid[r["forecastex_contract_id"]].append(r["canonical_id"])

    # Stats
    updated_no = 0   # NO conid populated
    updated_yes_no = 0  # Both YES and NO populated (for IND parents)
    skipped = 0

    # ═══════════════════════════════════════════════════════════════════
    # PHASE A: OPT children — adjacent conid scan
    # ═══════════════════════════════════════════════════════════════════
    logger.info("=" * 60)
    logger.info("PHASE A: Adjacent scan for %d OPT conids", len(opt_conids))
    logger.info("=" * 60)

    for yes_conid, info in opt_conids:
        parent = str(
            info.get("underlying_con_id")
            or info.get("underlyingConid")
            or info.get("undConid")
            or info.get("underconid")
            or ""
        )
        strike = info.get("strike")
        try:
            strike_f = float(strike) if strike is not None else 1.0
        except (TypeError, ValueError):
            strike_f = 1.0

        if not parent or parent == "0":
            logger.warning("  [%s] OPT YES %s has no parent — skipping",
                           canonical_for_conid[yes_conid], yes_conid)
            skipped += 1
            continue

        yes_int = int(yes_conid)
        found_no: str | None = None

        for offset in range(-args.scan_range, args.scan_range + 1):
            if offset == 0:
                continue
            candidate = str(yes_int + offset)
            if candidate in conid_info:
                cinfo = conid_info[candidate]
            else:
                try:
                    cinfo = await client.get_contract_info(candidate)
                    conid_info[candidate] = cinfo if isinstance(cinfo, dict) else {}
                except RuntimeError as exc:
                    if "429" in str(exc):
                        logger.warning("  429 — backing off %.0fs", args.backoff)
                        await asyncio.sleep(args.backoff)
                        try:
                            cinfo = await client.get_contract_info(candidate)
                            conid_info[candidate] = cinfo if isinstance(cinfo, dict) else {}
                        except Exception:
                            cinfo = {}
                    else:
                        cinfo = {}
                except Exception:
                    cinfo = {}
                await asyncio.sleep(args.call_delay)

            if not isinstance(cinfo, dict) or not cinfo:
                continue

            c_parent = str(
                cinfo.get("underlying_con_id")
                or cinfo.get("underlyingConid")
                or cinfo.get("undConid")
                or cinfo.get("underconid")
                or ""
            )
            c_right = str(cinfo.get("right") or "").upper()
            if c_parent != parent or c_right not in ("P", "PUT"):
                continue
            try:
                c_strike = float(cinfo.get("strike") or 0.0)
            except (TypeError, ValueError):
                continue
            if abs(c_strike - strike_f) < 1e-6:
                found_no = candidate
                break

        if found_no:
            for cid in canonical_for_conid[yes_conid]:
                logger.info("  [%s] YES %s -> NO %s (offset=%s)",
                            cid, yes_conid, found_no,
                            int(found_no) - yes_int)
                if not args.dry_run:
                    await conn.execute("""
                        UPDATE market_mappings
                        SET forecastex_no_contract_id = $1, updated_at = NOW()
                        WHERE canonical_id = $2
                    """, found_no, cid)
                updated_no += 1
        else:
            for cid in canonical_for_conid[yes_conid]:
                logger.warning("  [%s] YES %s: no Put sibling in range +-%d",
                               cid, yes_conid, args.scan_range)
            skipped += len(canonical_for_conid[yes_conid])

    # ═══════════════════════════════════════════════════════════════════
    # PHASE B: IND parents — direct secdef/info for political parents
    # ═══════════════════════════════════════════════════════════════════
    # IBKR's secdef/strikes endpoint is heavily rate-limited and returns
    # empty for economic multi-outcome parents (CPI, GDP, FF, etc.) anyway.
    # Instead, we classify parents:
    #   - Political (senate/house/governor races): try direct secdef/info
    #     with strikes 1.0/2.0 for NOV26/NOV27/NOV28 (the cheap fallback)
    #   - Economic/crypto/weather: skip (documented IBKR limitation —
    #     no child OPT conids available via Client Portal API)
    logger.info("=" * 60)
    logger.info("PHASE B: Direct secdef/info for %d IND parents", len(ind_conids))
    logger.info("=" * 60)

    POLITICAL_KEYWORDS = frozenset({
        "HOUSE", "SENATE", "CONTROL", "MAJORITY", "PRESIDENT", "ELECTION",
        "GOVERNOR", "RACE", "PARTY", "HORC", "SENM", "PRPA", "PRESH",
        "PREST", "FEDRO",
    })
    POLITICAL_SYMBOL_PREFIXES = (
        "SR", "GR", "MXX", "AXX", "BXX", "OXX", "CXX", "GP", "H0", "I0",
        "HORC", "SENM", "PCD", "PCR", "PRPA", "PRESH", "PREST", "FEDRO",
        "PRAZH", "PRGAH", "PRMIH", "PRNCH", "PRNVH", "PRPAH", "PRWIH",
    )
    FALLBACK_MONTHS = ("NOV26", "NOV27", "NOV28")
    FALLBACK_STRIKES = (1.0, 2.0)

    for parent_conid, info in ind_conids:
        sym = str(info.get("symbol") or "").upper()
        name = str(info.get("company_name") or "").upper()
        mappings_using = canonical_for_conid[parent_conid]

        # Classify: political or economic?
        is_political = (
            any(kw in name for kw in POLITICAL_KEYWORDS)
            or any(sym.startswith(pfx) for pfx in POLITICAL_SYMBOL_PREFIXES)
        )

        if not is_political:
            logger.info("  Parent %s (%s — %s) x%d: SKIP — economic/multi-outcome (no API children)",
                         parent_conid, sym, name[:40], len(mappings_using))
            skipped += len(mappings_using)
            continue

        logger.info("  Parent %s (%s — %s) x%d: POLITICAL — trying direct secdef/info",
                     parent_conid, sym, name[:40], len(mappings_using))

        # Direct secdef/info fallback: try strikes 1.0/2.0 for election months
        children: list[dict] = []
        for month in FALLBACK_MONTHS:
            month_hits = 0
            for strike in FALLBACK_STRIKES:
                try:
                    payload = await client._request(
                        "GET", "/iserver/secdef/info",
                        params={
                            "conid": str(parent_conid),
                            "exchange": "FORECASTX",
                            "sectype": "OPT",
                            "month": month,
                            "strike": str(strike),
                        },
                        use_circuit=False,
                    )
                except RuntimeError as exc:
                    if "429" in str(exc):
                        logger.warning("    429 on secdef/info — backing off %.0fs", args.backoff)
                        await asyncio.sleep(args.backoff)
                        try:
                            payload = await client._request(
                                "GET", "/iserver/secdef/info",
                                params={
                                    "conid": str(parent_conid),
                                    "exchange": "FORECASTX",
                                    "sectype": "OPT",
                                    "month": month,
                                    "strike": str(strike),
                                },
                                use_circuit=False,
                            )
                        except Exception:
                            continue
                    else:
                        continue
                except Exception:
                    continue

                items = payload.get("items") if isinstance(payload, dict) else payload
                if not isinstance(items, list):
                    continue
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    c_conid = str(item.get("conid") or "").strip()
                    if not c_conid:
                        continue
                    children.append({
                        "conid": c_conid,
                        "right": str(item.get("right") or "").upper(),
                        "strike": item.get("strike"),
                        "desc": item.get("desc2") or item.get("desc1") or "",
                    })
                    month_hits += 1
                await asyncio.sleep(args.call_delay)
            if month_hits:
                break  # First month with hits wins

        if not children:
            logger.warning("    0 children from secdef/info fallback")
            skipped += len(mappings_using)
            await asyncio.sleep(1.0)
            continue

        # Group by strike
        calls_by_strike: dict[float, str] = {}
        puts_by_strike: dict[float, str] = {}
        for child in children:
            c_conid = child["conid"]
            c_right = child["right"]
            try:
                c_strike = float(child.get("strike") or 0.0)
            except (TypeError, ValueError):
                continue
            c_desc = child.get("desc", "")[:50]
            logger.info("      conid=%s right=%s strike=%.1f desc=%s",
                         c_conid, c_right, c_strike, c_desc)
            if c_right in ("C", "CALL") and c_conid:
                calls_by_strike[c_strike] = c_conid
            elif c_right in ("P", "PUT") and c_conid:
                puts_by_strike[c_strike] = c_conid

        # Match children to mappings
        if len(calls_by_strike) == 1 and len(puts_by_strike) == 1:
            call_conid = list(calls_by_strike.values())[0]
            put_conid = list(puts_by_strike.values())[0]
            for cid in mappings_using:
                logger.info("    [%s] parent %s -> YES=%s NO=%s",
                            cid, parent_conid, call_conid, put_conid)
                if not args.dry_run:
                    await conn.execute("""
                        UPDATE market_mappings
                        SET forecastex_contract_id = $1,
                            forecastex_no_contract_id = $2,
                            updated_at = NOW()
                        WHERE canonical_id = $3
                    """, call_conid, put_conid, cid)
                updated_yes_no += 1
        elif calls_by_strike and puts_by_strike:
            for cid in mappings_using:
                cid_lower = cid.lower()
                matched_strike: float | None = None
                if any(tok in cid_lower for tok in ("dem_", "_dem", "democratic")):
                    matched_strike = 1.0
                elif any(tok in cid_lower for tok in ("gop_", "_gop", "republican", "rep_")):
                    matched_strike = 2.0

                if matched_strike is not None:
                    call_c = calls_by_strike.get(matched_strike)
                    put_c = puts_by_strike.get(matched_strike)
                    if call_c and put_c:
                        logger.info("    [%s] matched strike=%.1f -> YES=%s NO=%s",
                                    cid, matched_strike, call_c, put_c)
                        if not args.dry_run:
                            await conn.execute("""
                                UPDATE market_mappings
                                SET forecastex_contract_id = $1,
                                    forecastex_no_contract_id = $2,
                                    updated_at = NOW()
                                WHERE canonical_id = $3
                            """, call_c, put_c, cid)
                        updated_yes_no += 1
                    else:
                        logger.warning("    [%s] matched strike=%.1f but missing call/put",
                                       cid, matched_strike)
                        skipped += 1
                else:
                    logger.warning("    [%s] cannot auto-match strike (available: %s)",
                                   cid, sorted(calls_by_strike.keys()))
                    skipped += 1
        else:
            logger.warning("    No Call/Put pairs (calls=%d puts=%d)",
                           len(calls_by_strike), len(puts_by_strike))
            skipped += len(mappings_using)

        await asyncio.sleep(1.0)

    # ── Summary ──────────────────────────────────────────────────────
    mode = "DRY RUN" if args.dry_run else "LIVE"
    print(f"\n{'='*60}")
    print(f"  populate_no_conids [{mode}] complete")
    print(f"{'='*60}")
    print(f"  Total mappings:                {len(rows)}")
    print(f"  Phase A (OPT adjacent scan):   {len(opt_conids)} conids")
    print(f"    -> NO conids populated:      {updated_no}")
    print(f"  Phase B (IND resolve):         {len(ind_conids)} parents")
    print(f"    -> YES+NO conids populated:  {updated_yes_no}")
    print(f"  Total updated:                 {updated_no + updated_yes_no}")
    print(f"  Skipped (unresolvable):        {skipped}")
    print(f"{'='*60}\n")

    await client.close()
    await conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
