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
from typing import Dict, List

from .adapters.base import PlatformAdapter
from .engine import Order, OrderStatus
from .store import ExecutionStore

logger = logging.getLogger("arbiter.execution.recovery")


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
        logger.error("recovery: failed to list non-terminal orders: %s", exc)
        return []

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
