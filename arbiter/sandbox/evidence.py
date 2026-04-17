"""Per-scenario evidence helpers (DB dumps + balance snapshots)."""
from __future__ import annotations

import json
import pathlib
from typing import Any, Dict

import asyncpg


SANDBOX_TABLES = (
    "execution_orders",
    "execution_fills",
    "execution_incidents",
    "execution_arbs",
)


async def dump_execution_tables(pool: asyncpg.Pool, directory: pathlib.Path) -> None:
    """Dump every row from execution_* tables into <table>.json files under `directory`."""
    directory.mkdir(parents=True, exist_ok=True)
    for table in SANDBOX_TABLES:
        async with pool.acquire() as conn:
            rows = await conn.fetch(f"SELECT * FROM {table}")
        (directory / f"{table}.json").write_text(
            json.dumps([dict(r) for r in rows], indent=2, default=str),
            encoding="utf-8",
        )


def write_balances(
    directory: pathlib.Path,
    pre: Dict[str, Any],
    post: Dict[str, Any],
) -> None:
    """Write pre/post BalanceMonitor snapshots as balances_pre.json and balances_post.json."""
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "balances_pre.json").write_text(
        json.dumps(pre, indent=2, default=str), encoding="utf-8",
    )
    (directory / "balances_post.json").write_text(
        json.dumps(post, indent=2, default=str), encoding="utf-8",
    )
