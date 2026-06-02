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
    """Records every execute/fetch/fetchrow call. Configurable responses.

    Also tracks `conn.transaction()` usage so tests can verify that multi-write
    operations are wrapped in a single transaction (C4.1).
    """

    def __init__(
        self,
        fetch_response: Any = None,
        fetchrow_response: Any = None,
        fail_on_execute_index: Optional[int] = None,
    ):
        self.calls: List[Tuple[str, str, tuple]] = []  # (method, sql, args)
        self._fetch_response = fetch_response if fetch_response is not None else []
        self._fetchrow_response = fetchrow_response
        # Transaction tracking
        self._txn_depth = 0
        self.transactions_started = 0
        self.transactions_committed = 0
        self.transactions_rolled_back = 0
        self.executes_inside_txn = 0
        # Inject failure on the N-th execute() call (0-indexed). Used to test
        # transaction rollback on intermediate failure.
        self._fail_on_execute_index = fail_on_execute_index
        self._execute_call_index = 0

    async def execute(self, sql: str, *args) -> str:
        idx = self._execute_call_index
        self._execute_call_index += 1
        if self._txn_depth > 0:
            self.executes_inside_txn += 1
        if self._fail_on_execute_index is not None and idx == self._fail_on_execute_index:
            self.calls.append(("execute", sql, args))
            import asyncpg
            raise asyncpg.PostgresError(
                f"simulated execute failure at call index {idx}",
            )
        self.calls.append(("execute", sql, args))
        return "OK"

    async def fetch(self, sql: str, *args) -> List[Any]:
        self.calls.append(("fetch", sql, args))
        return self._fetch_response

    async def fetchrow(self, sql: str, *args) -> Optional[Any]:
        self.calls.append(("fetchrow", sql, args))
        return self._fetchrow_response

    def transaction(self):
        """Mimic asyncpg.Connection.transaction() context manager."""
        conn = self

        class _Tx:
            async def __aenter__(self_tx):
                conn._txn_depth += 1
                conn.transactions_started += 1
                return self_tx

            async def __aexit__(self_tx, exc_type, exc, tb):
                conn._txn_depth -= 1
                if exc_type is None:
                    conn.transactions_committed += 1
                else:
                    conn.transactions_rolled_back += 1
                return False  # don't suppress

        return _Tx()

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


# ─── C4.1: record_arb transactional wrapping ─────────────────────────────


def _build_arb_execution(arb_id: str = "ARB-000099", canonical_id: str = "TEST_TXN"):
    """Build a minimal in-memory ArbExecution for record_arb tests."""
    from arbiter.execution.engine import ArbExecution
    from arbiter.scanner.arbitrage import ArbitrageOpportunity

    opp = ArbitrageOpportunity(
        canonical_id=canonical_id,
        description="d",
        yes_platform="kalshi",
        yes_price=0.50, yes_fee=0.0,
        yes_market_id="K-1",
        no_platform="polymarket",
        no_price=0.50, no_fee=0.0,
        no_market_id="P-1",
        gross_edge=0.0, total_fees=0.0,
        net_edge=0.05, net_edge_cents=5.0,
        suggested_qty=1, max_profit_usd=0.05,
        timestamp=time.time(),
    )
    leg_yes = Order(
        order_id=f"{arb_id}-YES-aaaa",
        platform="kalshi", market_id="K-1", canonical_id=canonical_id,
        side="yes", price=0.50, quantity=1,
        status=OrderStatus.FILLED, fill_price=0.50, fill_qty=1,
    )
    leg_no = Order(
        order_id=f"{arb_id}-NO-bbbb",
        platform="polymarket", market_id="P-1", canonical_id=canonical_id,
        side="no", price=0.50, quantity=1,
        status=OrderStatus.FILLED, fill_price=0.50, fill_qty=1,
    )
    return ArbExecution(
        arb_id=arb_id, opportunity=opp,
        leg_yes=leg_yes, leg_no=leg_no,
        status="filled", realized_pnl=0.05, timestamp=time.time(),
    )


@pytest.mark.asyncio
async def test_record_arb_wraps_all_three_writes_in_single_transaction(mock_pool):
    """C4.1: record_arb + both leg upserts must execute inside a single
    `conn.transaction()` block on the SAME connection. If Postgres bounces
    mid-write the three rows must roll back together — no orphaned arb row
    with no legs, no legs with no parent arb row."""
    store = ExecutionStore(database_url="postgres://mock/mock")
    await store.connect()
    arb = _build_arb_execution()
    await store.record_arb(arb)

    # Exactly one transaction opened.
    assert mock_pool.conn.transactions_started == 1, (
        f"expected exactly 1 transaction; got {mock_pool.conn.transactions_started}"
    )
    assert mock_pool.conn.transactions_committed == 1
    assert mock_pool.conn.transactions_rolled_back == 0
    # All three writes (arb INSERT + 2 leg upserts) inside the transaction.
    assert mock_pool.conn.executes_inside_txn == 3, (
        f"expected 3 executes inside the transaction; "
        f"got {mock_pool.conn.executes_inside_txn}"
    )


@pytest.mark.asyncio
async def test_record_arb_rolls_back_when_leg_upsert_fails(mock_pool, monkeypatch):
    """C4.1: if the SECOND leg upsert raises, the transaction must roll back
    so the execution_arbs INSERT and the first leg upsert are undone
    together. The exception propagates so the engine sees the failure and
    can escalate (C4.2)."""
    import asyncpg

    # Fail on the third execute call (arb INSERT = 0, leg_yes = 1, leg_no = 2)
    mock_pool.conn._fail_on_execute_index = 2

    store = ExecutionStore(database_url="postgres://mock/mock")
    await store.connect()
    arb = _build_arb_execution()

    with pytest.raises(asyncpg.PostgresError):
        await store.record_arb(arb)

    assert mock_pool.conn.transactions_started == 1
    assert mock_pool.conn.transactions_committed == 0
    assert mock_pool.conn.transactions_rolled_back == 1, (
        "transaction must roll back on intermediate failure"
    )


@pytest.mark.asyncio
async def test_record_arb_rolls_back_when_arb_insert_fails(mock_pool):
    """C4.1: if the arb INSERT itself fails (first write), the transaction
    rolls back and neither leg upsert is attempted."""
    import asyncpg

    mock_pool.conn._fail_on_execute_index = 0  # first execute fails

    store = ExecutionStore(database_url="postgres://mock/mock")
    await store.connect()
    arb = _build_arb_execution()

    with pytest.raises(asyncpg.PostgresError):
        await store.record_arb(arb)

    # Transaction opened and rolled back
    assert mock_pool.conn.transactions_started == 1
    assert mock_pool.conn.transactions_rolled_back == 1
    # Only the arb INSERT was attempted; the leg upserts never ran.
    assert mock_pool.conn.executes_inside_txn == 1


# ─── C5.1: record_arb_stub_with_leg atomicity ────────────────────────────


@pytest.mark.asyncio
async def test_record_arb_stub_with_leg_writes_both_in_single_transaction(mock_pool):
    """C5.1: the arb stub INSERT and the first leg's order upsert must execute
    inside a single ``conn.transaction()`` block.  Closes the historic phantom
    window where ``record_arb_stub`` committed alone, then anything between
    that commit and the first leg's ``upsert_order`` could leave an orphan
    arb_row with zero legs (169 of 295 prod phantoms followed that path).
    """
    from arbiter.scanner.arbitrage import ArbitrageOpportunity

    store = ExecutionStore(database_url="postgres://mock/mock")
    await store.connect()
    opp = ArbitrageOpportunity(
        canonical_id="TEST_C5",
        description="d",
        yes_platform="kalshi", yes_price=0.50, yes_fee=0.0, yes_market_id="K-1",
        no_platform="polymarket", no_price=0.50, no_fee=0.0, no_market_id="P-1",
        gross_edge=0.0, total_fees=0.0,
        net_edge=0.05, net_edge_cents=5.0,
        suggested_qty=1, max_profit_usd=0.05,
        timestamp=time.time(),
    )
    leg = Order(
        order_id="ARB-C5-YES-aaaa",
        platform="kalshi", market_id="K-1", canonical_id="TEST_C5",
        side="yes", price=0.50, quantity=1,
        status=OrderStatus.FILLED, fill_price=0.50, fill_qty=1,
    )
    await store.record_arb_stub_with_leg(
        arb_id="ARB-C5",
        canonical_id="TEST_C5",
        first_leg=leg,
        opportunity=opp,
        net_edge=0.05,
    )

    assert mock_pool.conn.transactions_started == 1
    assert mock_pool.conn.transactions_committed == 1
    assert mock_pool.conn.transactions_rolled_back == 0
    # Two writes inside the txn: arb INSERT + leg upsert.
    assert mock_pool.conn.executes_inside_txn == 2, (
        f"expected 2 executes inside the transaction; "
        f"got {mock_pool.conn.executes_inside_txn}"
    )


@pytest.mark.asyncio
async def test_record_arb_stub_with_leg_rolls_back_when_leg_upsert_fails(mock_pool):
    """C5.1: if the leg upsert raises after the arb INSERT, the transaction
    must roll back so the arb stub is undone — no phantom parent row left."""
    import asyncpg
    from arbiter.scanner.arbitrage import ArbitrageOpportunity

    # Fail on the SECOND execute (index 1) — arb INSERT succeeds at index 0,
    # leg upsert fails at index 1, both must roll back.
    mock_pool.conn._fail_on_execute_index = 1

    store = ExecutionStore(database_url="postgres://mock/mock")
    await store.connect()
    opp = ArbitrageOpportunity(
        canonical_id="TEST_RB",
        description="d",
        yes_platform="kalshi", yes_price=0.50, yes_fee=0.0, yes_market_id="K-1",
        no_platform="polymarket", no_price=0.50, no_fee=0.0, no_market_id="P-1",
        gross_edge=0.0, total_fees=0.0,
        net_edge=0.05, net_edge_cents=5.0,
        suggested_qty=1, max_profit_usd=0.05,
        timestamp=time.time(),
    )
    leg = Order(
        order_id="ARB-RB-YES-aaaa",
        platform="kalshi", market_id="K-1", canonical_id="TEST_RB",
        side="yes", price=0.50, quantity=1,
        status=OrderStatus.FILLED, fill_price=0.50, fill_qty=1,
    )

    with pytest.raises(asyncpg.PostgresError):
        await store.record_arb_stub_with_leg(
            arb_id="ARB-RB",
            canonical_id="TEST_RB",
            first_leg=leg,
            opportunity=opp,
        )

    assert mock_pool.conn.transactions_started == 1
    assert mock_pool.conn.transactions_committed == 0
    assert mock_pool.conn.transactions_rolled_back == 1, (
        "leg upsert failure must roll back the arb INSERT too"
    )


# ─── Rehydration ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_load_execution_history_can_load_all_rows_for_reconciliation(mock_pool):
    store = ExecutionStore(database_url="postgres://mock/mock")
    await store.connect()

    result = await store.load_execution_history(limit=None)

    assert result == []
    assert mock_pool.conn.calls, "load_execution_history made no DB call"
    method, sql, args = mock_pool.conn.calls[-1]
    assert method == "fetch"
    assert "LIMIT $1" not in sql
    assert args == ()


@pytest.mark.asyncio
async def test_load_execution_history_defaults_to_bounded_recent_rows(mock_pool):
    store = ExecutionStore(database_url="postgres://mock/mock")
    await store.connect()

    result = await store.load_execution_history()

    assert result == []
    method, sql, args = mock_pool.conn.calls[-1]
    assert method == "fetch"
    assert "LIMIT $1" in sql
    assert args == (200,)


@pytest.mark.asyncio
async def test_resolve_superseded_half_recorded_incidents_marks_duplicate_alerts(mock_pool):
    store = ExecutionStore(database_url="postgres://mock/mock")
    await store.connect()

    updated = await store.resolve_superseded_half_recorded_incidents([
        "ARB-000491",
        "ARB-000492",
    ])

    assert updated == 0
    method, sql, args = mock_pool.conn.calls[-1]
    assert method == "execute"
    assert "metadata->>'event_type' = 'half_recorded_arb'" in sql
    assert "incident_id <> ('INC-HALF-' || arb_id)" in sql
    assert args == (["ARB-000491", "ARB-000492"],)


@pytest.mark.asyncio
async def test_mark_no_exposure_half_recorded_arbs_failed_closes_stubs(mock_pool):
    store = ExecutionStore(database_url="postgres://mock/mock")
    await store.connect()

    updated = await store.mark_no_exposure_half_recorded_arbs_failed([
        "ARB-000700",
        "ARB-000701",
    ])

    assert updated == 0
    method, sql, args = mock_pool.conn.calls[-1]
    assert method == "execute"
    assert "UPDATE execution_arbs" in sql
    assert "status = 'failed'" in sql
    assert "recovery_notes" in sql
    assert "closed_at = COALESCE(closed_at, NOW())" in sql
    assert args == (["ARB-000700", "ARB-000701"],)


@pytest.mark.asyncio
async def test_resolve_half_recorded_incidents_resolves_all_for_arbs(mock_pool):
    store = ExecutionStore(database_url="postgres://mock/mock")
    await store.connect()

    updated = await store.resolve_half_recorded_incidents(
        ["ARB-000700"],
        note="cleared",
    )

    assert updated == 0
    method, sql, args = mock_pool.conn.calls[-1]
    assert method == "execute"
    assert "metadata->>'event_type' = 'half_recorded_arb'" in sql
    assert "status = 'resolved'" in sql
    assert args == (["ARB-000700"], "cleared")


@pytest.mark.asyncio
async def test_resolve_stale_half_recorded_incidents_resolves_non_active_arbs(mock_pool):
    store = ExecutionStore(database_url="postgres://mock/mock")
    await store.connect()

    updated = await store.resolve_stale_half_recorded_incidents([
        "ARB-000701",
        "ARB-000702",
    ])

    assert updated == 0
    method, sql, args = mock_pool.conn.calls[-1]
    assert method == "execute"
    assert "metadata->>'event_type' = 'half_recorded_arb'" in sql
    assert "NOT (arb_id = ANY($1::text[]))" in sql
    assert "status = 'resolved'" in sql
    assert args == (["ARB-000701", "ARB-000702"],)


@pytest.mark.asyncio
async def test_list_half_recorded_arbs_includes_fill_exposure_fields(mock_pool):
    from arbiter.execution.store import NAKED_LEG_RECONCILED_SENTINEL

    store = ExecutionStore(database_url="postgres://mock/mock")
    await store.connect()

    result = await store.list_half_recorded_arbs()

    assert result == []
    method, sql, args = mock_pool.conn.calls[-1]
    assert method == "fetch"
    assert "filled_leg_count" in sql
    assert "filled_notional" in sql
    assert "'closed'" in sql
    # 2026-05-22 audit: query MUST surface asymmetric-fill arbs where one
    # leg filled but the counterpart did not and unwind PnL was never booked
    # (mode 2). Otherwise the ARB-000240..261 class of naked-leg orphans
    # silently bypasses the recovery endpoint.
    assert "unfilled_leg_count" in sql
    # Mode 2 keys off the sentinel string in recovery_notes, NOT off
    # unwind_pnl/realized_pnl (which can be legitimately zero) and NOT off
    # recovery_notes != '' (which is polluted by recovery_check polling).
    assert "POSITION($1" in sql
    assert args == (NAKED_LEG_RECONCILED_SENTINEL,)


@pytest.mark.asyncio
async def test_list_half_recorded_arbs_surfaces_asymmetric_fill(mock_pool):
    """Mode 2: arb status='failed' with one filled + one unfilled leg and
    no booked unwind must still be returned so the operator can reconcile."""
    mock_pool.conn = MockConn(fetch_response=[
        {
            "arb_id": "ARB-000240",
            "canonical_id": "DEM_HOUSE_2026",
            "status": "failed",
            "created_at": None,
            "leg_count": 2,
            "filled_leg_count": 1,
            "unfilled_leg_count": 1,
            "filled_notional": 7.40,
            "leg_order_ids": ["k-1", "p-1"],
        }
    ])
    store = ExecutionStore(database_url="postgres://mock/mock")
    await store.connect()

    result = await store.list_half_recorded_arbs()

    assert len(result) == 1
    assert result[0]["arb_id"] == "ARB-000240"
    assert result[0]["status"] == "failed"
    assert result[0]["filled_leg_count"] == 1
    assert result[0]["unfilled_leg_count"] == 1
    assert result[0]["filled_notional"] == 7.40


@pytest.mark.asyncio
async def test_mark_naked_leg_reconciled_writes_sentinel(mock_pool):
    """``mark_naked_leg_reconciled`` must (a) update unwind_pnl, (b) embed
    the ``[naked-leg-reconciled]`` sentinel in recovery_notes, and (c) be
    idempotent — re-applying does not duplicate the sentinel.
    """
    from arbiter.execution.store import NAKED_LEG_RECONCILED_SENTINEL

    store = ExecutionStore(database_url="postgres://mock/mock")
    await store.connect()

    await store.mark_naked_leg_reconciled(
        "ARB-000240",
        unwind_pnl=-4.70,
        note="Backfill (2026-05-22): naked kalshi yes leg auto-unwound",
    )

    method, sql, args = mock_pool.conn.calls[-1]
    assert method == "execute"
    assert "UPDATE execution_arbs" in sql
    assert "unwind_pnl = $2" in sql
    assert "THEN 'closed'" in sql
    assert "closed_at = CASE" in sql
    assert "status IN ('pending', 'submitted', 'partial')" in sql
    # Sentinel must be a query parameter, not interpolated, so we don't
    # rot the string if the constant is renamed in the future.
    assert NAKED_LEG_RECONCILED_SENTINEL in args
    assert args[0] == "ARB-000240"
    # POSITION-based idempotency: if sentinel already present, leave notes
    # untouched (no double-write).
    assert "POSITION($3 IN recovery_notes) > 0" in sql


@pytest.mark.asyncio
async def test_list_incidents_reads_persisted_open_incidents(mock_pool):
    mock_pool.conn = MockConn(fetch_response=[
        {
            "incident_id": "INC-HALF-ARB-000491",
            "arb_id": "ARB-000491",
            "canonical_id": "MKT_NAKED",
            "severity": "critical",
            "message": "Half-recorded arb",
            "metadata": {"event_type": "half_recorded_arb"},
            "status": "open",
            "ts": 123.0,
            "resolved_ts": None,
            "resolution_note": "",
        }
    ])
    store = ExecutionStore(database_url="postgres://mock/mock")
    await store.connect()

    incidents = await store.list_incidents(status="open", limit=50)

    assert len(incidents) == 1
    assert incidents[0].incident_id == "INC-HALF-ARB-000491"
    assert incidents[0].severity == "critical"
    method, sql, args = mock_pool.conn.calls[-1]
    assert method == "fetch"
    assert "FROM execution_incidents" in sql
    assert args == ("open", 50)


# ─── C4.3: UNIQUE(client_order_id) migration shape ────────────────────────


def test_migration_005_replaces_nonunique_index_with_unique_partial():
    """C4.3: migration 005 drops the non-unique partial index on
    execution_orders.client_order_id (added by 001) and replaces it with
    a UNIQUE partial index that excludes NULLs. NULL exclusion is
    mandatory because Polymarket orders have no client_order_id and would
    otherwise collide on a single shared NULL row constraint."""
    from pathlib import Path
    migration_dir = (
        Path(__file__).resolve().parent.parent / "sql" / "migrations"
    )
    migration_path = migration_dir / "005_unique_client_order_id.sql"
    assert migration_path.exists(), (
        f"missing migration: {migration_path}"
    )
    sql = migration_path.read_text()
    # drops the prior non-unique index
    assert "DROP INDEX IF EXISTS idx_execution_orders_client_id" in sql
    # adds a UNIQUE partial index
    assert "CREATE UNIQUE INDEX" in sql
    assert "client_order_id" in sql
    assert "WHERE client_order_id IS NOT NULL" in sql


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
    assert "order_id NOT LIKE '%-SKIPPED'" in sql
    assert "order_id NOT LIKE '%-EDGE-LOST'" in sql
    assert "order_id NOT LIKE '%-UNPROFITABLE'" in sql
    assert "order_id NOT LIKE '%-EXCEPTION'" in sql
    assert "order_id NOT LIKE '%-NOADAPTER'" in sql
    assert "order_id NOT LIKE '%-DBFAIL'" in sql


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
