"""TDD for Polymarket US fill honesty (P0: never report a phantom fill).

The diagnosis found 25 polymarket legs recorded ``status='filled'`` with
``fill_qty=0`` — a phantom fill that produced unverifiable exposure. The
adapter already downgrades an *explicit* zero fill (venue confessed
``cumQuantity=0``) to CANCELLED. This pins the remaining gap: a bare ``FILLED``
with NO quantity field must NOT be reported as a confirmed fill — it is
unconfirmed and must go to SUBMITTED so the engine re-polls ``get_order`` for
the authoritative cumQuantity (verify-before-act), never assuming a fill.
"""
from __future__ import annotations

from types import SimpleNamespace

from arbiter.execution.adapters.polymarket_us import PolymarketUSAdapter
from arbiter.execution.engine import OrderStatus


def _order(response: dict):
    adapter = PolymarketUSAdapter(client=SimpleNamespace())
    return adapter._order_from_response(
        response, "ARB-1", "MKT", "CANON", "yes", 0.55, 10
    )


def test_filled_with_no_quantity_field_is_unconfirmed_submitted():
    # Bare FILLED with NO cumQuantity/filledQty/size_matched: we must NOT
    # report a phantom FILLED-fill_qty-0. Downgrade to SUBMITTED so the engine
    # re-polls get_order for the authoritative fill rather than treating an
    # unconfirmed reply as a real (or non-) fill.
    o = _order({"id": "P1", "state": "FILLED"})
    assert o.status == OrderStatus.SUBMITTED, (
        f"bare FILLED-without-quantity must be unconfirmed (SUBMITTED), "
        f"got {o.status} fill_qty={o.fill_qty}"
    )
    assert o.fill_qty == 0.0


def test_filled_with_explicit_zero_fill_is_cancelled():
    # Venue confessed an explicit zero — definitively no fill → CANCELLED so the
    # secondary leg is suppressed (existing behavior, must not regress).
    o = _order({"id": "P2", "state": "FILLED", "filledQty": 0})
    assert o.status == OrderStatus.CANCELLED


def test_filled_with_real_cum_quantity_is_filled():
    o = _order({
        "id": "P3", "state": "FILLED",
        "executions": [{"order": {"cumQuantity": 10}}],
    })
    assert o.status == OrderStatus.FILLED
    assert o.fill_qty == 10.0


def test_filled_with_real_filledqty_is_filled():
    o = _order({"id": "P4", "state": "FILLED", "filledQty": 7})
    assert o.status == OrderStatus.FILLED
    assert o.fill_qty == 7.0
