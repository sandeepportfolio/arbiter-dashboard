"""Sandbox DB asyncpg pool fixture (hard-asserts DATABASE_URL is the sandbox DB)."""
from __future__ import annotations

import os

import asyncpg
import pytest


@pytest.fixture
async def sandbox_db_pool():
    """asyncpg pool pointed at `arbiter_sandbox`. Refuses to connect to any other DB."""
    url = os.getenv("DATABASE_URL", "")
    assert "arbiter_sandbox" in url, (
        f"SAFETY: DATABASE_URL must point at the arbiter_sandbox DB for Phase 4 live-fire; "
        f"got {url!r}. Source .env.sandbox before running `pytest -m live`."
    )
    pool = await asyncpg.create_pool(url, min_size=1, max_size=4)
    try:
        yield pool
    finally:
        await pool.close()
