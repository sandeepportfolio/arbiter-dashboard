#!/usr/bin/env python3
"""Backfill ``execution_arbs.analysis_md`` for every existing arb.

Run inside the prod container so DATABASE_URL is set::

    docker exec arbiter-api-prod python -m scripts.backfill_trade_analysis
    docker exec arbiter-api-prod python -m scripts.backfill_trade_analysis --force
    docker exec arbiter-api-prod python -m scripts.backfill_trade_analysis --arb ARB-000203

Without ``--force`` only rows whose stored ``analysis_version`` is older than
the current ``ANALYZER_VERSION`` are rewritten — safe to re-run idempotently.
Migration 004 must already be applied; the script aborts otherwise.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from typing import List

# Allow `python scripts/backfill_trade_analysis.py` from repo root or as -m.
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from arbiter.analysis.trade_analyzer import ANALYZER_VERSION, analyze_arb_from_db  # noqa: E402
from arbiter.sql.connection import connect  # noqa: E402

logger = logging.getLogger("backfill_trade_analysis")


async def _ensure_columns(conn) -> None:
    cols = await conn.fetch(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name = 'execution_arbs' AND column_name IN "
        "('analysis_md', 'analysis_version', 'analysis_updated_at')"
    )
    have = {r["column_name"] for r in cols}
    missing = {"analysis_md", "analysis_version", "analysis_updated_at"} - have
    if missing:
        raise SystemExit(
            f"Migration 004 not applied yet — missing columns: {sorted(missing)}. "
            "Run `python -m arbiter.sql.migrate` first."
        )


async def _select_arbs(conn, *, arb_ids: List[str], force: bool) -> List[str]:
    if arb_ids:
        return list(arb_ids)
    if force:
        rows = await conn.fetch(
            "SELECT arb_id FROM execution_arbs ORDER BY created_at ASC"
        )
    else:
        rows = await conn.fetch(
            "SELECT arb_id FROM execution_arbs "
            "WHERE COALESCE(analysis_version, 0) < $1 "
            "   OR COALESCE(analysis_md, '') = '' "
            "ORDER BY created_at ASC",
            ANALYZER_VERSION,
        )
    return [r["arb_id"] for r in rows]


async def _backfill_one(conn, arb_id: str) -> bool:
    try:
        md = await analyze_arb_from_db(conn, arb_id)
    except LookupError:
        logger.warning("skip %s: not found", arb_id)
        return False
    except Exception as exc:  # noqa: BLE001 - log and move on
        logger.error("skip %s: analyzer raised: %s", arb_id, exc)
        return False
    await conn.execute(
        "UPDATE execution_arbs "
        "   SET analysis_md = $2, "
        "       analysis_version = $3, "
        "       analysis_updated_at = NOW() "
        " WHERE arb_id = $1",
        arb_id,
        md,
        ANALYZER_VERSION,
    )
    return True


async def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument(
        "--force",
        action="store_true",
        help="re-write every arb regardless of stored analysis_version",
    )
    p.add_argument(
        "--arb",
        action="append",
        default=[],
        metavar="ARB-NNNNNN",
        help="restrict to specific arb_id(s) (repeatable)",
    )
    args = p.parse_args(argv)

    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        raise SystemExit("DATABASE_URL is not set")

    conn = await connect(db_url)
    try:
        await _ensure_columns(conn)
        targets = await _select_arbs(conn, arb_ids=args.arb, force=args.force)
        if not targets:
            print("nothing to do — all analyses are at version", ANALYZER_VERSION)
            return 0
        print(f"backfilling {len(targets)} arb(s) at version {ANALYZER_VERSION}")
        ok = 0
        for arb_id in targets:
            if await _backfill_one(conn, arb_id):
                ok += 1
                print(f"  ✓ {arb_id}")
            else:
                print(f"  ✗ {arb_id}")
        print(f"done: {ok}/{len(targets)} succeeded")
        return 0 if ok == len(targets) else 2
    finally:
        await conn.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    sys.exit(asyncio.run(main(sys.argv[1:])))
