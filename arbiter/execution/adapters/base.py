"""PlatformAdapter Protocol — the structural contract every adapter implements.

Engine code (arbiter/execution/engine.py after Plan 06's refactor) depends ONLY
on this Protocol — never on concrete adapter classes. Adapters opt-in via
structural typing (no inheritance required); runtime_checkable enables
`isinstance(adapter, PlatformAdapter)` for conformance tests.

Note: Order / OrderStatus / ExecutionIncident dataclasses stay in
arbiter/execution/engine.py for now (PATTERNS.md — moving them risks
circular imports during the mid-refactor in Plan 06). Adapters import them
from `..engine`.
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..engine import Order


@runtime_checkable
class PlatformAdapter(Protocol):
    """Every platform adapter must implement these methods.

    The engine knows ONLY about this Protocol. Concrete adapters
    (KalshiAdapter, PolymarketAdapter) live in their own modules and
    are passed to ExecutionEngine via constructor injection.
    """

    platform: str  # "kalshi" | "polymarket"

    async def check_depth(
        self, market_id: str, side: str, required_qty: int,
    ) -> tuple[bool, float]:
        """Pre-trade liquidity check (EXEC-03).

        Returns (sufficient, best_price_at_depth).
          sufficient: True if visible book has >= required_qty at acceptable price
          best_price_at_depth: best price executable for required_qty (cents-per-share)

        Adapters MAY perform multiple platform-specific checks (e.g. Polymarket
        cross-checks `get_order_book` against `get_price` to defend against
        Pitfall 1 stale-book bug).
        """
        ...

    async def best_executable_price(
        self, market_id: str, side: str, required_qty: int,
    ) -> tuple[bool, float]:
        """Return the worst price needed to fully fill ``required_qty`` (FOK-safe).

        Walks the visible asks (for buys), summing depth until cumulative
        >= required_qty. Returns (fillable, worst_price):
          fillable: True if visible book contains >= required_qty across all levels
          worst_price: the highest ask price that the FOK must touch to absorb
                       required_qty. Returned as a probability in [0, 1].

        Distinct from ``check_depth`` (which returns the BEST price at the top
        of book). This method is what should be used as the FOK limit price
        — placing FOK at the top-of-book price when liquidity is fragmented
        across levels is the root cause of the Kalshi 409
        ``fill_or_kill_insufficient_resting_volume`` rejections.

        On any error returns (False, 0.0); never raises.
        """
        ...

    async def place_fok(
        self,
        arb_id: str,
        market_id: str,
        canonical_id: str,
        side: str,
        price: float,
        qty: int,
    ) -> Order:
        """Submit a fill-or-kill limit order (EXEC-01).

        FOK is enforced via the platform's native mechanism:
          Kalshi:    `time_in_force: "fill_or_kill"`
          Polymarket: `OrderType.FOK` on `post_order(signed, OrderType.FOK)`

        MUST return an Order in a terminal state from a healthy adapter
        (FILLED or CANCELLED) — FOK never leaves a partial. On adapter error,
        return Order with status=FAILED and `error` populated. NEVER raise
        across the engine/adapter boundary (engine is state-machine driven).
        """
        ...

    async def cancel_order(self, order: Order) -> bool:
        """Best-effort cancel — used for EXEC-05 timeout path and one-leg recovery."""
        ...

    async def cancel_all(self) -> list[str]:
        """Cancel every open order on this platform in a single batched operation.

        Returns list of cancelled order_ids (best-effort — empty list on adapter
        error, never raises). Used by SafetySupervisor.trip_kill() (SAFE-01) and
        graceful shutdown (SAFE-05).

        Kalshi:     DELETE /portfolio/orders/batched (20 orders per call, chunked).
        Polymarket: client.cancel_all() single SDK call.
        """
        ...

    async def get_order(self, order: Order) -> Order:
        """Query platform for current order state — used by startup reconciliation
        (arbiter/execution/recovery.py in Plan 06)."""
        ...

    async def list_open_orders_by_client_id(
        self, client_order_id_prefix: str,
    ) -> list[Order]:
        """List open orders whose client_order_id starts with the given prefix.

        Used by startup recovery to find orphaned orders from the previous
        process instance. Kalshi supports client_order_id natively; Polymarket
        does not — Polymarket adapters MAY implement this by listing all
        open orders and filtering by external bookkeeping (or returning [] if
        the adapter has no client_order_id concept).
        """
        ...
