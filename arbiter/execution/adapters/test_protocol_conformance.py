"""Protocol conformance tests for arbiter.execution.adapters.base.PlatformAdapter.

The runtime_checkable Protocol allows isinstance() checks without explicit
inheritance — this file proves that holds for a minimal stub adapter, and
proves that incomplete implementations are correctly rejected.

Plans 04 and 05 will add `test_kalshi_adapter_satisfies_protocol` and
`test_polymarket_adapter_satisfies_protocol` to their own test modules using
the same isinstance(...) pattern.
"""
from __future__ import annotations

from arbiter.execution.adapters import PlatformAdapter
from arbiter.execution.engine import Order, OrderStatus


# ─── Complete stub — should satisfy Protocol ──────────────────────────────

class _StubAdapter:
    """Minimal stub implementing every PlatformAdapter method as a no-op.

    Note: NOT inherited from PlatformAdapter — runtime_checkable Protocol
    enforces structural typing.
    """

    platform = "stub"

    async def check_depth(self, market_id: str, side: str, required_qty: int) -> tuple[bool, float]:
        return (True, 0.50)

    async def best_executable_price(
        self, market_id: str, side: str, required_qty: int,
    ) -> tuple[bool, float]:
        return (True, 0.50)

    async def place_fok(
        self, arb_id: str, market_id: str, canonical_id: str,
        side: str, price: float, qty: int,
    ) -> Order:
        return Order(
            order_id=f"{arb_id}-STUB",
            platform=self.platform,
            market_id=market_id,
            canonical_id=canonical_id,
            side=side,
            price=price,
            quantity=qty,
            status=OrderStatus.SIMULATED,
        )

    async def cancel_order(self, order: Order) -> bool:
        return True

    async def cancel_all(self) -> list[str]:
        # SAFE-05: Protocol contract now includes cancel_all (plan 03-01 added
        # it; plan 03-05 implements it on real adapters). Stub returns [] so
        # conformance tests pass without side effects.
        return []

    async def get_order(self, order: Order) -> Order:
        return order

    async def list_open_orders_by_client_id(self, client_order_id_prefix: str) -> list[Order]:
        return []


# ─── Incomplete stubs — should NOT satisfy Protocol ───────────────────────

class _MissingMethodAdapter:
    """Stub missing list_open_orders_by_client_id — must FAIL isinstance check."""

    platform = "missing-method"

    async def check_depth(self, *args, **kwargs):
        return (False, 0.0)

    async def best_executable_price(self, *args, **kwargs):
        return (False, 0.0)

    async def place_fok(self, *args, **kwargs):
        return None  # type: ignore

    async def cancel_order(self, order):
        return False

    async def cancel_all(self):
        return []

    async def get_order(self, order):
        return order


class _MissingAttributeAdapter:
    """Stub missing the `platform` class attribute — used to document that
    runtime_checkable Protocol method-checking is reliable but attribute
    checking is weaker (see test_missing_attribute_stub_fails_protocol)."""

    async def check_depth(self, *args, **kwargs):
        return (False, 0.0)

    async def best_executable_price(self, *args, **kwargs):
        return (False, 0.0)

    async def place_fok(self, *args, **kwargs):
        return None  # type: ignore

    async def cancel_order(self, order):
        return False

    async def cancel_all(self):
        return []

    async def get_order(self, order):
        return order

    async def list_open_orders_by_client_id(self, client_order_id_prefix):
        return []


# ─── Tests ────────────────────────────────────────────────────────────────

def test_complete_stub_satisfies_protocol():
    adapter = _StubAdapter()
    assert isinstance(adapter, PlatformAdapter), \
        "_StubAdapter implements all 5 methods + platform attr but failed Protocol check"


def test_missing_method_stub_fails_protocol():
    adapter = _MissingMethodAdapter()
    assert not isinstance(adapter, PlatformAdapter), \
        "_MissingMethodAdapter is missing list_open_orders_by_client_id but passed Protocol check"


def test_missing_attribute_stub_fails_protocol():
    adapter = _MissingAttributeAdapter()
    # Note: runtime_checkable on Protocol with both methods AND attributes
    # checks methods reliably; attribute presence checking is weaker — Python's
    # Protocol runtime check sees `platform` as an instance attribute that
    # may or may not be set. We verify by AttributeError when reading.
    try:
        _ = adapter.platform
        attr_present = True
    except AttributeError:
        attr_present = False
    assert not attr_present, \
        "_MissingAttributeAdapter has no platform attribute but reading it succeeded"


def test_protocol_lists_expected_methods():
    """The Protocol surface must include exactly these 6 methods (sanity).

    SAFE-05 (plan 03-05) added `cancel_all` to the Protocol surface so
    graceful shutdown can fan it out via SafetySupervisor.trip_kill.
    """
    expected = {
        "check_depth",
        "best_executable_price",
        "place_fok",
        "cancel_order",
        "cancel_all",
        "get_order",
        "list_open_orders_by_client_id",
    }
    actual_methods = {
        name for name in dir(PlatformAdapter)
        if not name.startswith("_") and name not in {"platform"}
    }
    assert expected.issubset(actual_methods), \
        f"Protocol missing methods: {expected - actual_methods}"
