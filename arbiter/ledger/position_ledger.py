"""
Durable position ledger backed by Postgres.
Replaces in-memory _open_positions tracking in RiskManager.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

import asyncpg

logger = logging.getLogger("arbiter.ledger")


class PositionStatus(str, Enum):
    OPEN = "open"
    HEDGED = "hedged"
    CLOSED = "closed"
    SETTLED = "settled"
    UNWIND = "unwind"
    CANCELLED = "cancelled"


class HedgeStatus(str, Enum):
    NONE = "none"
    PENDING = "pending"
    COMPLETE = "complete"
    FAILED = "failed"


@dataclass
class Position:
    position_id: str
    canonical_id: str
    description: str
    yes_platform: str
    no_platform: str
    yes_market_id: str
    no_market_id: str
    quantity: int
    yes_price: float
    no_price: float
    status: PositionStatus
    hedge_status: HedgeStatus = HedgeStatus.NONE
    hedge_order_id: str = ""
    yes_order_id: str = ""
    no_order_id: str = ""
    yes_fill_price: float = 0.0
    no_fill_price: float = 0.0
    realized_pnl: float = 0.0
    settlement_price: float = 0.0
    settlement_pnl: float = 0.0
    fees_paid: float = 0.0
    is_simulation: bool = True
    created_at: datetime = field(default_factory=datetime.utcnow)
    entry_confirmed_at: datetime = field(default_factory=datetime.utcnow)
    closed_at: Optional[datetime] = None
    settled_at: Optional[datetime] = None
    unwind_reason: str = ""
    notes: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "position_id": self.position_id,
            "canonical_id": self.canonical_id,
            "description": self.description,
            "yes_platform": self.yes_platform,
            "no_platform": self.no_platform,
            "yes_market_id": self.yes_market_id,
            "no_market_id": self.no_market_id,
            "quantity": self.quantity,
            "yes_price": round(self.yes_price, 4),
            "no_price": round(self.no_price, 4),
            "status": self.status.value,
            "hedge_status": self.hedge_status.value,
            "hedge_order_id": self.hedge_order_id,
            "yes_order_id": self.yes_order_id,
            "no_order_id": self.no_order_id,
            "yes_fill_price": round(self.yes_fill_price, 4),
            "no_fill_price": round(self.no_fill_price, 4),
            "realized_pnl": round(self.realized_pnl, 4),
            "settlement_price": round(self.settlement_price, 4),
            "settlement_pnl": round(self.settlement_pnl, 4),
            "fees_paid": round(self.fees_paid, 4),
            "is_simulation": self.is_simulation,
            "created_at": self.created_at.isoformat(),
            "entry_confirmed_at": self.entry_confirmed_at.isoformat(),
            "closed_at": self.closed_at.isoformat() if self.closed_at else None,
            "settled_at": self.settled_at.isoformat() if self.settled_at else None,
            "unwind_reason": self.unwind_reason,
            "notes": list(self.notes),
        }


@dataclass
class UnrealizedPnL:
    canonical_id: str
    quantity: int
    cost: float
    current_yes_bid: float
    current_no_bid: float
    yes_liquidation: float
    no_liquidation: float
    unrealized: float
    hedge_needed: bool


@dataclass
class PositionSummary:
    total_open: int
    total_hedged: int
    total_closed: int
    total_realized_pnl: float
    total_unrealized_pnl: float
    total_exposure: float
    by_platform: Dict[str, int]
    by_canonical: Dict[str, int]


class PositionLedger:
    """
    Postgres-backed durable position ledger.
    Tracks all positions from open through hedge → close → settle → unwind.
    """

    _pool: Optional[asyncpg.Pool] = None

    def __init__(self, database_url: str, is_simulation: bool = True):
        self.database_url = database_url
        self.is_simulation = is_simulation

    # ─── Connection Management ───────────────────────────────────────────────

    async def connect(self) -> None:
        if self._pool is None:
            self._pool = await asyncpg.create_pool(
                self.database_url,
                min_size=2,
                max_size=10,
                command_timeout=30,
            )
            logger.info("PositionLedger: connected to Postgres")

    async def disconnect(self) -> None:
        if self._pool:
            await self._pool.close()
            self._pool = None
            logger.info("PositionLedger: disconnected from Postgres")

    async def acquire(self) -> asyncpg.Connection:
        if self._pool is None:
            await self.connect()
        return await self._pool.acquire()

    # ─── Init ─────────────────────────────────────────────────────────────────

    SQL_INIT = """
    CREATE TABLE IF NOT EXISTS positions (
        position_id      VARCHAR(40) PRIMARY KEY,
        canonical_id     VARCHAR(60) NOT NULL,
        description      TEXT,
        yes_platform     VARCHAR(20) NOT NULL,
        no_platform      VARCHAR(20) NOT NULL,
        yes_market_id    VARCHAR(100),
        no_market_id     VARCHAR(100),
        quantity         INT NOT NULL,
        yes_price        DECIMAL(8,4) NOT NULL,
        no_price         DECIMAL(8,4) NOT NULL,
        status           VARCHAR(20) NOT NULL DEFAULT 'open',
        hedge_status     VARCHAR(20) NOT NULL DEFAULT 'none',
        hedge_order_id   VARCHAR(100) DEFAULT '',
        yes_order_id     VARCHAR(100) DEFAULT '',
        no_order_id      VARCHAR(100) DEFAULT '',
        yes_fill_price   DECIMAL(8,4) DEFAULT 0,
        no_fill_price    DECIMAL(8,4) DEFAULT 0,
        realized_pnl     DECIMAL(10,4) DEFAULT 0,
        settlement_price  DECIMAL(8,4) DEFAULT 0,
        settlement_pnl   DECIMAL(10,4) DEFAULT 0,
        fees_paid        DECIMAL(10,4) DEFAULT 0,
        is_simulation    BOOLEAN DEFAULT TRUE,
        unwind_reason    TEXT DEFAULT '',
        notes            TEXT[] DEFAULT '{}',
        created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        entry_confirmed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        closed_at        TIMESTAMPTZ,
        settled_at       TIMESTAMPTZ,
        UNIQUE(position_id)
    );

    CREATE INDEX IF NOT EXISTS idx_positions_canonical ON positions(canonical_id);
    CREATE INDEX IF NOT EXISTS idx_positions_status ON positions(status);
    CREATE INDEX IF NOT EXISTS idx_positions_created ON positions(created_at);

    CREATE TABLE IF NOT EXISTS position_events (
        id               SERIAL PRIMARY KEY,
        position_id      VARCHAR(40) NOT NULL REFERENCES positions(position_id),
        event_type       VARCHAR(30) NOT NULL,
        delta_pnl        DECIMAL(10,4) DEFAULT 0,
        delta_fees       DECIMAL(10,4) DEFAULT 0,
        metadata         JSONB DEFAULT '{}',
        created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );

    CREATE INDEX IF NOT EXISTS idx_position_events_position ON position_events(position_id);
    """

    async def init_schema(self) -> None:
        """Run once to create tables."""
        conn = await self.acquire()
        try:
            await conn.execute(self.SQL_INIT)
            logger.info("PositionLedger schema initialized")
        finally:
            await self._pool.release(conn)

    # ─── CRUD ─────────────────────────────────────────────────────────────────

    async def open_position(
        self,
        canonical_id: str,
        description: str,
        yes_platform: str,
        no_platform: str,
        yes_market_id: str,
        no_market_id: str,
        quantity: int,
        yes_price: float,
        no_price: float,
        yes_order_id: str = "",
        no_order_id: str = "",
        yes_fill_price: float = 0.0,
        no_fill_price: float = 0.0,
        fees_paid: float = 0.0,
        is_simulation: bool = True,
    ) -> Position:
        """Record a new open position after both legs have been filled."""
        position_id = f"POS-{uuid.uuid4().hex[:12].upper()}"
        now = datetime.utcnow()

        conn = await self.acquire()
        try:
            await conn.execute(
                """
                INSERT INTO positions (
                    position_id, canonical_id, description,
                    yes_platform, no_platform, yes_market_id, no_market_id,
                    quantity, yes_price, no_price,
                    yes_order_id, no_order_id, yes_fill_price, no_fill_price,
                    fees_paid, is_simulation, status,
                    entry_confirmed_at
                ) VALUES (
                    $1, $2, $3, $4, $5, $6, $7, $8, $9, $10,
                    $11, $12, $13, $14, $15, $16, 'open', $17
                )
                """,
                position_id, canonical_id, description,
                yes_platform, no_platform, yes_market_id, no_market_id,
                quantity, Decimal(str(yes_price)), Decimal(str(no_price)),
                yes_order_id, no_order_id,
                Decimal(str(yes_fill_price or yes_price)),
                Decimal(str(no_fill_price or no_price)),
                Decimal(str(fees_paid)), is_simulation, now,
            )

            await conn.execute(
                """
                INSERT INTO position_events (position_id, event_type, metadata)
                VALUES ($1, 'opened', $2)
                """,
                position_id, {"canonical_id": canonical_id, "quantity": quantity},
            )

            position = Position(
                position_id=position_id,
                canonical_id=canonical_id,
                description=description,
                yes_platform=yes_platform,
                no_platform=no_platform,
                yes_market_id=yes_market_id,
                no_market_id=no_market_id,
                quantity=quantity,
                yes_price=yes_price,
                no_price=no_price,
                yes_fill_price=yes_fill_price or yes_price,
                no_fill_price=no_fill_price or no_price,
                fees_paid=fees_paid,
                is_simulation=is_simulation,
                status=PositionStatus.OPEN,
                created_at=now,
                entry_confirmed_at=now,
            )

            logger.info(
                f"[Ledger] Opened position {position_id} on {canonical_id}, "
                f"qty={quantity}, cost=${yes_price * quantity:.2f} + ${no_price * quantity:.2f}"
            )
            return position

        finally:
            await self._pool.release(conn)

    async def mark_hedged(
        self,
        position_id: str,
        hedge_order_id: str,
        hedge_fill_price: float,
        fees_paid: float = 0.0,
    ) -> Position:
        """Record that the hedge leg has been filled."""
        conn = await self.acquire()
        try:
            row = await conn.fetchrow(
                """
                UPDATE positions
                SET hedge_status = 'complete',
                    hedge_order_id = $2,
                    no_fill_price = $3,
                    fees_paid = fees_paid + $4,
                    status = 'hedged'
                WHERE position_id = $1
                RETURNING *
                """,
                position_id, hedge_order_id, Decimal(str(hedge_fill_price)), Decimal(str(fees_paid)),
            )

            await conn.execute(
                """
                INSERT INTO position_events (position_id, event_type, delta_fees, metadata)
                VALUES ($1, 'hedged', $2, $3)
                """,
                position_id, Decimal(str(fees_paid)),
                {"hedge_order_id": hedge_order_id, "hedge_fill_price": hedge_fill_price},
            )

            return self._row_to_position(row)

        finally:
            await self._pool.release(conn)

    async def close_position(
        self,
        position_id: str,
        reason: str = "manual",
        notes: str = "",
    ) -> Position:
        """Close an open/hedged position."""
        now = datetime.utcnow()
        conn = await self.acquire()
        try:
            row = await conn.fetchrow(
                """
                UPDATE positions
                SET status = 'closed',
                    closed_at = $2,
                    notes = array_append(notes, $3)
                WHERE position_id = $1
                RETURNING *
                """,
                position_id, now, f"close:{reason}",
            )

            if row:
                await conn.execute(
                    """
                    INSERT INTO position_events (position_id, event_type, metadata)
                    VALUES ($1, 'closed', $2)
                    """,
                    position_id, {"reason": reason, "notes": notes},
                )
                logger.info(f"[Ledger] Closed position {position_id}: {reason}")

            return self._row_to_position(row) if row else None

        finally:
            await self._pool.release(conn)

    async def settle_position(
        self,
        position_id: str,
        settlement_price: float,
        settlement_pnl: float,
    ) -> Position:
        """Record market resolution and final P&L."""
        now = datetime.utcnow()
        conn = await self.acquire()
        try:
            row = await conn.fetchrow(
                """
                UPDATE positions
                SET status = 'settled',
                    settlement_price = $2,
                    settlement_pnl = $3,
                    settled_at = $4,
                    realized_pnl = realized_pnl + $3
                WHERE position_id = $1
                RETURNING *
                """,
                position_id, Decimal(str(settlement_price)), Decimal(str(settlement_pnl)), now,
            )

            await conn.execute(
                """
                INSERT INTO position_events (position_id, event_type, delta_pnl, metadata)
                VALUES ($1, 'settled', $2, $3)
                """,
                position_id, Decimal(str(settlement_pnl)),
                {"settlement_price": settlement_price},
            )

            if row:
                logger.info(
                    f"[Ledger] Settled position {position_id}: "
                    f"price={settlement_price:.4f}, pnl={settlement_pnl:.4f}"
                )

            return self._row_to_position(row) if row else None

        finally:
            await self._pool.release(conn)

    async def unwind_position(
        self,
        position_id: str,
        reason: str,
        unwind_pnl: float = 0.0,
    ) -> Position:
        """Record an unwind event (one-leg recovery, emergency exit)."""
        now = datetime.utcnow()
        conn = await self.acquire()
        try:
            row = await conn.fetchrow(
                """
                UPDATE positions
                SET status = 'unwind',
                    unwind_reason = $2,
                    realized_pnl = realized_pnl + $3,
                    closed_at = $4
                WHERE position_id = $1
                RETURNING *
                """,
                position_id, reason, Decimal(str(unwind_pnl)), now,
            )

            await conn.execute(
                """
                INSERT INTO position_events (position_id, event_type, delta_pnl, metadata)
                VALUES ($1, 'unwound', $2, $3)
                """,
                position_id, Decimal(str(unwind_pnl)),
                {"reason": reason},
            )

            if row:
                logger.warning(f"[Ledger] Unwound position {position_id}: {reason}, pnl={unwind_pnl:.4f}")

            return self._row_to_position(row) if row else None

        finally:
            await self._pool.release(conn)

    # ─── Queries ──────────────────────────────────────────────────────────────

    async def get_open_positions(
        self,
        canonical_id: Optional[str] = None,
        platform: Optional[str] = None,
    ) -> List[Position]:
        """Return all currently open (non-closed, non-settled) positions."""
        conn = await self.acquire()
        try:
            query = "SELECT * FROM positions WHERE status IN ('open', 'hedged')"
            params = []
            if canonical_id:
                query += " AND canonical_id = $" + str(len(params) + 1)
                params.append(canonical_id)
            if platform:
                query += f" AND (yes_platform = ${len(params)+1} OR no_platform = ${len(params)+1})"
                params.append(platform)
            query += " ORDER BY created_at DESC"

            rows = await conn.fetch(query, *params)
            return [self._row_to_position(r) for r in rows]

        finally:
            await self._pool.release(conn)

    async def get_total_exposure(self) -> float:
        """Sum of cost basis for all open positions."""
        conn = await self.acquire()
        try:
            row = await conn.fetchrow(
                """
                SELECT COALESCE(SUM((yes_price + no_price) * quantity), 0) as total
                FROM positions
                WHERE status IN ('open', 'hedged')
                """
            )
            return float(row["total"]) if row else 0.0

        finally:
            await self._pool.release(conn)

    async def get_position_summary(self) -> PositionSummary:
        """Aggregate view of all positions."""
        conn = await self.acquire()
        try:
            status_rows = await conn.fetch(
                """
                SELECT status, COUNT(*) as count,
                       COALESCE(SUM(realized_pnl), 0) as pnl,
                       COALESCE(SUM((yes_price + no_price) * quantity), 0) as exposure
                FROM positions
                GROUP BY status
                """
            )

            platform_rows = await conn.fetch(
                """
                SELECT yes_platform as platform, COUNT(*) as count
                FROM positions WHERE status IN ('open', 'hedged') GROUP BY yes_platform
                UNION ALL
                SELECT no_platform as platform, COUNT(*) as count
                FROM positions WHERE status IN ('open', 'hedged') GROUP BY no_platform
                """
            )

            canonical_rows = await conn.fetch(
                """
                SELECT canonical_id, COUNT(*) as count
                FROM positions WHERE status IN ('open', 'hedged')
                GROUP BY canonical_id
                """
            )

            total_realized = 0.0
            total_exposure = 0.0
            by_status = {}
            for row in status_rows:
                status = row["status"]
                by_status[status] = row["count"]
                total_realized += float(row["pnl"])
                if status in ("open", "hedged"):
                    total_exposure += float(row["exposure"])

            by_platform = {}
            for row in platform_rows:
                p = row["platform"]
                by_platform[p] = by_platform.get(p, 0) + row["count"]

            by_canonical = {r["canonical_id"]: r["count"] for r in canonical_rows}

            return PositionSummary(
                total_open=by_status.get("open", 0),
                total_hedged=by_status.get("hedged", 0),
                total_closed=by_status.get("closed", 0) + by_status.get("settled", 0),
                total_realized_pnl=total_realized,
                total_unrealized_pnl=0.0,  # computed live from price store
                total_exposure=total_exposure,
                by_platform=by_platform,
                by_canonical=by_canonical,
            )

        finally:
            await self._pool.release(conn)

    async def get_position(self, position_id: str) -> Optional[Position]:
        conn = await self.acquire()
        try:
            row = await conn.fetchrow(
                "SELECT * FROM positions WHERE position_id = $1", position_id
            )
            return self._row_to_position(row) if row else None

        finally:
            await self._pool.release(conn)

    async def get_position_events(self, position_id: str) -> List[dict]:
        conn = await self.acquire()
        try:
            rows = await conn.fetch(
                """
                SELECT * FROM position_events
                WHERE position_id = $1
                ORDER BY created_at ASC
                """,
                position_id,
            )
            return [dict(r) for r in rows]

        finally:
            await self._pool.release(conn)

    # ─── Helpers ──────────────────────────────────────────────────────────────

    def _row_to_position(self, row: asyncpg.Record) -> Position:
        if row is None:
            return None
        return Position(
            position_id=row["position_id"],
            canonical_id=row["canonical_id"],
            description=row["description"] or "",
            yes_platform=row["yes_platform"],
            no_platform=row["no_platform"],
            yes_market_id=row["yes_market_id"] or "",
            no_market_id=row["no_market_id"] or "",
            quantity=row["quantity"],
            yes_price=float(row["yes_price"]),
            no_price=float(row["no_price"]),
            status=PositionStatus(row["status"]),
            hedge_status=HedgeStatus(row["hedge_status"]),
            hedge_order_id=row["hedge_order_id"] or "",
            yes_order_id=row["yes_order_id"] or "",
            no_order_id=row["no_order_id"] or "",
            yes_fill_price=float(row["yes_fill_price"]),
            no_fill_price=float(row["no_fill_price"]),
            realized_pnl=float(row["realized_pnl"]),
            settlement_price=float(row["settlement_price"]),
            settlement_pnl=float(row["settlement_pnl"]),
            fees_paid=float(row["fees_paid"]),
            is_simulation=row["is_simulation"],
            unwind_reason=row["unwind_reason"] or "",
            notes=list(row["notes"]) if row["notes"] else [],
            created_at=row["created_at"],
            entry_confirmed_at=row["entry_confirmed_at"],
            closed_at=row["closed_at"],
            settled_at=row["settled_at"],
        )
