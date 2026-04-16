"""Forward-only PostgreSQL migration runner for arbiter.

Usage:
    python -m arbiter.sql.migrate                   # apply all pending migrations
    python -m arbiter.sql.migrate --status          # show what is/isn't applied

Migrations live in arbiter/sql/migrations/ as `NNN_name.sql` files. The runner
applies them in filename-sorted order, recording each in a `schema_migrations`
table. Already-applied migrations are skipped.

Migrations are append-only -- never edit a file after applying it to any env.

Security note: DATABASE_URL may contain a password; it is NEVER logged or printed
by this module. Only migration filenames are emitted.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path
from typing import Iterable

import asyncpg

logger = logging.getLogger("arbiter.sql.migrate")

MIGRATIONS_DIR = Path(__file__).parent / "migrations"

SCHEMA_MIGRATIONS_DDL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    filename    TEXT PRIMARY KEY,
    applied_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
"""


def _list_migration_files() -> list[Path]:
    if not MIGRATIONS_DIR.exists():
        return []
    return sorted(MIGRATIONS_DIR.glob("*.sql"))


async def _applied_filenames(conn: asyncpg.Connection) -> set[str]:
    rows = await conn.fetch("SELECT filename FROM schema_migrations")
    return {row["filename"] for row in rows}


async def apply_pending(database_url: str) -> list[str]:
    """Connect, apply any unapplied migrations in order, return list of applied filenames."""
    conn = await asyncpg.connect(database_url)
    try:
        await conn.execute(SCHEMA_MIGRATIONS_DDL)
        already = await _applied_filenames(conn)
        applied: list[str] = []
        for path in _list_migration_files():
            if path.name in already:
                logger.info("skip %s (already applied)", path.name)
                continue
            logger.info("apply %s", path.name)
            sql = path.read_text(encoding="utf-8")
            async with conn.transaction():
                await conn.execute(sql)
                await conn.execute(
                    "INSERT INTO schema_migrations (filename) VALUES ($1)", path.name
                )
            applied.append(path.name)
        return applied
    finally:
        await conn.close()


async def status(database_url: str) -> tuple[list[str], list[str]]:
    """Return (applied, pending) filenames."""
    conn = await asyncpg.connect(database_url)
    try:
        await conn.execute(SCHEMA_MIGRATIONS_DDL)
        already = await _applied_filenames(conn)
    finally:
        await conn.close()
    all_files = [p.name for p in _list_migration_files()]
    applied = [f for f in all_files if f in already]
    pending = [f for f in all_files if f not in already]
    return applied, pending


def _resolve_database_url() -> str:
    url = os.getenv("DATABASE_URL")
    if not url:
        raise SystemExit(
            "DATABASE_URL env var not set. Source .env or export DATABASE_URL=postgresql://..."
        )
    return url


def main(argv: Iterable[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser(description="ARBITER PostgreSQL migration runner")
    parser.add_argument("--status", action="store_true", help="Show applied vs pending")
    args = parser.parse_args(list(argv) if argv is not None else None)

    database_url = _resolve_database_url()

    if args.status:
        applied, pending = asyncio.run(status(database_url))
        print(f"applied ({len(applied)}):")
        for f in applied:
            print(f"  {f}")
        print(f"pending ({len(pending)}):")
        for f in pending:
            print(f"  {f}")
        return 0

    applied = asyncio.run(apply_pending(database_url))
    if applied:
        print(f"applied {len(applied)} migration(s):")
        for f in applied:
            print(f"  {f}")
    else:
        print("no pending migrations")
    return 0


if __name__ == "__main__":
    sys.exit(main())
