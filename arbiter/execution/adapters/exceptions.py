"""Shared exceptions for execution adapters.

Single source of truth for adapter-layer exceptions so no adapter module
defines a parallel copy.
"""
from __future__ import annotations


class OrderRejected(Exception):
    """Raised by an execution adapter when an order is rejected before it
    reaches the wire.

    Used by PolymarketUSAdapter (and future adapters) to reject orders at
    hard-lock or supervisor-armed gates.  The message must contain enough
    context to identify WHICH gate fired (e.g. "PHASE4", "PHASE5",
    "supervisor armed") so callers / tests can assert on the gate ordering.
    """
