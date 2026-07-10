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
from .order_identity import SYNTHETIC_PLACEHOLDER_ORDER_SUFFIXES

logger = logging.getLogger("arbiter.execution.store")

_TERMINAL_STATUSES = {
    OrderStatus.FILLED,
    OrderStatus.CANCELLED,
    OrderStatus.FAILED,
    OrderStatus.ABORTED,
    OrderStatus.SIMULATED,
}

# Sentinel written into ``execution_arbs.recovery_notes`` by the naked-leg
# reconciliation flow (see ``mark_naked_leg_reconciled`` below). The
# ``list_half_recorded_arbs`` mode-2 query checks for this string to know
# whether an asymmetric-fill arb has already been booked, distinguishing
# real exposure from runtime ``recovery_check: ...`` polling noise. Stable
# string — do not rename without updating both the query and any
# operator-side runbooks that grep for it.
NAKED_LEG_RECONCILED_SENTINEL = "[naked-leg-reconciled]"


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
        async with self._pool.acquire() as conn:
            await self._upsert_order_on_conn(
                conn, order, arb_id=arb_id, client_order_id=client_order_id,
            )

    async def _upsert_order_on_conn(
        self,
        conn: asyncpg.Connection,
        order: Order,
        *,
        arb_id: Optional[str] = None,
        client_order_id: Optional[str] = None,
    ) -> None:
        """Execute the upsert against an already-acquired connection.

        Extracted so ``record_arb`` can wrap the arb INSERT and both leg
        upserts in a single ``conn.transaction()`` (C4.1) — three separate
        ``pool.acquire`` blocks would let a Postgres bounce leave orphan rows.
        """
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
        derived_arb_id = arb_id or _derive_arb_id(order.order_id)
        if derived_arb_id is None:
            raise ValueError(
                f"upsert_order requires arb_id (could not derive from order_id={order.order_id!r})"
            )
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
        synthetic_filters = " ".join(
            f"AND order_id NOT LIKE '%{suffix}'"
            for suffix in SYNTHETIC_PLACEHOLDER_ORDER_SUFFIXES
        )
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM execution_orders "
                "WHERE status IN ('pending', 'submitted', 'partial') "
                f"{synthetic_filters} "
                "ORDER BY submitted_at ASC"
            )
        return [self._row_to_order(r) for r in rows if r is not None]

    async def list_half_recorded_arbs(self) -> List[Dict[str, Any]]:
        """Surface arbs that still represent (or recently represented) naked
        single-leg venue exposure that the system has not yet booked.

        Two failure modes are detected:

        1. **Stub orphan** — ``record_arb_stub`` ran and the primary leg's
           ``upsert_order`` persisted, but ``record_arb`` (the atomic both-legs
           upsert) never completed. The arb row sits in a non-terminal status
           with fewer than two leg rows.

        2. **Asymmetric-fill orphan** — both leg rows persisted but only one
           filled (e.g. Kalshi YES filled, Polymarket NO returned ``failed``
           or ``cancelled``). The arb was marked ``failed``/``closed`` but
           ``unwind_pnl``/``realized_pnl`` were never recorded, so the books
           do not reflect what the auto-unwind paid (or, worse, the unwind
           never ran and venue exposure is still live). Original detection
           missed this class entirely because it required
           ``status NOT IN ('failed','closed',...)`` AND ``COUNT(orders) < 2``.

        The 2026-05-22 audit found 21 such arbs (ARB-000240..261 on
        DEM_HOUSE_2026) that the old query silently passed over. We surface
        every asymmetric-fill arb whose unwind has not been booked
        (``COALESCE(unwind_pnl, 0) = 0 AND COALESCE(realized_pnl, 0) = 0``)
        so an operator can verify venue state and either book the unwind PnL
        or close the residual position.

        ForecastEx pre-fix fills may persist as ``status='filled'`` with
        ``fill_qty=0`` even though the live broker account holds the position.
        Treat those rows as exposure-bearing using ``quantity`` as the
        effective fill quantity; false positives are safer than letting a
        naked venue position disappear from readiness.

        Returned dicts: ``arb_id``, ``canonical_id``, ``status``,
        ``created_at``, ``leg_count``, ``filled_leg_count``,
        ``zero_qty_filled_leg_count``, ``filled_notional``,
        ``leg_order_ids``.
        """
        if self._pool is None:
            await self.connect()
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                WITH order_exposure AS (
                    SELECT
                        o.*,
                        CASE
                            WHEN o.status IN ('filled', 'simulated')
                             AND COALESCE(o.fill_qty, 0) > 0
                            THEN COALESCE(o.fill_qty, 0)
                            WHEN o.platform = 'forecastex'
                             AND o.status = 'filled'
                             AND COALESCE(o.fill_qty, 0) = 0
                             AND COALESCE(o.quantity, 0) > 0
                            THEN COALESCE(o.quantity, 0)
                            ELSE 0
                        END AS effective_fill_qty,
                        CASE
                            WHEN o.platform = 'forecastex'
                             AND o.status = 'filled'
                             AND COALESCE(o.fill_qty, 0) = 0
                             AND COALESCE(o.quantity, 0) > 0
                            THEN 1
                            ELSE 0
                        END AS zero_qty_filled_leg
                    FROM execution_orders o
                )
                SELECT
                    a.arb_id,
                    a.canonical_id,
                    a.status,
                    a.created_at,
                    COUNT(o.order_id) AS leg_count,
                    COALESCE(SUM(
                        CASE
                            WHEN COALESCE(o.effective_fill_qty, 0) > 0
                            THEN 1
                            ELSE 0
                        END
                    ), 0) AS filled_leg_count,
                    COALESCE(SUM(
                        CASE
                            WHEN NOT (COALESCE(o.effective_fill_qty, 0) > 0)
                            THEN 1
                            ELSE 0
                        END
                    ), 0) AS unfilled_leg_count,
                    COALESCE(SUM(o.zero_qty_filled_leg), 0) AS zero_qty_filled_leg_count,
                    COALESCE(SUM(
                        CASE
                            WHEN COALESCE(o.effective_fill_qty, 0) > 0
                            THEN COALESCE(o.effective_fill_qty, 0) * (
                                CASE
                                    WHEN COALESCE(o.fill_price, 0) > 0
                                    THEN COALESCE(o.fill_price, 0)
                                    ELSE COALESCE(o.price, 0)
                                END
                            )
                            ELSE 0
                        END
                    ), 0) AS filled_notional,
                    COALESCE(
                        ARRAY_AGG(o.order_id ORDER BY o.submitted_at)
                            FILTER (WHERE o.order_id IS NOT NULL),
                        ARRAY[]::text[]
                    ) AS leg_order_ids
                FROM execution_arbs a
                LEFT JOIN order_exposure o ON o.arb_id = a.arb_id
                GROUP BY a.arb_id, a.canonical_id, a.status,
                         a.created_at, a.realized_pnl, a.unwind_pnl
                HAVING
                    -- Mode 1: stub orphan (active status, <2 leg rows)
                    (
                        COUNT(o.order_id) < 2
                        AND a.status NOT IN
                            ('filled', 'failed', 'simulated', 'recovering', 'closed')
                    )
                    OR
                    -- Mode 2: asymmetric fill, unwind not yet booked.
                    -- "Not yet booked" = no unwind/realized PnL AND no
                    -- reconciliation sentinel in recovery_notes. The sentinel
                    -- ``NAKED_LEG_RECONCILED_SENTINEL`` is written by the
                    -- backfill flow when an operator (or a future automated
                    -- reconciler) confirms what the auto-unwind did. We use
                    -- a sentinel rather than ``recovery_notes != ''`` because
                    -- the engine's runtime recovery loop appends noisy
                    -- ``recovery_check: ...`` lines on every poll, so a
                    -- non-empty ``recovery_notes`` alone is not evidence of
                    -- reconciliation. We use a sentinel rather than
                    -- ``unwind_pnl != 0`` because a clean auto-unwind that
                    -- closed at the buy price (e.g. NBA spread games where
                    -- the second leg failed before any price drift) produces
                    -- a *legitimate* unwind_pnl of 0.
                    (
                        SUM(
                            CASE WHEN COALESCE(o.effective_fill_qty, 0) > 0
                                 THEN 1 ELSE 0
                            END
                        ) >= 1
                        AND SUM(
                            CASE WHEN NOT (COALESCE(o.effective_fill_qty, 0) > 0)
                                 THEN 1 ELSE 0
                            END
                        ) >= 1
                        AND POSITION($1 IN COALESCE(a.recovery_notes, '')) = 0
                    )
                ORDER BY a.created_at ASC
                """,
                NAKED_LEG_RECONCILED_SENTINEL,
            )
        return [
            {
                "arb_id": r["arb_id"],
                "canonical_id": r["canonical_id"],
                "status": r["status"],
                "created_at": r["created_at"],
                "leg_count": int(r["leg_count"] or 0),
                "filled_leg_count": int(r["filled_leg_count"] or 0),
                "unfilled_leg_count": int(r["unfilled_leg_count"] or 0),
                "zero_qty_filled_leg_count": int(r["zero_qty_filled_leg_count"] or 0),
                "filled_notional": float(r["filled_notional"] or 0.0),
                "leg_order_ids": list(r["leg_order_ids"] or []),
            }
            for r in rows
        ]

    async def mark_naked_leg_reconciled(
        self,
        arb_id: str,
        *,
        unwind_pnl: float,
        note: str,
    ) -> None:
        """Idempotently mark an asymmetric-fill arb as reconciled.

        Writes the ``NAKED_LEG_RECONCILED_SENTINEL`` into ``recovery_notes``
        (appending if existing notes are present), updates ``unwind_pnl``,
        and bumps ``updated_at``. Future ``list_half_recorded_arbs`` calls
        will skip this arb's mode-2 branch.

        Called from the dashboard's manual reconciliation flow and from the
        2026-05-22 audit backfill. Idempotent: re-applying with the same
        values is a no-op-ish UPDATE that just touches ``updated_at``.
        """
        if self._pool is None:
            await self.connect()
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE execution_arbs
                   SET unwind_pnl = $2,
                       status = CASE
                           WHEN NOT EXISTS (
                               SELECT 1
                                 FROM execution_orders
                                WHERE arb_id = $1
                                  AND status IN ('pending', 'submitted', 'partial')
                           )
                           THEN 'closed'
                           ELSE status
                       END,
                       closed_at = CASE
                           WHEN NOT EXISTS (
                               SELECT 1
                                 FROM execution_orders
                                WHERE arb_id = $1
                                  AND status IN ('pending', 'submitted', 'partial')
                           )
                           THEN COALESCE(closed_at, NOW())
                           ELSE closed_at
                       END,
                       updated_at = NOW(),
                       recovery_notes = CASE
                           WHEN COALESCE(recovery_notes, '') = ''
                           THEN $3 || ' ' || $4
                           WHEN POSITION($3 IN recovery_notes) > 0
                           THEN recovery_notes
                           ELSE recovery_notes || E'\n' || $3 || ' ' || $4
                       END
                 WHERE arb_id = $1
                """,
                arb_id,
                Decimal(str(unwind_pnl)),
                NAKED_LEG_RECONCILED_SENTINEL,
                note,
            )

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
                    severity = EXCLUDED.severity,
                    message = EXCLUDED.message,
                    metadata = EXCLUDED.metadata,
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

    async def resolve_superseded_half_recorded_incidents(
        self,
        arb_ids: List[str],
    ) -> int:
        """Resolve duplicate legacy half-recorded alerts for active arbs.

        Older recovery runs generated a fresh random ``INC-HALF-*`` incident
        on every restart. The deterministic incident id remains open; prior
        duplicates are marked resolved so the operator sees one actionable
        alert per affected arb instead of restart noise.
        """
        if not arb_ids:
            return 0
        if self._pool is None:
            await self.connect()
        async with self._pool.acquire() as conn:
            result = await conn.execute(
                """
                UPDATE execution_incidents
                   SET status = 'resolved',
                       resolved_at = COALESCE(resolved_at, NOW()),
                       resolution_note = CASE
                           WHEN COALESCE(resolution_note, '') = ''
                           THEN 'Superseded by deterministic half-recorded incident'
                           ELSE resolution_note
                       END
                 WHERE status = 'open'
                   AND metadata->>'event_type' = 'half_recorded_arb'
                   AND arb_id = ANY($1::text[])
                   AND incident_id <> ('INC-HALF-' || arb_id)
                """,
                arb_ids,
            )
        try:
            return int(str(result).split()[-1])
        except (IndexError, ValueError):
            return 0

    async def mark_no_exposure_half_recorded_arbs_failed(
        self,
        arb_ids: List[str],
    ) -> int:
        """Close half-recorded DB stubs that never persisted any order leg."""
        if not arb_ids:
            return 0
        if self._pool is None:
            await self.connect()
        async with self._pool.acquire() as conn:
            result = await conn.execute(
                """
                UPDATE execution_arbs
                   SET status = 'failed',
                       updated_at = NOW(),
                       closed_at = COALESCE(closed_at, NOW()),
                       recovery_notes = CASE
                           WHEN COALESCE(recovery_notes, '') = ''
                           THEN 'Auto-closed on startup: half-recorded arb had zero persisted orders and zero filled notional.'
                           ELSE recovery_notes || E'\nAuto-closed on startup: half-recorded arb had zero persisted orders and zero filled notional.'
                       END
                 WHERE arb_id = ANY($1::text[])
                   AND status NOT IN ('filled', 'failed', 'simulated', 'recovering', 'closed')
                   AND NOT EXISTS (
                       SELECT 1 FROM execution_orders o
                        WHERE o.arb_id = execution_arbs.arb_id
                   )
                """,
                arb_ids,
            )
        try:
            return int(str(result).split()[-1])
        except (IndexError, ValueError):
            return 0

    async def resolve_half_recorded_incidents(
        self,
        arb_ids: List[str],
        *,
        note: str,
    ) -> int:
        """Resolve all open half-recorded incidents for the supplied arbs."""
        if not arb_ids:
            return 0
        if self._pool is None:
            await self.connect()
        async with self._pool.acquire() as conn:
            result = await conn.execute(
                """
                UPDATE execution_incidents
                   SET status = 'resolved',
                       resolved_at = COALESCE(resolved_at, NOW()),
                       resolution_note = CASE
                           WHEN COALESCE(resolution_note, '') = ''
                           THEN $2
                           ELSE resolution_note
                       END
                 WHERE status = 'open'
                   AND metadata->>'event_type' = 'half_recorded_arb'
                   AND arb_id = ANY($1::text[])
                """,
                arb_ids,
                note,
            )
        try:
            return int(str(result).split()[-1])
        except (IndexError, ValueError):
            return 0

    async def resolve_stale_half_recorded_incidents(
        self,
        active_arb_ids: List[str],
    ) -> int:
        """Resolve half-recorded incidents for arbs no longer in recovery output."""
        if self._pool is None:
            await self.connect()
        async with self._pool.acquire() as conn:
            result = await conn.execute(
                """
                UPDATE execution_incidents
                   SET status = 'resolved',
                       resolved_at = COALESCE(resolved_at, NOW()),
                       resolution_note = CASE
                           WHEN COALESCE(resolution_note, '') = ''
                           THEN 'Auto-resolved: half-recorded arb is no longer an active startup recovery blocker.'
                           ELSE resolution_note
                       END
                 WHERE status = 'open'
                   AND metadata->>'event_type' = 'half_recorded_arb'
                   AND NOT (arb_id = ANY($1::text[]))
                """,
                active_arb_ids,
            )
        try:
            return int(str(result).split()[-1])
        except (IndexError, ValueError):
            return 0

    async def list_incidents(
        self,
        *,
        status: Optional[str] = None,
        limit: int = 200,
    ) -> List[ExecutionIncident]:
        if self._pool is None:
            await self.connect()
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT incident_id, arb_id, canonical_id, severity, message,
                       metadata, status,
                       EXTRACT(EPOCH FROM created_at) AS ts,
                       EXTRACT(EPOCH FROM resolved_at) AS resolved_ts,
                       resolution_note
                FROM execution_incidents
                WHERE ($1::text IS NULL OR status = $1)
                ORDER BY created_at DESC
                LIMIT $2
                """,
                status,
                int(limit),
            )
        incidents: List[ExecutionIncident] = []
        for row in rows:
            metadata = row["metadata"] or {}
            if isinstance(metadata, str):
                try:
                    metadata = json.loads(metadata)
                except json.JSONDecodeError:
                    metadata = {}
            incidents.append(
                ExecutionIncident(
                    incident_id=row["incident_id"],
                    arb_id=row["arb_id"] or "",
                    canonical_id=row["canonical_id"] or "",
                    severity=row["severity"] or "warning",
                    message=row["message"] or "",
                    timestamp=float(row["ts"] or 0.0),
                    metadata=metadata,
                    status=row["status"] or "open",
                    resolved_at=float(row["resolved_ts"] or 0.0),
                    resolution_note=row["resolution_note"] or "",
                )
            )
        return incidents

    # ─── Top-level arb persistence ───────────────────────────────────────────

    async def record_arb_stub(
        self,
        arb_id: str,
        canonical_id: str,
        opportunity: Any = None,
        net_edge: Optional[float] = None,
    ) -> None:
        """Insert a placeholder ``execution_arbs`` row before any leg orders are
        persisted, so the FK from ``execution_orders.arb_id`` is satisfied while
        the legs are still in flight. Idempotent — uses ON CONFLICT DO NOTHING
        so the later ``record_arb`` call still upserts the final state.
        """
        if self._pool is None:
            await self.connect()
        opp_json = _opp_to_jsonb(opportunity)
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO execution_arbs (
                    arb_id, canonical_id, status, net_edge, realized_pnl,
                    opportunity_json, is_simulation
                ) VALUES ($1, $2, 'pending', $3, 0, $4::jsonb, FALSE)
                ON CONFLICT (arb_id) DO NOTHING
                """,
                arb_id,
                canonical_id,
                Decimal(str(net_edge)) if net_edge is not None else None,
                opp_json,
            )

    async def record_arb_stub_with_leg(
        self,
        arb_id: str,
        canonical_id: str,
        first_leg: Order,
        opportunity: Any = None,
        net_edge: Optional[float] = None,
        client_order_id: Optional[str] = None,
    ) -> None:
        """Insert the arb stub + first leg's order row in a single transaction.

        Replaces the legacy ``record_arb_stub`` → ``upsert_order`` two-write
        sequence used by the primary-leg path.  Previously the arb row could
        live in the DB for the duration of the secondary venue call while
        only one leg was persisted; if the engine crashed in that window the
        arb existed without legs (169 of 295 prod phantoms).  Now the arb
        cannot appear in ``execution_arbs`` without its first leg in
        ``execution_orders`` — either both rows commit or neither.

        Idempotent: the arb INSERT uses ON CONFLICT DO NOTHING (matching the
        legacy stub semantics so a re-call from recovery does not clobber
        in-flight state); the order uses ON CONFLICT DO UPDATE via the
        existing ``_upsert_order_on_conn`` helper.
        """
        if self._pool is None:
            await self.connect()
        opp_json = _opp_to_jsonb(opportunity)
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    """
                    INSERT INTO execution_arbs (
                        arb_id, canonical_id, status, net_edge, realized_pnl,
                        opportunity_json, is_simulation
                    ) VALUES ($1, $2, 'pending', $3, 0, $4::jsonb, FALSE)
                    ON CONFLICT (arb_id) DO NOTHING
                    """,
                    arb_id,
                    canonical_id,
                    Decimal(str(net_edge)) if net_edge is not None else None,
                    opp_json,
                )
                await self._upsert_order_on_conn(
                    conn, first_leg, arb_id=arb_id, client_order_id=client_order_id,
                )

    async def record_arb(self, arb_execution: ArbExecution) -> None:
        """Persist the arb row plus both leg upserts atomically (C4.1).

        All three writes live in a single ``conn.transaction()`` block on one
        connection: either every row commits or none do. Previously each call
        opened its own ``pool.acquire`` block, so a Postgres bounce between
        them could leave an orphan ``execution_arbs`` row with no legs, or
        legs with no parent — destroying the audit trail.
        """
        if self._pool is None:
            await self.connect()
        opp_json = _opp_to_jsonb(arb_execution.opportunity)
        is_sim = bool(arb_execution.status == "simulated")
        net_edge = getattr(arb_execution.opportunity, "net_edge", None)
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    """
                    INSERT INTO execution_arbs (
                        arb_id, canonical_id, status, net_edge, realized_pnl,
                        unwind_pnl, opportunity_json, is_simulation
                    ) VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, $8)
                    ON CONFLICT (arb_id) DO UPDATE SET
                        status         = EXCLUDED.status,
                        realized_pnl   = EXCLUDED.realized_pnl,
                        unwind_pnl     = EXCLUDED.unwind_pnl,
                        updated_at     = NOW(),
                        closed_at      = CASE WHEN EXCLUDED.status IN ('filled','failed','simulated','recovering')
                                             THEN NOW() ELSE execution_arbs.closed_at END
                    """,
                    arb_execution.arb_id,
                    arb_execution.opportunity.canonical_id if arb_execution.opportunity else "",
                    arb_execution.status,
                    Decimal(str(net_edge)) if net_edge is not None else None,
                    Decimal(str(arb_execution.realized_pnl)),
                    Decimal(str(getattr(arb_execution, "unwind_pnl", 0.0) or 0.0)),
                    opp_json,
                    is_sim,
                )
                await self._upsert_order_on_conn(
                    conn, arb_execution.leg_yes, arb_id=arb_execution.arb_id,
                )
                await self._upsert_order_on_conn(
                    conn, arb_execution.leg_no, arb_id=arb_execution.arb_id,
                )

        # Persist a trade analysis if the engine attached one. Best-effort:
        # a generation failure must never block the lifecycle write above.
        analysis_md = getattr(arb_execution, "analysis_md", None)
        if analysis_md:
            try:
                await self.update_arb_analysis(arb_execution.arb_id, analysis_md)
            except Exception as exc:
                logger.warning(
                    "ExecutionStore: persisting analysis_md for %s failed: %s",
                    arb_execution.arb_id,
                    exc,
                )

    async def update_arb_analysis(
        self, arb_id: str, analysis_md: str, version: int = 1
    ) -> None:
        """Write the markdown post-mortem produced by ``trade_analyzer``.

        Safe to call after every state transition: the upsert keeps the most
        recent analysis. ``version`` lets the backfill detect stale formats.
        """
        if self._pool is None:
            await self.connect()
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE execution_arbs
                   SET analysis_md = $2,
                       analysis_version = $3,
                       analysis_updated_at = NOW()
                 WHERE arb_id = $1
                """,
                arb_id,
                analysis_md or "",
                int(version),
            )

    # ─── Rehydration (load past trades on restart) ────────────────────────────

    async def load_execution_history(self, limit: Optional[int] = 200) -> List[ArbExecution]:
        """Rehydrate ArbExecution objects from the database for dashboard display.

        Loads the most recent ``limit`` arb executions with their YES/NO leg
        orders. Pass ``limit=None`` when startup reconciliation needs the full
        realized-P&L ledger instead of a bounded dashboard slice.
        """
        if self._pool is None:
            await self.connect()
        from ..scanner.arbitrage import ArbitrageOpportunity

        arb_sql = """
                SELECT arb_id, canonical_id, status, net_edge, realized_pnl,
                       COALESCE(unwind_pnl, 0) AS unwind_pnl,
                       opportunity_json, is_simulation, created_at
                FROM execution_arbs
                ORDER BY created_at DESC
                """
        args: tuple[Any, ...] = ()
        if limit is not None:
            arb_sql += "LIMIT $1"
            args = (int(limit),)

        async with self._pool.acquire() as conn:
            arb_rows = await conn.fetch(arb_sql, *args)
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
                unwind_pnl=float(arb_row["unwind_pnl"] or 0),
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
