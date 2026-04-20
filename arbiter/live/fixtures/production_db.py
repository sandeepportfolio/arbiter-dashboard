"""Production DB asyncpg pool fixture (hard-asserts DATABASE_URL is arbiter_live).

Inverse of arbiter/sandbox/fixtures/sandbox_db.py: refuses to run unless
``DATABASE_URL`` contains ``arbiter_live`` AND does NOT contain ``arbiter_sandbox``
or ``arbiter_dev``. This is the schema-level guard rail that keeps Phase 5
live-fire tests from accidentally writing into the sandbox or dev databases.
"""
from __future__ import annotations

import os

import asyncpg
import pytest


@pytest.fixture
async def production_db_pool():
    """asyncpg pool pointed at ``arbiter_live``. Refuses to connect elsewhere."""
    url = os.getenv("DATABASE_URL", "")
    assert "arbiter_live" in url, (
        f"SAFETY: DATABASE_URL must include 'arbiter_live' for Phase 5 live-fire; "
        f"got {url!r}. Source .env.production before running `pytest -m live`."
    )
    assert "arbiter_sandbox" not in url, (
        f"SAFETY: DATABASE_URL must NOT be sandbox; got {url!r}."
    )
    assert "arbiter_dev" not in url, (
        f"SAFETY: DATABASE_URL must NOT be dev; got {url!r}."
    )
    pool = await asyncpg.create_pool(url, min_size=1, max_size=4)
    try:
        yield pool
    finally:
        await pool.close()
