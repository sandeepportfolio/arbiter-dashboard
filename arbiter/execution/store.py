"""ExecutionStore -- durable Postgres-backed audit trail for execution state.

Mirrors arbiter.ledger.position_ledger.PositionLedger lifecycle.
Writes on every state transition for full audit (per CONTEXT D-16).

Security: every SQL statement uses asyncpg parameterized bindings ($1, $2, ...);
no f-string interpolation of caller data. The single dynamic-SQL clause
(`terminal_clause` in upsert_order) is selected from two fixed string literals
based on OrderStatus enum membership -- no user input is ever interpolated.
"""
from __future__ import annotations

import json
import logging
from dataclasses import asdict, is_dataclass
from decimal import Decimal
from typing import Any, Dict, List, Optional

import asyncpg

from ..sql.connection import create_pool
from ..sql.migrate import apply_pending
from .engine import ArbExecution, ExecutionIncident, Order, OrderStatus

logger = logging.getLogger("arbiter.execution.store")

_TERMINAL_STATUSES = {
    OrderStatus.FILLED,
    OrderStatus.CANCELLED,
    OrderStatus.FAILED,
    OrderStatus.ABORTED,
    OrderStatus.SIMULATED,
}


def _opp_to_jsonb(opportunity: Any) -> str:
    """Serialize ArbitrageOpportunity (or any dataclass) to JSON string for JSONB column."""
    if opportunity is None:
        return "null"
    if hasattr(opportunity, "to_dict"):
        return json.dumps(opportunity.to_dict(), default=str)
    if is_dataclass(opportunity):
        return json.dumps(asdict(opportunity), default=str)
    return json.dumps(opportunity, default=str)


class ExecutionStore:
    """Postgres-backed durable store for execution state.

    Pool config matches arbiter/ledger/position_ledger.py exactly so behavior
    under load is consistent across the two stores.
    """

    _pool: Optional[asyncpg.Pool] = None

    def __init__(self, database_url: str):
        self.database_url = database_url

    # ─── Connection lifecycle ────────────────────────────────────────────────

    async def connect(self) -> None:
        if self._pool is None:
            self._pool = await create_pool(
                self.database_url,
                min_size=2,
                max_size=10,
                command_timeout=30,
            )
            logger.info("ExecutionStore: connected to Postgres")

    async def disconnect(self) -> None:
        if self._pool:
            await self._pool.close()
            self._pool = None
            logger.info("ExecutionStore: disconnected from Postgres")

    async def acquire(self) -> asyncpg.Connection:
        if self._pool is None:
            await self.connect()
        return await self._pool.acquire()

    async def init_schema(self) -> None:
        """Apply any pending migrations in arbiter/sql/migrations/."""
        applied = await apply_pending(self.database_url)
        logger.info("ExecutionStore: applied %d migration(s): %s", len(applied), applied)

    # ─── Order persistence (every state transition) ──────────────────────────

    async def upsert_order(
        self,
        order: Order,
        *,
        arb_id: Optional[str] = None,
        client_order_id: Optional[str] = None,
    ) -> None:
        """Insert order on first call; update status/fill/error on subsequent calls.

        arb_id and client_order_id are required on first INSERT; on UPDATE they
        are not changed. Pass arb_id from the caller (engine knows the ARB-NNN
        prefix); client_order_id is set by the adapter (Kalshi only -- Polymarket has none).
        """
        if self._pool is None:
            await self.connect()

        # terminal_clause is chosen from two fixed literals based on enum membership -- no user input.
        terminal_clause = (
            "terminal_at = NOW()"
            if order.status in _TERMINAL_STATUSES
            else "terminal_at = execution_orders.terminal_at"
        )

        sql = f"""
        INSERT INTO execution_orders (
            order_id, arb_id, client_order_id, platform,
            market_id, canonical_id, side, price, quantity, status,
            fill_price, fill_qty, error
        ) VALUES (
            $1, $2, $3, $4,
            $5, $6, $7, $8, $9, $10,
            $11, $12, $13
        )
        ON CONFLICT (order_id) DO UPDATE SET
            status      = EXCLUDED.status,
            fill_price  = EXCLUDED.fill_price,
            fill_qty    = EXCLUDED.fill_qty,
            error       = EXCLUDED.error,
            updated_at  = NOW(),
            {terminal_clause}
        """
        # arb_id resolution: prefer explicit, then derive from order_id prefix "ARB-NNNNNN-..."
        derived_arb_id = arb_id or _derive_arb_id(order.order_id)
        if derived_arb_id is None:
            raise ValueError(
                f"upsert_order requires arb_id (could not derive from order_id={order.order_id!r})"
            )

        async with self._pool.acquire() as conn:
            await conn.execute(
                sql,
                order.order_id,
                derived_arb_id,
                client_order_id,
                order.platform,
                order.market_id,
                order.canonical_id,
                order.side,
                Decimal(str(order.price)),
                Decimal(str(order.quantity)),
                order.status.value,
                Decimal(str(order.fill_price)),
                Decimal(str(order.fill_qty)),
                order.error or "",
            )

    async def get_order(self, order_id: str) -> Optional[Order]:
        if self._pool is None:
            await self.connect()
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM execution_orders WHERE order_id = $1", order_id
            )
        return self._row_to_order(row)

    async def list_non_terminal_orders(self) -> List[Order]:
        if self._pool is None:
            await self.connect()
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM execution_orders "
                "WHERE status IN ('pending', 'submitted', 'partial') "
                "ORDER BY submitted_at ASC"
            )
        return [self._row_to_order(r) for r in rows if r is not None]

    # ─── Fill persistence ────────────────────────────────────────────────────

    async def insert_fill(
        self, order_id: str, price: float, quantity: float, fees_paid: float = 0.0
    ) -> int:
        if self._pool is None:
            await self.connect()
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO execution_fills (order_id, price, quantity, fees_paid)
                VALUES ($1, $2, $3, $4)
                RETURNING fill_id
                """,
                order_id,
                Decimal(str(price)),
                Decimal(str(quantity)),
                Decimal(str(fees_paid)),
            )
        return int(row["fill_id"])

    # ─── Incident persistence ────────────────────────────────────────────────

    async def insert_incident(self, incident: ExecutionIncident) -> None:
        if self._pool is None:
            await self.connect()
        metadata_json = json.dumps(incident.metadata or {}, default=str)
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO execution_incidents (
                    incident_id, arb_id, canonical_id, severity,
                    message, metadata, status, resolved_at, resolution_note
                ) VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7, $8, $9)
                ON CONFLICT (incident_id) DO UPDATE SET
                    status = EXCLUDED.status,
                    resolved_at = EXCLUDED.resolved_at,
                    resolution_note = EXCLUDED.resolution_note
                """,
                incident.incident_id,
                incident.arb_id,
                incident.canonical_id,
                incident.severity,
                incident.message,
                metadata_json,
                incident.status,
                _epoch_to_ts(incident.resolved_at),
                incident.resolution_note or "",
            )

    # ─── Top-level arb persistence ───────────────────────────────────────────

    async def record_arb(self, arb_execution: ArbExecution) -> None:
        if self._pool is None:
            await self.connect()
        opp_json = _opp_to_jsonb(arb_execution.opportunity)
        is_sim = bool(arb_execution.status == "simulated")
        net_edge = getattr(arb_execution.opportunity, "net_edge", None)
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO execution_arbs (
                    arb_id, canonical_id, status, net_edge, realized_pnl,
                    opportunity_json, is_simulation
                ) VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7)
                ON CONFLICT (arb_id) DO UPDATE SET
                    status         = EXCLUDED.status,
                    realized_pnl   = EXCLUDED.realized_pnl,
                    updated_at     = NOW(),
                    closed_at      = CASE WHEN EXCLUDED.status IN ('filled','failed','simulated','recovering')
                                         THEN NOW() ELSE execution_arbs.closed_at END
                """,
                arb_execution.arb_id,
                arb_execution.opportunity.canonical_id if arb_execution.opportunity else "",
                arb_execution.status,
                Decimal(str(net_edge)) if net_edge is not None else None,
                Decimal(str(arb_execution.realized_pnl)),
                opp_json,
                is_sim,
            )
        # Persist both legs (delegates to upsert_order)
        await self.upsert_order(arb_execution.leg_yes, arb_id=arb_execution.arb_id)
        await self.upsert_order(arb_execution.leg_no, arb_id=arb_execution.arb_id)

    # ─── Rehydration (load past trades on restart) ────────────────────────────

    async def load_execution_history(self, limit: int = 200) -> List[ArbExecution]:
        """Rehydrate ArbExecution objects from the database for dashboard display.

        Loads the most recent `limit` arb executions with their YES/NO leg orders.
        Used on startup to populate the engine's execution_history so the
        Trades and Positions tabs show historical data.
        """
        if self._pool is None:
            await self.connect()
        from ..scanner.arbitrage import ArbitrageOpportunity

        async with self._pool.acquire() as conn:
            arb_rows = await conn.fetch(
                """
                SELECT arb_id, canonical_id, status, net_edge, realized_pnl,
                       opportunity_json, is_simulation, created_at
                FROM execution_arbs
                ORDER BY created_at DESC
                LIMIT $1
                """,
                limit,
            )
            # Build a map of arb_id -> [order rows]
            arb_ids = [r["arb_id"] for r in arb_rows]
            if not arb_ids:
                return []

            order_rows = await conn.fetch(
                """
                SELECT order_id, arb_id, platform, market_id, canonical_id,
                       side, price, quantity, status, fill_price, fill_qty,
                       submitted_at, error
                FROM execution_orders
                WHERE arb_id = ANY($1)
                ORDER BY submitted_at ASC
                """,
                arb_ids,
            )

        # Group orders by arb_id
        orders_by_arb: Dict[str, List] = {}
        for row in order_rows:
            orders_by_arb.setdefault(row["arb_id"], []).append(row)

        executions: List[ArbExecution] = []
        for arb_row in reversed(arb_rows):  # oldest first
            arb_id = arb_row["arb_id"]
            opp_json = arb_row["opportunity_json"]

            # Reconstruct ArbitrageOpportunity from stored JSON
            opp_data = json.loads(opp_json) if isinstance(opp_json, str) else (opp_json or {})
            try:
                opp = ArbitrageOpportunity(
                    canonical_id=opp_data.get("canonical_id", arb_row["canonical_id"] or ""),
                    description=opp_data.get("description", ""),
                    yes_platform=opp_data.get("yes_platform", ""),
                    yes_price=float(opp_data.get("yes_price", 0)),
                    yes_fee=float(opp_data.get("yes_fee", 0)),
                    yes_market_id=opp_data.get("yes_market_id", ""),
                    no_platform=opp_data.get("no_platform", ""),
                    no_price=float(opp_data.get("no_price", 0)),
                    no_fee=float(opp_data.get("no_fee", 0)),
                    no_market_id=opp_data.get("no_market_id", ""),
                    gross_edge=float(opp_data.get("gross_edge", 0)),
                    total_fees=float(opp_data.get("total_fees", 0)),
                    net_edge=float(opp_data.get("net_edge", 0)),
                    net_edge_cents=float(opp_data.get("net_edge_cents", 0)),
                    suggested_qty=int(opp_data.get("suggested_qty", 0)),
                    max_profit_usd=float(opp_data.get("max_profit_usd", 0)),
                    timestamp=float(opp_data.get("timestamp", 0)),
                    status=opp_data.get("status", "candidate"),
                    mapping_status=opp_data.get("mapping_status", "candidate"),
                )
            except Exception as exc:
                logger.warning("Failed to reconstruct opportunity for %s: %s", arb_id, exc)
                continue

            # Find YES and NO leg orders
            leg_orders = orders_by_arb.get(arb_id, [])
            leg_yes = leg_no = None
            for orow in leg_orders:
                order = self._row_to_order(orow)
                if order is None:
                    continue
                side = (orow["side"] or "").upper()
                if side == "YES" or "YES" in (orow["order_id"] or "").upper():
                    leg_yes = order
                elif side == "NO" or "NO" in (orow["order_id"] or "").upper():
                    leg_no = order

            # Fallback: create placeholder orders if not found
            if leg_yes is None:
                leg_yes = Order(
                    order_id=f"{arb_id}-YES",
                    platform=opp.yes_platform,
                    market_id=opp.yes_market_id,
                    canonical_id=opp.canonical_id,
                    side="YES",
                    price=opp.yes_price,
                    quantity=opp.suggested_qty,
                    status=OrderStatus.FILLED if arb_row["status"] in ("filled", "simulated") else OrderStatus.PENDING,
                    fill_price=opp.yes_price,
                    fill_qty=opp.suggested_qty if arb_row["status"] in ("filled", "simulated") else 0,
                )
            if leg_no is None:
                leg_no = Order(
                    order_id=f"{arb_id}-NO",
                    platform=opp.no_platform,
                    market_id=opp.no_market_id,
                    canonical_id=opp.canonical_id,
                    side="NO",
                    price=opp.no_price,
                    quantity=opp.suggested_qty,
                    status=OrderStatus.FILLED if arb_row["status"] in ("filled", "simulated") else OrderStatus.PENDING,
                    fill_price=opp.no_price,
                    fill_qty=opp.suggested_qty if arb_row["status"] in ("filled", "simulated") else 0,
                )

            created_at = arb_row["created_at"]
            ts = created_at.timestamp() if created_at else 0.0

            execution = ArbExecution(
                arb_id=arb_id,
                opportunity=opp,
                leg_yes=leg_yes,
                leg_no=leg_no,
                status=arb_row["status"] or "unknown",
                realized_pnl=float(arb_row["realized_pnl"] or 0),
                timestamp=ts,
            )
            executions.append(execution)

        logger.info("Rehydrated %d execution(s) from database", len(executions))
        return executions

    # ─── Helpers ─────────────────────────────────────────────────────────────

    @staticmethod
    def _row_to_order(row: Optional[asyncpg.Record]) -> Optional[Order]:
        if row is None:
            return None
        return Order(
            order_id=row["order_id"],
            platform=row["platform"],
            market_id=row["market_id"],
            canonical_id=row["canonical_id"],
            side=row["side"],
            price=float(row["price"]),
            quantity=int(float(row["quantity"])),
            status=OrderStatus(row["status"]),
            fill_price=float(row["fill_price"]),
            fill_qty=float(row["fill_qty"]),
            timestamp=row["submitted_at"].timestamp() if row["submitted_at"] else 0.0,
            error=row["error"] or "",
        )


def _derive_arb_id(order_id: str) -> Optional[str]:
    """ARB-NNNNNN-YES-... -> ARB-NNNNNN. Returns None if not a recognized prefix."""
    if not order_id or not order_id.startswith("ARB-"):
        return None
    parts = order_id.split("-")
    if len(parts) >= 2:
        return f"{parts[0]}-{parts[1]}"
    return None


def _epoch_to_ts(epoch: float) -> Optional[Any]:
    """Convert epoch seconds to a datetime asyncpg can write to TIMESTAMPTZ. None for 0/falsy."""
    if not epoch:
        return None
    from datetime import datetime, timezone
    return datetime.fromtimestamp(float(epoch), tz=timezone.utc)
