"""Tests for arbiter.execution.store.ExecutionStore.

- Unit tier: MockPool/MockConn -- no real Postgres required (CI-default).
- Integration tier: real asyncpg.Pool -- gated on DATABASE_URL env var
  (run locally with `docker compose up -d postgres` first).
"""
from __future__ import annotations

import json
import os
import time
from typing import Any, List, Optional, Tuple

import pytest

from arbiter.execution.engine import ExecutionIncident, Order, OrderStatus
from arbiter.execution.store import ExecutionStore, _derive_arb_id


# ─── _derive_arb_id ────────────────────────────────────────────────────────

def test_derive_arb_id_recognizes_standard_prefix():
    assert _derive_arb_id("ARB-000001-YES-abcd1234") == "ARB-000001"
    assert _derive_arb_id("ARB-999999-NO-deadbeef") == "ARB-999999"


def test_derive_arb_id_returns_none_for_unknown():
    assert _derive_arb_id("") is None
    assert _derive_arb_id("MANUAL-ARB-001") is None
    assert _derive_arb_id(None) is None


# ─── MockPool / MockConn ───────────────────────────────────────────────────

class MockConn:
    """Records every execute/fetch/fetchrow call. Configurable responses."""

    def __init__(self, fetch_response: Any = None, fetchrow_response: Any = None):
        self.calls: List[Tuple[str, str, tuple]] = []  # (method, sql, args)
        self._fetch_response = fetch_response if fetch_response is not None else []
        self._fetchrow_response = fetchrow_response

    async def execute(self, sql: str, *args) -> str:
        self.calls.append(("execute", sql, args))
        return "OK"

    async def fetch(self, sql: str, *args) -> List[Any]:
        self.calls.append(("fetch", sql, args))
        return self._fetch_response

    async def fetchrow(self, sql: str, *args) -> Optional[Any]:
        self.calls.append(("fetchrow", sql, args))
        return self._fetchrow_response

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class MockPool:
    """Minimal asyncpg.Pool replacement for unit tests."""

    def __init__(self, conn: Optional[MockConn] = None):
        self.conn = conn or MockConn()
        self.closed = False

    def acquire(self):
        # asyncpg.Pool.acquire() returns an async context manager
        pool = self

        class _Ctx:
            async def __aenter__(self_):
                return pool.conn

            async def __aexit__(self_, *exc):
                return False

        return _Ctx()

    async def close(self):
        self.closed = True


@pytest.fixture
def mock_pool(monkeypatch):
    pool = MockPool()

    async def fake_connect(self):
        if self._pool is None:
            self._pool = pool

    monkeypatch.setattr(ExecutionStore, "connect", fake_connect)
    return pool


# ─── Order: INSERT path ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_upsert_order_writes_full_insert(mock_pool):
    store = ExecutionStore(database_url="postgres://mock/mock")
    await store.connect()
    order = Order(
        order_id="ARB-000001-YES-deadbeef",
        platform="kalshi",
        market_id="DEM_HOUSE_2026-YES",
        canonical_id="DEM_HOUSE_2026",
        side="yes",
        price=0.55,
        quantity=10,
        status=OrderStatus.SUBMITTED,
        fill_price=0.0,
        fill_qty=0,
        timestamp=time.time(),
    )
    await store.upsert_order(order, client_order_id="cid-deadbeef")
    assert mock_pool.conn.calls, "upsert_order made no DB call"
    method, sql, args = mock_pool.conn.calls[-1]
    assert method == "execute"
    assert "INSERT INTO execution_orders" in sql
    assert "ON CONFLICT (order_id) DO UPDATE" in sql
    # First positional arg is order_id
    assert args[0] == "ARB-000001-YES-deadbeef"
    # arb_id derived from prefix
    assert args[1] == "ARB-000001"
    # client_order_id passed through
    assert args[2] == "cid-deadbeef"


@pytest.mark.asyncio
async def test_upsert_order_writes_terminal_at_when_filled(mock_pool):
    store = ExecutionStore(database_url="postgres://mock/mock")
    await store.connect()
    order = Order(
        order_id="ARB-000002-YES-aaaaaaaa",
        platform="kalshi",
        market_id="m",
        canonical_id="c",
        side="yes",
        price=0.5,
        quantity=1,
        status=OrderStatus.FILLED,
    )
    await store.upsert_order(order)
    _, sql, _ = mock_pool.conn.calls[-1]
    # The UPDATE branch should set terminal_at = NOW() for FILLED orders.
    update_branch = sql.split("ON CONFLICT")[1] if "ON CONFLICT" in sql else sql
    assert "terminal_at = NOW()" in update_branch, (
        f"FILLED order should set terminal_at = NOW(); got SQL: {sql}"
    )


@pytest.mark.asyncio
async def test_upsert_order_does_not_set_terminal_at_when_pending(mock_pool):
    store = ExecutionStore(database_url="postgres://mock/mock")
    await store.connect()
    order = Order(
        order_id="ARB-000003-NO-bbbbbbbb",
        platform="polymarket",
        market_id="m2",
        canonical_id="c2",
        side="no",
        price=0.45,
        quantity=2,
        status=OrderStatus.PENDING,
    )
    await store.upsert_order(order)
    _, sql, _ = mock_pool.conn.calls[-1]
    # The UPDATE branch should preserve existing terminal_at when status is not terminal.
    update_branch = sql.split("ON CONFLICT")[1] if "ON CONFLICT" in sql else sql
    assert "terminal_at = execution_orders.terminal_at" in update_branch, (
        f"PENDING order should preserve terminal_at; got SQL: {sql}"
    )


@pytest.mark.asyncio
async def test_upsert_order_raises_when_arb_id_underivable(mock_pool):
    store = ExecutionStore(database_url="postgres://mock/mock")
    await store.connect()
    order = Order(
        order_id="MANUAL-WEIRD-ID",
        platform="kalshi",
        market_id="m",
        canonical_id="c",
        side="yes",
        price=0.5,
        quantity=1,
        status=OrderStatus.PENDING,
    )
    with pytest.raises(ValueError, match="arb_id"):
        await store.upsert_order(order)


# ─── Incident persistence ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_insert_incident_serializes_metadata_as_jsonb(mock_pool):
    store = ExecutionStore(database_url="postgres://mock/mock")
    await store.connect()
    incident = ExecutionIncident(
        incident_id="INC-deadbeef",
        arb_id="ARB-000001",
        canonical_id="DEM_HOUSE_2026",
        severity="warning",
        message="circuit open on kalshi",
        timestamp=time.time(),
        metadata={"platform": "kalshi", "consecutive_failures": 5},
    )
    await store.insert_incident(incident)
    _, sql, args = mock_pool.conn.calls[-1]
    assert "INSERT INTO execution_incidents" in sql
    metadata_arg = args[5]  # 6th positional ($6) is metadata json string
    parsed = json.loads(metadata_arg)
    assert parsed == {"platform": "kalshi", "consecutive_failures": 5}


# ─── Non-terminal listing ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_list_non_terminal_orders_filters_correctly(mock_pool):
    """SQL must include the non-terminal status filter literal."""
    # Replace the default conn with one that returns an empty fetch
    mock_pool.conn = MockConn(fetch_response=[])
    store = ExecutionStore(database_url="postgres://mock/mock")
    await store.connect()
    result = await store.list_non_terminal_orders()
    assert result == []
    method, sql, _ = mock_pool.conn.calls[-1]
    assert method == "fetch"
    assert "WHERE status IN ('pending', 'submitted', 'partial')" in sql


# ─── Integration tier (real Postgres) ─────────────────────────────────────

INTEGRATION_DB_URL = os.getenv("DATABASE_URL")


@pytest.mark.skipif(not INTEGRATION_DB_URL, reason="DATABASE_URL not set; skipping integration test")
@pytest.mark.asyncio
async def test_integration_order_lifecycle_persisted():
    """Full round-trip: connect → init_schema → upsert → re-read → reconnect → re-read."""
    store = ExecutionStore(database_url=INTEGRATION_DB_URL)
    await store.connect()
    await store.init_schema()

    arb_id = f"ARB-{int(time.time()) % 1_000_000:06d}"
    order_id = f"{arb_id}-YES-{int(time.time()) % 99999999:08x}"

    # Need a parent execution_arbs row first (FK)
    async with store._pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO execution_arbs (arb_id, canonical_id, status)
               VALUES ($1, $2, $3)
               ON CONFLICT (arb_id) DO NOTHING""",
            arb_id, "TEST_CANON", "submitted",
        )

    order = Order(
        order_id=order_id,
        platform="kalshi",
        market_id="TEST_MARKET",
        canonical_id="TEST_CANON",
        side="yes",
        price=0.55,
        quantity=10,
        status=OrderStatus.SUBMITTED,
    )
    await store.upsert_order(order, arb_id=arb_id, client_order_id="cid-test")

    fetched = await store.get_order(order_id)
    assert fetched is not None
    assert fetched.order_id == order_id
    assert fetched.status == OrderStatus.SUBMITTED

    # Update to FILLED -- every transition writes
    order.status = OrderStatus.FILLED
    order.fill_price = 0.55
    order.fill_qty = 10
    await store.upsert_order(order, arb_id=arb_id)

    fetched2 = await store.get_order(order_id)
    assert fetched2.status == OrderStatus.FILLED
    assert fetched2.fill_qty == 10

    # Restart simulation: disconnect + reconnect, data persists
    await store.disconnect()
    await store.connect()
    fetched3 = await store.get_order(order_id)
    assert fetched3 is not None
    assert fetched3.status == OrderStatus.FILLED

    # Cleanup
    async with store._pool.acquire() as conn:
        await conn.execute("DELETE FROM execution_orders WHERE order_id = $1", order_id)
        await conn.execute("DELETE FROM execution_arbs WHERE arb_id = $1", arb_id)
    await store.disconnect()


@pytest.mark.skipif(not INTEGRATION_DB_URL, reason="DATABASE_URL not set; skipping integration test")
@pytest.mark.asyncio
async def test_integration_incident_persisted():
    store = ExecutionStore(database_url=INTEGRATION_DB_URL)
    await store.connect()
    await store.init_schema()

    incident = ExecutionIncident(
        incident_id=f"INC-{int(time.time()) % 99999999:08x}",
        arb_id=None,
        canonical_id="TEST_CANON",
        severity="warning",
        message="integration test incident",
        timestamp=time.time(),
        metadata={"key": "value", "n": 42},
    )
    await store.insert_incident(incident)

    async with store._pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT incident_id, severity, metadata FROM execution_incidents WHERE incident_id = $1",
            incident.incident_id,
        )
    assert row is not None
    assert row["severity"] == "warning"
    md = row["metadata"]
    # asyncpg may return JSONB as a parsed dict or a JSON string depending on codec registration.
    if isinstance(md, str):
        md = json.loads(md)
    assert md == {"key": "value", "n": 42}

    async with store._pool.acquire() as conn:
        await conn.execute("DELETE FROM execution_incidents WHERE incident_id = $1", incident.incident_id)
    await store.disconnect()
