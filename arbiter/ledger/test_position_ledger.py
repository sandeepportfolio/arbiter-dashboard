"""
Tests for PositionLedger.
Uses pytest with an in-memory SQLite-like approach via asyncpg's mock,
or real Postgres if DATABASE_URL is set.
"""
import asyncio
import os
import sys
from datetime import datetime, timezone

import pytest

# Add package to path for standalone runs
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from arbiter.ledger.position_ledger import (
    HedgeStatus,
    PositionLedger,
    PositionStatus,
)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


# ─── Mock Postgres for unit testing ──────────────────────────────────────────

class MockRecord:
    def __init__(self, data: dict):
        self._data = data

    def __getitem__(self, key):
        return self._data.get(key)

    def keys(self):
        return self._data.keys()


class MockConn:
    def __init__(self):
        self._positions = {}
        self._events = []
        self._id_counter = 1

    async def execute(self, query: str, *args):
        # Basic mock handling for init schema and inserts
        if "CREATE TABLE" in query:
            return None
        if "INSERT INTO positions" in query:
            # args: position_id(1), canonical_id(2), description(3),
            #       yes_platform(4), no_platform(5), yes_market_id(6), no_market_id(7),
            #       quantity(8), yes_price(9), no_price(10), yes_order_id(11), no_order_id(12),
            #       yes_fill_price(13), no_fill_price(14), fees_paid(15), is_simulation(16), now(17)
            pos = {
                "position_id": args[0],
                "canonical_id": args[1],
                "description": args[2],
                "yes_platform": args[3],
                "no_platform": args[4],
                "yes_market_id": args[5],
                "no_market_id": args[6],
                "quantity": int(args[7]),
                "yes_price": args[8],
                "no_price": args[9],
                "yes_order_id": args[10],
                "no_order_id": args[11],
                "yes_fill_price": args[12],
                "no_fill_price": args[13],
                "fees_paid": args[14],
                "is_simulation": args[15],
                "status": "open",
                "hedge_status": "none",
                "hedge_order_id": "",
                "realized_pnl": 0,
                "settlement_price": 0,
                "settlement_pnl": 0,
                "unwind_reason": "",
                "notes": [],
                "created_at": args[16],
                "entry_confirmed_at": args[16],
            }
            self._positions[args[0]] = pos
            return "INSERT 0 1"
        if "INSERT INTO position_events" in query:
            # args: position_id, event_type, metadata (dict)
            self._events.append({"position_id": args[0], "event_type": args[1], "metadata": args[2] if len(args) > 2 else {}})
            return "INSERT 0 1"
        if "UPDATE positions" in query and "hedge_status" in query.lower():
            pos_id = args[0]
            if pos_id in self._positions:
                self._positions[pos_id]["hedge_status"] = "complete"
                self._positions[pos_id]["hedge_order_id"] = args[1]
                self._positions[pos_id]["no_fill_price"] = args[2]
                self._positions[pos_id]["fees_paid"] = float(self._positions[pos_id]["fees_paid"]) + float(args[3])
                self._positions[pos_id]["status"] = "hedged"
            return "UPDATE 1"
        if "UPDATE positions" in query and "status = 'closed'" in query:
            pos_id = args[0]
            if pos_id in self._positions:
                self._positions[pos_id]["status"] = "closed"
                self._positions[pos_id]["closed_at"] = args[1]
            return "UPDATE 1"
        if "UPDATE positions" in query and "status = 'settled'" in query:
            pos_id = args[0]
            if pos_id in self._positions:
                self._positions[pos_id]["status"] = "settled"
                self._positions[pos_id]["settlement_price"] = args[1]
                self._positions[pos_id]["settlement_pnl"] = args[2]
                self._positions[pos_id]["settled_at"] = args[3]
                self._positions[pos_id]["realized_pnl"] = float(self._positions[pos_id]["realized_pnl"]) + float(args[2])
            return "UPDATE 1"
        if "UPDATE positions" in query and "status = 'unwind'" in query:
            pos_id = args[0]
            if pos_id in self._positions:
                self._positions[pos_id]["status"] = "unwind"
                self._positions[pos_id]["unwind_reason"] = args[1]
                self._positions[pos_id]["realized_pnl"] = float(self._positions[pos_id]["realized_pnl"]) + float(args[2])
                self._positions[pos_id]["closed_at"] = args[3]
            return "UPDATE 1"
        return None

    async def fetchrow(self, query: str, *args):
        if "UPDATE positions" in query and "RETURNING *" in query:
            # Handle UPDATE ... RETURNING * — apply changes first
            pos_id = args[0]
            if pos_id not in self._positions:
                return None
            p = self._positions[pos_id]
            _q = query.lower()
            if "hedge_status" in _q and "'complete'" in _q:
                p["hedge_status"] = "complete"
                p["hedge_order_id"] = str(args[1])
                p["no_fill_price"] = float(args[2])
                p["fees_paid"] = float(p["fees_paid"]) + float(args[3])
                p["status"] = "hedged"
            elif "status = 'settled'" in query:
                p["status"] = "settled"
                p["settlement_price"] = float(args[1])
                p["settlement_pnl"] = float(args[2])
                p["settled_at"] = args[3]
                p["realized_pnl"] = float(p["realized_pnl"]) + float(args[2])
            elif "status = 'unwind'" in query:
                p["status"] = "unwind"
                p["unwind_reason"] = str(args[1])
                p["realized_pnl"] = float(p["realized_pnl"]) + float(args[2])
                p["closed_at"] = args[3]
            return MockRecord(p)
        if "SELECT * FROM positions WHERE position_id" in query:
            pos_id = args[0]
            if pos_id in self._positions:
                return MockRecord(self._positions[pos_id])
            return None
        if "SELECT" in query and "COALESCE(SUM" in query:
            # Total exposure query
            total = sum(
                float(p["yes_price"]) * p["quantity"] + float(p["no_price"]) * p["quantity"]
                for p in self._positions.values()
                if p["status"] in ("open", "hedged")
            )
            return {"total": total}
        if "GROUP BY status" in query:
            status_counts = {}
            for p in self._positions.values():
                s = p["status"]
                status_counts[s] = status_counts.get(s, 0) + 1
            return [
                MockRecord({"status": s, "count": c, "pnl": 0, "exposure": 0})
                for s, c in status_counts.items()
            ]
        return None

    async def fetch(self, query: str, *args):
        if "SELECT * FROM positions WHERE status IN" in query:
            # args[0] = canonical_id if filter is present
            canon_filter = args[0] if args else None
            return [
                MockRecord(p) for p in self._positions.values()
                if p["status"] in ("open", "hedged")
                and (canon_filter is None or p["canonical_id"] == canon_filter)
            ]
        if "SELECT * FROM positions WHERE position_id" in query:
            pos_id = args[0]
            if pos_id in self._positions:
                return [MockRecord(self._positions[pos_id])]
            return []
        if "SELECT * FROM position_events" in query:
            return [MockRecord(e) for e in self._events if e["position_id"] == args[0]]
        return []


class MockPool:
    def __init__(self):
        self._conn = MockConn()

    async def acquire(self):
        return self._conn

    async def release(self, conn):
        pass

    async def close(self):
        pass


# ─── Fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture
def mock_pool(monkeypatch):
    pool = MockPool()
    # Patch connect to skip real connection
    async def fake_connect(self):
        self._pool = pool
        logger = __import__("logging").getLogger("arbiter.ledger")
        logger.info("Using mock Postgres pool")
    monkeypatch.setattr(PositionLedger, "connect", fake_connect)
    return pool


# ─── Tests ───────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_open_position(mock_pool, monkeypatch):
    ledger = PositionLedger(database_url="postgres://mock/mock", is_simulation=True)
    await ledger.connect()

    pos = await ledger.open_position(
        canonical_id="TEST-EVENT-yes",
        description="Test trade",
        yes_platform="kalshi",
        no_platform="polymarket",
        yes_market_id="kalshi-123",
        no_market_id="poly-abc",
        quantity=10,
        yes_price=0.55,
        no_price=0.48,
        yes_order_id="K-ORD-1",
        no_order_id="P-ORD-1",
        yes_fill_price=0.55,
        no_fill_price=0.48,
        fees_paid=0.50,
    )

    assert pos.position_id.startswith("POS-")
    assert pos.canonical_id == "TEST-EVENT-yes"
    assert pos.quantity == 10
    assert pos.status == PositionStatus.OPEN
    assert pos.yes_fill_price == 0.55
    assert pos.no_fill_price == 0.48
    assert pos.fees_paid == 0.50
    assert pos.is_simulation is True
    await ledger.disconnect()


@pytest.mark.asyncio
async def test_mark_hedged(mock_pool, monkeypatch):
    ledger = PositionLedger(database_url="postgres://mock/mock", is_simulation=True)
    await ledger.connect()

    pos = await ledger.open_position(
        canonical_id="TEST-EVENT-yes",
        description="Hedge test",
        yes_platform="kalshi",
        no_platform="polymarket",
        yes_market_id="kalshi-123",
        no_market_id="poly-abc",
        quantity=5,
        yes_price=0.60,
        no_price=0.42,
        fees_paid=0.10,
    )

    pos_id = pos.position_id

    # Override: manually insert so we have a real position_id
    mock_pool._conn._positions[pos_id] = {
        "position_id": pos_id,
        "canonical_id": "TEST-EVENT-yes",
        "description": "Hedge test",
        "yes_platform": "kalshi",
        "no_platform": "polymarket",
        "yes_market_id": "kalshi-123",
        "no_market_id": "poly-abc",
        "quantity": 5,
        "yes_price": 0.60,
        "no_price": 0.42,
        "yes_order_id": "K-ORD-1",
        "no_order_id": "P-ORD-1",
        "yes_fill_price": 0.60,
        "no_fill_price": 0.42,
        "fees_paid": 0.10,
        "is_simulation": True,
        "status": "open",
        "hedge_status": "none",
        "hedge_order_id": "",
        "realized_pnl": 0,
        "settlement_price": 0,
        "settlement_pnl": 0,
        "unwind_reason": "",
        "notes": [],
        "created_at": utc_now(),
        "entry_confirmed_at": utc_now(),
    }

    updated = await ledger.mark_hedged(
        position_id=pos_id,
        hedge_order_id="HEDGE-ORD-1",
        hedge_fill_price=0.43,
        fees_paid=0.05,
    )

    assert updated is not None
    # Verify via mock
    stored = mock_pool._conn._positions[pos_id]
    assert stored["hedge_status"] == "complete"
    assert stored["status"] == "hedged"
    assert float(stored["no_fill_price"]) == 0.43

    await ledger.disconnect()


@pytest.mark.asyncio
async def test_get_open_positions(mock_pool, monkeypatch):
    ledger = PositionLedger(database_url="postgres://mock/mock", is_simulation=True)
    await ledger.connect()

    # Insert two positions
    for i in range(2):
        pos_id = f"POS-{i:03d}"
        mock_pool._conn._positions[pos_id] = {
            "position_id": pos_id,
            "canonical_id": f"CANON-{i}",
            "description": f"Test {i}",
            "yes_platform": "kalshi",
            "no_platform": "polymarket",
            "yes_market_id": f"k-{i}",
            "no_market_id": f"p-{i}",
            "quantity": 5,
            "yes_price": 0.50,
            "no_price": 0.50,
            "yes_order_id": f"K-{i}",
            "no_order_id": f"P-{i}",
            "yes_fill_price": 0.50,
            "no_fill_price": 0.50,
            "fees_paid": 0.10,
            "is_simulation": True,
            "status": "open",
            "hedge_status": "none",
            "hedge_order_id": "",
            "realized_pnl": 0,
            "settlement_price": 0,
            "settlement_pnl": 0,
            "unwind_reason": "",
            "notes": [],
            "created_at": utc_now(),
            "entry_confirmed_at": utc_now(),
        }

    positions = await ledger.get_open_positions()
    assert len(positions) == 2

    # Filter by canonical_id
    positions = await ledger.get_open_positions(canonical_id="CANON-0")
    assert len(positions) == 1
    assert positions[0].canonical_id == "CANON-0"

    await ledger.disconnect()


@pytest.mark.asyncio
async def test_settle_position(mock_pool, monkeypatch):
    ledger = PositionLedger(database_url="postgres://mock/mock", is_simulation=True)
    await ledger.connect()

    pos_id = "POS-SETTLE"
    mock_pool._conn._positions[pos_id] = {
        "position_id": pos_id,
        "canonical_id": "TEST-SETTLE",
        "description": "Settlement test",
        "yes_platform": "kalshi",
        "no_platform": "polymarket",
        "yes_market_id": "k-1",
        "no_market_id": "p-1",
        "quantity": 10,
        "yes_price": 0.55,
        "no_price": 0.45,
        "yes_order_id": "K-1",
        "no_order_id": "P-1",
        "yes_fill_price": 0.55,
        "no_fill_price": 0.45,
        "fees_paid": 0.50,
        "is_simulation": True,
        "status": "closed",
        "hedge_status": "complete",
        "hedge_order_id": "H-1",
        "realized_pnl": 0,
        "settlement_price": 0,
        "settlement_pnl": 0,
        "unwind_reason": "",
        "notes": [],
        "created_at": utc_now(),
        "entry_confirmed_at": utc_now(),
    }

    settled = await ledger.settle_position(
        position_id=pos_id,
        settlement_price=0.52,
        settlement_pnl=1.25,
    )

    assert settled is not None
    stored = mock_pool._conn._positions[pos_id]
    assert stored["status"] == "settled"
    assert float(stored["settlement_price"]) == 0.52
    assert float(stored["settlement_pnl"]) == 1.25
    assert float(stored["realized_pnl"]) == 1.25

    await ledger.disconnect()


@pytest.mark.asyncio
async def test_unwind_position(mock_pool, monkeypatch):
    ledger = PositionLedger(database_url="postgres://mock/mock", is_simulation=True)
    await ledger.connect()

    pos_id = "POS-UNWIND"
    mock_pool._conn._positions[pos_id] = {
        "position_id": pos_id,
        "canonical_id": "TEST-UNWIND",
        "description": "Unwind test",
        "yes_platform": "kalshi",
        "no_platform": "polymarket",
        "yes_market_id": "k-1",
        "no_market_id": "p-1",
        "quantity": 8,
        "yes_price": 0.60,
        "no_price": 0.42,
        "yes_order_id": "K-1",
        "no_order_id": "P-1",
        "yes_fill_price": 0.60,
        "no_fill_price": 0.42,
        "fees_paid": 0.30,
        "is_simulation": True,
        "status": "open",
        "hedge_status": "none",
        "hedge_order_id": "",
        "realized_pnl": 0,
        "settlement_price": 0,
        "settlement_pnl": 0,
        "unwind_reason": "",
        "notes": [],
        "created_at": utc_now(),
        "entry_confirmed_at": utc_now(),
    }

    unwound = await ledger.unwind_position(
        position_id=pos_id,
        reason="one-leg-fill-failure",
        unwind_pnl=-0.85,
    )

    assert unwound is not None
    stored = mock_pool._conn._positions[pos_id]
    assert stored["status"] == "unwind"
    assert stored["unwind_reason"] == "one-leg-fill-failure"
    assert float(stored["realized_pnl"]) == -0.85

    await ledger.disconnect()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
