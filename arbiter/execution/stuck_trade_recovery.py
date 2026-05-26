"""Recover trades that have been stuck in non-terminal states for too long.

The startup recovery in ``arbiter.execution.recovery`` only runs once per
process restart. A trade that gets stuck in ``pending`` / ``submitted`` /
``recovering`` after startup will sit there forever unless something polls
the venue API and reconciles. This module is that poller.

For each arb older than ``max_age_seconds`` (default 24h) and in a
non-terminal status:

  1. For each leg, ask the platform adapter what state it sees.
  2. If the venue says FILLED / CANCELLED / FAILED, update the DB row.
  3. If the venue raised an error (often a 404 — the order is unknown),
     mark the leg FAILED with ``"orphaned during recovery: ..."``.
  4. Compute a new ``status`` for the parent ``execution_arbs`` row from
     the (possibly updated) leg states and write it back.
  5. Always stamp ``last_checked_at`` so the UI can show when the system
     last looked at this trade, and append a one-line note to
     ``recovery_notes`` so the original status is preserved for audit.

The function is idempotent: re-running it on the same DB is a no-op once
every stuck trade has been reconciled, and partial progress on a previous
run does not block the next attempt.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import asyncpg

from .adapters.base import PlatformAdapter
from .engine import Order, OrderStatus
from .order_identity import is_synthetic_placeholder_order_id
from .store import ExecutionStore

logger = logging.getLogger("arbiter.execution.stuck_trade_recovery")


_TERMINAL_ARB_STATUSES = frozenset({
    "filled",
    "failed",
    "simulated",
    "cancelled",
    "closed",
})
_TERMINAL_LEG_STATUSES = frozenset({
    OrderStatus.FILLED,
    OrderStatus.CANCELLED,
    OrderStatus.FAILED,
    OrderStatus.ABORTED,
    OrderStatus.SIMULATED,
})


@dataclass
class StuckTradeOutcome:
    arb_id: str
    canonical_id: str
    original_status: str
    new_status: str
    age_seconds: float
    leg_updates: List[Dict[str, Any]] = field(default_factory=list)
    error: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "arb_id": self.arb_id,
            "canonical_id": self.canonical_id,
            "original_status": self.original_status,
            "new_status": self.new_status,
            "age_seconds": round(self.age_seconds, 1),
            "leg_updates": list(self.leg_updates),
            "error": self.error,
        }


@dataclass
class StuckTradeRecoveryStats:
    runs: int = 0
    last_run_ts: float = 0.0
    last_duration_s: float = 0.0
    inspected_last_run: int = 0
    updated_last_run: int = 0
    errors_last_run: int = 0
    total_updated: int = 0
    total_errors: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "runs": self.runs,
            "last_run_ts": self.last_run_ts,
            "last_duration_s": round(self.last_duration_s, 3),
            "inspected_last_run": self.inspected_last_run,
            "updated_last_run": self.updated_last_run,
            "errors_last_run": self.errors_last_run,
            "total_updated": self.total_updated,
            "total_errors": self.total_errors,
        }


async def list_stuck_arbs(
    store: ExecutionStore, *, max_age_seconds: float
) -> List[Dict[str, Any]]:
    """Return ``execution_arbs`` rows older than ``max_age_seconds`` and still
    in a non-terminal status, newest first. Each dict carries the parent arb
    fields plus a ``legs`` list of dicts (one per ``execution_orders`` row).
    """
    if store._pool is None:  # noqa: SLF001 — internal handle, intentionally
        await store.connect()
    cutoff_seconds = max(float(max_age_seconds), 0.0)
    async with store._pool.acquire() as conn:  # noqa: SLF001
        arb_rows = await conn.fetch(
            """
            SELECT arb_id, canonical_id, status, created_at, updated_at,
                   last_checked_at, recovery_notes,
                   EXTRACT(EPOCH FROM (NOW() - created_at)) AS age_seconds
              FROM execution_arbs
             WHERE status NOT IN ('filled','failed','simulated','cancelled','closed')
               AND EXTRACT(EPOCH FROM (NOW() - created_at)) >= $1
             ORDER BY created_at ASC
            """,
            cutoff_seconds,
        )
        if not arb_rows:
            return []
        arb_ids = [r["arb_id"] for r in arb_rows]
        leg_rows = await conn.fetch(
            """
            SELECT order_id, arb_id, platform, market_id, canonical_id,
                   side, price, quantity, status, fill_price, fill_qty,
                   error, submitted_at, updated_at, terminal_at
              FROM execution_orders
             WHERE arb_id = ANY($1)
            """,
            arb_ids,
        )

    legs_by_arb: Dict[str, List[asyncpg.Record]] = {}
    for row in leg_rows:
        legs_by_arb.setdefault(row["arb_id"], []).append(row)

    out: List[Dict[str, Any]] = []
    for row in arb_rows:
        out.append(
            {
                "arb_id": row["arb_id"],
                "canonical_id": row["canonical_id"],
                "status": row["status"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
                "last_checked_at": row["last_checked_at"],
                "recovery_notes": row["recovery_notes"] or "",
                "age_seconds": float(row["age_seconds"] or 0.0),
                "legs": legs_by_arb.get(row["arb_id"], []),
            }
        )
    return out


def _row_to_order(row: asyncpg.Record) -> Order:
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


def _derive_arb_status_from_legs(
    legs: List[Any],
) -> str:
    """Mirror the engine's arb-status derivation closely enough for recovery.

    Accepts either ``Order`` objects or bare ``OrderStatus`` values. When an
    Order is provided, a leg with ``status=FILLED`` and ``fill_qty<=0`` is
    treated as a *zero-fill* — the venue reported the order as terminal but
    no contracts changed hands — which means there is no naked exposure and
    the leg behaves like ``FAILED`` for status-derivation purposes. Without
    this, ARB-000682-style arbs (Polymarket FILLED@0 paired with a Kalshi
    ABORTED leg) stay in ``recovering`` forever because the unwind has
    nothing to close.

    Behavior table:
      - both filled (with qty>0) → ``filled``
      - either FAILED/CANCELLED/ABORTED with no surviving FILLED → ``failed``
      - one FILLED-with-qty>0 + the other terminal-not-filled → ``recovering``
      - one FILLED-with-qty==0 + the other terminal → ``failed``
        (no exposure, no recovery work)
      - fewer than two persisted legs plus a FILLED leg → manual blocker
      - any leg still non-terminal → leave the status alone (we did not get
        a clean answer from the venue and the operator must investigate)
    """
    effective: List[OrderStatus] = []
    for leg in legs:
        if isinstance(leg, OrderStatus):
            effective.append(leg)
            continue
        status = getattr(leg, "status", None)
        if not isinstance(status, OrderStatus):
            continue
        if status == OrderStatus.FILLED and float(getattr(leg, "fill_qty", 0) or 0) <= 0:
            # Venue acked the order as terminal but no contracts traded —
            # no exposure to recover. Demote to FAILED for derivation so
            # the arb can converge instead of looping forever.
            effective.append(OrderStatus.FAILED)
        else:
            effective.append(status)

    filled = sum(1 for s in effective if s == OrderStatus.FILLED)
    if len(effective) < 2:
        if filled:
            return ""
        if effective and all(s in _TERMINAL_LEG_STATUSES for s in effective):
            return "failed"
        return ""
    has_nonterminal = any(s not in _TERMINAL_LEG_STATUSES for s in effective)
    if has_nonterminal:
        return ""  # caller keeps original status; will retry on next run
    if filled == len(effective) and filled > 0:
        return "filled"
    if filled == 0:
        return "failed"
    # Mixed: one filled, the other terminal-not-filled → still naked unless
    # the survivor is itself ABORTED (engine recovery cancelled it) → failed.
    return "recovering" if any(s == OrderStatus.FILLED for s in effective) else "failed"


async def _query_leg_state(
    adapter: Optional[PlatformAdapter], order: Order,
) -> tuple[Order, str]:
    """Return (possibly updated Order, note). Never raises across the boundary."""
    if is_synthetic_placeholder_order_id(order.order_id):
        if order.status not in _TERMINAL_LEG_STATUSES:
            order.status = OrderStatus.ABORTED
            order.error = (
                (order.error + "; ") if order.error else ""
            ) + "local synthetic placeholder; never submitted to venue"
            return order, "local synthetic placeholder terminalized without venue lookup"
        return order, "local synthetic placeholder; no venue lookup"
    if adapter is None:
        return order, f"no adapter for platform={order.platform}"
    try:
        fresh = await adapter.get_order(order)
    except Exception as exc:  # noqa: BLE001 — adapter errors are expected
        order.status = OrderStatus.FAILED
        order.error = f"orphaned during recovery: {exc}"
        return order, f"get_order raised: {exc}"
    # Treat "not found" responses the same as the startup reconciler does.
    if (
        fresh.status == OrderStatus.FAILED
        and "not found" in (fresh.error or "").lower()
    ):
        return fresh, "venue reports order not found"
    if fresh.status == order.status:
        return fresh, "no change"
    return fresh, f"{order.status.value} -> {fresh.status.value}"


async def _persist_outcome(
    store: ExecutionStore,
    arb_id: str,
    new_status: str,
    original_status: str,
    note: str,
    now_ts: float,
) -> None:
    """Stamp ``last_checked_at`` and append to ``recovery_notes``. Updates
    the parent ``status`` only when ``new_status`` is non-empty."""
    if store._pool is None:  # noqa: SLF001
        await store.connect()
    note_line = f"[{int(now_ts)}] {note}"
    if new_status:
        sql = """
            UPDATE execution_arbs
               SET status = $2::text,
                   last_checked_at = NOW(),
                   updated_at = NOW(),
                   closed_at = CASE WHEN $2::text IN ('filled','failed','simulated','cancelled','closed')
                                    THEN COALESCE(closed_at, NOW())
                                    ELSE closed_at END,
                   recovery_notes = CASE
                       WHEN COALESCE(recovery_notes,'') = '' THEN $3
                       ELSE recovery_notes || E'\n' || $3
                   END
             WHERE arb_id = $1
        """
        params = (arb_id, new_status, note_line)
    else:
        sql = """
            UPDATE execution_arbs
               SET last_checked_at = NOW(),
                   recovery_notes = CASE
                       WHEN COALESCE(recovery_notes,'') = '' THEN $2
                       ELSE recovery_notes || E'\n' || $2
                   END
             WHERE arb_id = $1
        """
        params = (arb_id, note_line)
    async with store._pool.acquire() as conn:  # noqa: SLF001
        await conn.execute(sql, *params)


async def recover_stuck_trades(
    store: ExecutionStore,
    adapters: Dict[str, PlatformAdapter],
    *,
    max_age_seconds: float = 86400.0,
    stats: Optional[StuckTradeRecoveryStats] = None,
) -> List[StuckTradeOutcome]:
    """Inspect every stuck arb older than ``max_age_seconds`` and reconcile.

    Returns a list of ``StuckTradeOutcome`` (one per arb the function looked
    at). The list is the *report* for this run; persistence is done as we go,
    so a crash mid-loop still records the trades we already updated.
    """
    run_start = time.time()
    outcomes: List[StuckTradeOutcome] = []
    try:
        stuck = await list_stuck_arbs(store, max_age_seconds=max_age_seconds)
    except Exception as exc:  # noqa: BLE001
        logger.error("stuck_trade_recovery: list_stuck_arbs failed: %s", exc)
        if stats is not None:
            stats.runs += 1
            stats.last_run_ts = run_start
            stats.last_duration_s = time.time() - run_start
            stats.errors_last_run = 1
            stats.total_errors += 1
        return outcomes

    if not stuck:
        if stats is not None:
            stats.runs += 1
            stats.last_run_ts = run_start
            stats.last_duration_s = time.time() - run_start
            stats.inspected_last_run = 0
            stats.updated_last_run = 0
            stats.errors_last_run = 0
        return outcomes

    logger.info("stuck_trade_recovery: inspecting %d stuck arb(s)", len(stuck))

    updated = 0
    errors = 0
    for arb in stuck:
        outcome = StuckTradeOutcome(
            arb_id=arb["arb_id"],
            canonical_id=arb["canonical_id"] or "",
            original_status=arb["status"] or "",
            new_status=arb["status"] or "",
            age_seconds=arb["age_seconds"],
        )
        leg_statuses: List[OrderStatus] = []
        fresh_legs: List[Order] = []
        try:
            for leg_row in arb["legs"]:
                order = _row_to_order(leg_row)
                original_leg_status = order.status
                adapter = adapters.get(order.platform)
                fresh, note = await _query_leg_state(adapter, order)
                leg_statuses.append(fresh.status)
                fresh_legs.append(fresh)
                outcome.leg_updates.append(
                    {
                        "order_id": order.order_id,
                        "platform": order.platform,
                        "side": order.side,
                        "old_status": original_leg_status.value,
                        "new_status": fresh.status.value,
                        "note": note,
                    }
                )
                if fresh.status != original_leg_status:
                    try:
                        await store.upsert_order(
                            fresh, arb_id=arb["arb_id"],
                        )
                    except Exception as exc:  # noqa: BLE001
                        outcome.error = f"upsert_order failed: {exc}"
                        errors += 1

            new_status = _derive_arb_status_from_legs(fresh_legs) if fresh_legs else ""
            outcome.new_status = new_status or outcome.original_status

            note = (
                f"recovery_check: original={outcome.original_status} "
                f"derived={new_status or 'unchanged'} "
                f"legs={','.join(s.value for s in leg_statuses) or 'none'}"
            )
            await _persist_outcome(
                store,
                arb_id=arb["arb_id"],
                new_status=new_status if new_status and new_status != outcome.original_status else "",
                original_status=outcome.original_status,
                note=note,
                now_ts=time.time(),
            )
            if new_status and new_status != outcome.original_status:
                updated += 1
                logger.info(
                    "stuck_trade_recovery: %s %s -> %s (age=%ds)",
                    arb["arb_id"],
                    outcome.original_status,
                    new_status,
                    int(outcome.age_seconds),
                )
        except Exception as exc:  # noqa: BLE001 — per-arb failures isolate
            outcome.error = f"recovery raised: {exc}"
            errors += 1
            logger.warning(
                "stuck_trade_recovery: %s raised: %s", arb["arb_id"], exc,
            )

        outcomes.append(outcome)

    if stats is not None:
        stats.runs += 1
        stats.last_run_ts = run_start
        stats.last_duration_s = time.time() - run_start
        stats.inspected_last_run = len(outcomes)
        stats.updated_last_run = updated
        stats.errors_last_run = errors
        stats.total_updated += updated
        stats.total_errors += errors

    logger.info(
        "stuck_trade_recovery: complete inspected=%d updated=%d errors=%d duration=%.2fs",
        len(outcomes),
        updated,
        errors,
        time.time() - run_start,
    )
    return outcomes
