"""Execution order-id classification helpers."""
from __future__ import annotations

SYNTHETIC_PLACEHOLDER_ORDER_SUFFIXES = (
    "-SKIPPED",
    "-EDGE-LOST",
    "-UNPROFITABLE",
    "-EXCEPTION",
    "-NOADAPTER",
    "-DBFAIL",
)


def is_synthetic_placeholder_order_id(order_id: str | None) -> bool:
    """Return True for local safety placeholders that were never venue orders."""
    text = str(order_id or "").strip().upper()
    return any(text.endswith(suffix) for suffix in SYNTHETIC_PLACEHOLDER_ORDER_SUFFIXES)
