#!/usr/bin/env python3
"""
ARBITER — Database migration runner.
Usage: python scripts/migrate.py [--plan] [--apply]
"""
from __future__ import annotations

import argparse
import asyncio
import asyncpg
import logging
import os
import sys
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logger = logging.getLogger("arbiter.migrate")


MIGRATIONS = [
    # ── 001: Initial schema ──────────────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS trades (
        id SERIAL PRIMARY KEY,
        arb_id VARCHAR(20) NOT NULL,
        canonical_id VARCHAR(50) NOT NULL,
        yes_platform VARCHAR(20) NOT NULL,
        yes_price DECIMAL(6,4) NOT NULL,
        yes_market_id VARCHAR(100),
        no_platform VARCHAR(20) NOT NULL,
        no_price DECIMAL(6,4) NOT NULL,
        no_market_id VARCHAR(100),
        quantity INT NOT NULL,
        gross_edge DECIMAL(6,4),
        total_fees DECIMAL(6,4),
        net_edge DECIMAL(6,4),
        realized_pnl DECIMAL(10,4),
        status VARCHAR(20) DEFAULT 'pending',
        is_simulation BOOLEAN DEFAULT TRUE,
        created_at TIMESTAMPTZ DEFAULT NOW()
    );
    """,
    # ── 002: Positions ────────────────────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS positions (
        position_id VARCHAR(40) PRIMARY KEY,
        canonical_id VARCHAR(60) NOT NULL,
        description TEXT,
        yes_platform VARCHAR(20) NOT NULL,
        no_platform VARCHAR(20) NOT NULL,
        yes_market_id VARCHAR(100) DEFAULT '',
        no_market_id VARCHAR(100) DEFAULT '',
        quantity INT NOT NULL,
        yes_price DECIMAL(8,4) NOT NULL,
        no_price DECIMAL(8,4) NOT NULL,
        status VARCHAR(20) NOT NULL DEFAULT 'open',
        hedge_status VARCHAR(20) NOT NULL DEFAULT 'none',
        hedge_order_id VARCHAR(100) DEFAULT '',
        yes_order_id VARCHAR(100) DEFAULT '',
        no_order_id VARCHAR(100) DEFAULT '',
        yes_fill_price DECIMAL(8,4) DEFAULT 0,
        no_fill_price DECIMAL(8,4) DEFAULT 0,
        realized_pnl DECIMAL(10,4) DEFAULT 0,
        settlement_price DECIMAL(8,4) DEFAULT 0,
        settlement_pnl DECIMAL(10,4) DEFAULT 0,
        fees_paid DECIMAL(10,4) DEFAULT 0,
        is_simulation BOOLEAN DEFAULT TRUE,
        unwind_reason TEXT DEFAULT '',
        notes TEXT[] DEFAULT '{}',
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        entry_confirmed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        closed_at TIMESTAMPTZ,
        settled_at TIMESTAMPTZ
    );
    """,
    # ── 003: Position events ──────────────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS position_events (
        id SERIAL PRIMARY KEY,
        position_id VARCHAR(40) NOT NULL,
        event_type VARCHAR(30) NOT NULL,
        delta_pnl DECIMAL(10,4) DEFAULT 0,
        delta_fees DECIMAL(10,4) DEFAULT 0,
        metadata JSONB DEFAULT '{}',
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );
    """,
    # ── 004: Market mappings ─────────────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS market_mappings (
        canonical_id VARCHAR(60) PRIMARY KEY,
        description TEXT NOT NULL,
        status VARCHAR(20) NOT NULL DEFAULT 'candidate',
        allow_auto_trade BOOLEAN DEFAULT FALSE,
        aliases TEXT[] DEFAULT '{}',
        tags TEXT[] DEFAULT '{}',
        kalshi_market_id VARCHAR(100) DEFAULT '',
        polymarket_slug VARCHAR(200) DEFAULT '',
        polymarket_question TEXT DEFAULT '',
        predictit_id VARCHAR(100) DEFAULT '',
        predictit_contract_keywords TEXT[] DEFAULT '{}',
        notes TEXT DEFAULT '',
        review_note TEXT DEFAULT '',
        mapping_score DECIMAL(5,4) DEFAULT 0,
        confidence DECIMAL(5,4) DEFAULT 0,
        expires_at TIMESTAMPTZ,
        last_validated_at TIMESTAMPTZ,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );
    """,
    # ── 005: Mapping candidates ───────────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS mapping_candidates (
        id SERIAL PRIMARY KEY,
        canonical_id VARCHAR(60) NOT NULL,
        platform VARCHAR(20) NOT NULL,
        platform_market_id VARCHAR(200) NOT NULL,
        description TEXT,
        match_score DECIMAL(5,4) DEFAULT 0,
        status VARCHAR(20) DEFAULT 'pending',
        reviewed_at TIMESTAMPTZ,
        reviewer_note TEXT DEFAULT '',
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        UNIQUE(platform, platform_market_id)
    );
    """,
    # ── 006: Indexes ──────────────────────────────────────────────────────────
    """
    CREATE INDEX IF NOT EXISTS idx_trades_canonical ON trades(canonical_id);
    CREATE INDEX IF NOT EXISTS idx_trades_created ON trades(created_at);
    CREATE INDEX IF NOT EXISTS idx_positions_canonical ON positions(canonical_id);
    CREATE INDEX IF NOT EXISTS idx_positions_status ON positions(status);
    CREATE INDEX IF NOT EXISTS idx_positions_created ON positions(created_at);
    CREATE INDEX IF NOT EXISTS idx_position_events_position ON position_events(position_id);
    CREATE INDEX IF NOT EXISTS idx_mappings_status ON market_mappings(status);
    CREATE INDEX IF NOT EXISTS idx_mappings_kalshi ON market_mappings(kalshi_market_id) WHERE kalshi_market_id != '';
    CREATE INDEX IF NOT EXISTS idx_mappings_poly ON market_mappings(polymarket_slug) WHERE polymarket_slug != '';
    CREATE INDEX IF NOT EXISTS idx_mappings_predictit ON market_mappings(predictit_id) WHERE predictit_id != '';
    CREATE INDEX IF NOT EXISTS idx_mappings_expires ON market_mappings(expires_at) WHERE expires_at IS NOT NULL;
    CREATE INDEX IF NOT EXISTS idx_candidates_status ON mapping_candidates(status);
    CREATE INDEX IF NOT EXISTS idx_candidates_canonical ON mapping_candidates(canonical_id);
    """,
]


async def get_current_version(conn: asyncpg.Connection) -> int:
    """Get the last applied migration number."""
    try:
        row = await conn.fetchrow("SELECT MAX(version) as v FROM schema_migrations")
        return int(row["v"] or 0)
    except asyncpg.UndefinedTableError:
        return 0


async def apply_migration(conn: asyncpg.Connection, version: int, sql: str):
    """Apply a single migration inside a transaction."""
    async with conn.transaction():
        await conn.execute(sql)
        await conn.execute(
            "INSERT INTO schema_migrations (version, applied_at) VALUES ($1, NOW())",
            version,
        )
    logger.info(f"  ✓ Migration {version:03d} applied")


async def run_migrations(database_url: str, dry_run: bool = False):
    """Run all pending migrations."""
    logger.info(f"Connecting to database...")
    conn = await asyncpg.connect(database_url, timeout=30)

    try:
        # Ensure migrations tracking table exists
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version INTEGER PRIMARY KEY,
                applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)

        current = await get_current_version(conn)
        total = len(MIGRATIONS)

        logger.info(f"Current version: {current} / {total}")
        logger.info(f"Pending: {total - current}")

        if dry_run:
            logger.info("[DRY RUN] No changes made.")
            for i, sql in enumerate(MIGRATIONS[current:], start=current + 1):
                stmt = sql.strip().split("\n")[0][:60]
                logger.info(f"  Would apply: migration {i:03d}: {stmt}")
            return

        applied = 0
        for i, sql in enumerate(MIGRATIONS, start=1):
            if i <= current:
                continue
            stmt_preview = sql.strip().split("\n")[0][:60]
            logger.info(f"Applying migration {i:03d}: {stmt_preview}")
            await apply_migration(conn, i, sql)
            applied += 1

        if applied == 0:
            logger.info("Database is up to date.")
        else:
            logger.info(f"Done. Applied {applied} migration(s).")

    finally:
        await conn.close()


async def verify_database(database_url: str) -> bool:
    """Verify database connectivity and required extensions."""
    try:
        conn = await asyncpg.connect(database_url, timeout=10)
        try:
            await conn.fetchval("SELECT 1")
            # Check required tables
            tables = [
                "trades", "positions", "position_events",
                "market_mappings", "mapping_candidates", "schema_migrations",
            ]
            for table in tables:
                try:
                    await conn.fetchval(f'SELECT 1 FROM {table} LIMIT 1')
                except asyncpg.UndefinedTableError:
                    logger.error(f"  ✗ Missing table: {table}")
                    return False
            logger.info("Database verification: ✓ all tables present")
            return True
        finally:
            await conn.close()
    except Exception as e:
        logger.error(f"Database verification failed: {e}")
        return False


def validate_env() -> list[str]:
    """Validate required environment variables. Returns list of errors."""
    errors = []
    required = {
        "DATABASE_URL": "Postgres connection string (e.g. postgresql://user:pass@host:5432/arbiter)",
        "KALSHI_API_KEY_ID": "Kalshi API key ID",
        "KALSHI_PRIVATE_KEY_PATH": "Path to Kalshi private key file",
    }
    optional = {
        "POLY_PRIVATE_KEY": "Polymarket wallet private key",
        "TELEGRAM_BOT_TOKEN": "Telegram bot token for alerts",
        "TELEGRAM_CHAT_ID": "Telegram chat ID for alerts",
        "DRY_RUN": "Set to 'false' to enable live trading (default: true)",
        "REDIS_URL": "Redis URL for quote cache (optional)",
    }
    for var, desc in required.items():
        if not os.getenv(var):
            errors.append(f"  ✗ {var} is required: {desc}")
        else:
            logger.info(f"  ✓ {var} is set")
    for var, desc in optional.items():
        if os.getenv(var):
            logger.info(f"  ✓ {var} is set")
        else:
            logger.info(f"  - {var} not set (optional): {desc}")
    return errors


def validate_secrets() -> list[str]:
    """Validate that secret files exist and are readable."""
    errors = []
    key_path = os.getenv("KALSHI_PRIVATE_KEY_PATH", "")
    if key_path:
        path = Path(key_path).expanduser()
        if not path.exists():
            errors.append(f"  ✗ Kalshi private key not found at: {key_path}")
        elif not os.access(path, os.R_OK):
            errors.append(f"  ✗ Kalshi private key not readable: {key_path}")
        else:
            logger.info(f"  ✓ Kalshi private key readable at: {key_path}")
    poly_key = os.getenv("POLY_PRIVATE_KEY", "")
    if poly_key and len(poly_key) < 32:
        errors.append(f"  ✗ Polymarket private key looks too short (got {len(poly_key)} chars)")
    else:
        logger.info(f"  ✓ Polymarket key length OK")
    return errors


def print_startup_banner(config: dict):
    """Print a startup banner with config summary."""
    print("=" * 60)
    print("ARBITER — Production Startup")
    print("=" * 60)
    dry_run = os.getenv("DRY_RUN", "true").lower() != "false"
    print(f"  DRY_RUN:        {'✓ ENABLED (no real trades)' if dry_run else '✗ DISABLED — LIVE TRADING'}")
    print(f"  DATABASE:       {'✓ configured' if os.getenv('DATABASE_URL') else '✗ not configured'}")
    print(f"  KALSHI:         {'✓ configured' if os.getenv('KALSHI_API_KEY_ID') else '✗ not configured'}")
    print(f"  POLYMARKET:     {'✓ configured' if os.getenv('POLY_PRIVATE_KEY') else '- optional'}")
    print(f"  TELEGRAM:       {'✓ configured' if os.getenv('TELEGRAM_BOT_TOKEN') else '- optional'}")
    print(f"  REDIS:          {'✓ configured' if os.getenv('REDIS_URL') else '- optional (using in-memory)'}")
    print("=" * 60)


def main():
    parser = argparse.ArgumentParser(description="ARBITER migration and startup tool")
    parser.add_argument("--plan", action="store_true", help="Show pending migrations without applying")
    parser.add_argument("--apply", action="store_true", help="Apply migrations")
    parser.add_argument("--verify", action="store_true", help="Verify database connectivity")
    parser.add_argument("--check-env", action="store_true", help="Validate environment variables")
    args = parser.parse_args()

    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        logger.error("DATABASE_URL not set. Set it in .env or environment.")
        sys.exit(1)

    if args.check_env:
        print("Environment validation:")
        errors = validate_env()
        errors += validate_secrets()
        if errors:
            for e in errors:
                print(e)
            sys.exit(1)
        else:
            print("Environment: ✓ PASS")
        return

    if args.verify:
        ok = asyncio.run(verify_database(db_url))
        sys.exit(0 if ok else 1)

    if args.plan:
        asyncio.run(run_migrations(db_url, dry_run=True))
        return

    if args.apply:
        asyncio.run(run_migrations(db_url, dry_run=False))
        return

    parser.print_help()


if __name__ == "__main__":
    main()
