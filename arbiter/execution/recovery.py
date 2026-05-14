"""Restart reconciliation hook for ExecutionEngine (EXEC-02 part 2 / D-17).

Pitfall 5 mitigation: a process crash mid-execution can leave the DB
showing orders in non-terminal status (pending/submitted/partial) when the
platform has actually settled them (filled/cancelled). Without reconciliation,
DB and platform drift — in the worst case we have a ghost position.

This module is called from arbiter/main.py:run_system on startup, BEFORE
engine.run is called, so the engine begins with a coherent view of state.
"""
from __future__ import annotations

import logging
import time
import uuid
from typing import Any, Dict, List

from .adapters.base import PlatformAdapter
from .engine import ExecutionIncident, Order, OrderStatus
from .store import ExecutionStore

logger = logging.getLogger("arbiter.execution.recovery")


class RecoveryInitError(RuntimeError):
    """Raised when restart reconciliation cannot enumerate prior state.

    A clean startup MUST know which orders the platform considers open; we
    cannot infer that from a missing DB connection or a half-built schema.
    Callers are expected to either refuse to start the engine or arm the
    SafetySupervisor (no new trades) and surface an operator-facing alert.
    """


async def reconcile_non_terminal_orders(
    store: ExecutionStore,
    adapters: Dict[str, PlatformAdapter],
) -> List[Order]:
    """Reconcile DB-state vs platform-state for any non-terminal orders.

    Returns the list of orphaned orders (platform has no record of them).
    Caller (``arbiter/main.py``) should emit an incident for each orphaned
    order and let the operator decide whether to manually intervene.

    The function is idempotent — running it twice in a row is safe
    (second run either finds the same set, or a strict subset because
    the first run already reconciled the others).
    """
    orphaned: List[Order] = []
    try:
        orders = await store.list_non_terminal_orders()
    except Exception as exc:
        # CRITICAL: a silent return-[] here lets the engine start with no
        # idea of what's actually open on the platform — exactly the ghost-
        # position scenario this module exists to prevent. Re-raise so the
        # caller (arbiter/main.py) refuses to start the engine, or — at the
        # operator's discretion — arms SafetySupervisor before any trade.
        logger.critical(
            "recovery: failed to list non-terminal orders — aborting startup: %s",
            exc,
        )
        raise RecoveryInitError(
            f"failed to enumerate non-terminal orders during recovery: {exc}"
        ) from exc

    if not orders:
        logger.info("recovery: no non-terminal orders to reconcile")
        return []

    logger.info("recovery: reconciling %d non-terminal orders", len(orders))

    for order in orders:
        adapter = adapters.get(order.platform)
        if adapter is None:
            logger.warning(
                "recovery: no adapter for platform=%s order_id=%s",
                order.platform,
                order.order_id,
            )
            continue

        try:
            fresh = await adapter.get_order(order)
        except Exception as exc:
            logger.warning(
                "recovery: get_order raised for %s: %s", order.order_id, exc,
            )
            order.status = OrderStatus.FAILED
            order.error = f"orphaned on restart: {exc}"
            try:
                await store.upsert_order(order, arb_id=_derive_arb_id(order.order_id))
            except Exception as upsert_exc:
                logger.error(
                    "recovery: failed to mark orphaned %s: %s",
                    order.order_id,
                    upsert_exc,
                )
            orphaned.append(order)
            continue

        # Adapter returned a clean "not found" response
        if (
            fresh.status == OrderStatus.FAILED
            and "not found" in (fresh.error or "").lower()
        ):
            logger.info(
                "recovery: orphaned (platform has no record) %s", order.order_id,
            )
            try:
                await store.upsert_order(fresh, arb_id=_derive_arb_id(order.order_id))
            except Exception as upsert_exc:
                logger.error(
                    "recovery: failed to mark orphaned %s: %s",
                    order.order_id,
                    upsert_exc,
                )
            orphaned.append(fresh)
            continue

        if fresh.status != order.status:
            logger.info(
                "recovery: reconciled %s old=%s new=%s",
                order.order_id,
                order.status.value,
                fresh.status.value,
            )
            try:
                await store.upsert_order(fresh, arb_id=_derive_arb_id(order.order_id))
            except Exception as upsert_exc:
                logger.error(
                    "recovery: failed to upsert reconciled %s: %s",
                    order.order_id,
                    upsert_exc,
                )

    logger.info("recovery: complete. orphaned=%d", len(orphaned))
    return orphaned


def _derive_arb_id(order_id: str) -> str:
    """``ARB-NNNNNN-YES-...`` -> ``ARB-NNNNNN``.

    Returns the input unchanged when the format is not recognized; the
    caller (``store.upsert_order``) raises on a genuinely unusable arb_id.
    """
    if not order_id or not order_id.startswith("ARB-"):
        return order_id
    parts = order_id.split("-")
    if len(parts) >= 2:
        return f"{parts[0]}-{parts[1]}"
    return order_id


async def reconcile_half_recorded_arbs(
    store: ExecutionStore,
) -> List[Dict[str, Any]]:
    """Detect ``execution_arbs`` rows with fewer than 2 leg orders persisted.

    These are crash / exception-escape orphans: ``record_arb_stub`` and the
    primary leg's ``upsert_order`` ran, but ``record_arb`` (the atomic
    both-legs upsert) never completed. Caused historically by an exception
    escaping ``_live_execution`` between primary fill and secondary order
    construction — that path is now wrapped in try/except, but a true
    process kill (SIGKILL / OOM / power-loss between writes) can still
    leave this footprint.

    For each half-recorded arb, persist a critical ``half_recorded_arb``
    ``ExecutionIncident`` so the operator is paged and can manually
    reconcile against the venue. Best-effort: a per-row insert failure
    must not abort the rest of the loop.

    Raises ``RecoveryInitError`` if the underlying query fails — callers
    should refuse to start the engine and arm SafetySupervisor (no new
    trades) until the operator clears the underlying problem.

    Returns the list of half-recorded arb summaries (same shape as
    ``ExecutionStore.list_half_recorded_arbs``).
    """
    try:
        orphans = await store.list_half_recorded_arbs()
    except Exception as exc:
        logger.critical(
            "recovery: failed to list half-recorded arbs — aborting startup: %s",
            exc,
        )
        raise RecoveryInitError(
            f"failed to enumerate half-recorded arbs during recovery: {exc}"
        ) from exc

    if not orphans:
        logger.info("recovery: no half-recorded arbs to reconcile")
        return []

    logger.warning(
        "recovery: detected %d half-recorded arb(s) — emitting critical incidents",
        len(orphans),
    )

    for orphan in orphans:
        arb_id = orphan["arb_id"]
        leg_count = int(orphan.get("leg_count") or 0)
        leg_ids = list(orphan.get("leg_order_ids") or [])
        incident = ExecutionIncident(
            incident_id=f"INC-HALF-{uuid.uuid4().hex[:8]}",
            arb_id=arb_id,
            canonical_id=(orphan.get("canonical_id") or ""),
            severity="critical",
            message=(
                f"Half-recorded arb {arb_id}: only {leg_count} leg(s) persisted. "
                "Engine crashed or an exception escaped between primary fill and "
                "secondary order construction. Check the venue for an unmatched "
                "fill and reconcile manually."
            ),
            timestamp=time.time(),
            metadata={
                "event_type": "half_recorded_arb",
                "arb_id": arb_id,
                "leg_count": leg_count,
                "leg_order_ids": leg_ids,
                "stuck_status": orphan.get("status"),
            },
        )
        try:
            await store.insert_incident(incident)
        except Exception as exc:
            logger.error(
                "recovery: failed to persist half-recorded incident for %s: %s",
                arb_id, exc,
            )

    return orphans
