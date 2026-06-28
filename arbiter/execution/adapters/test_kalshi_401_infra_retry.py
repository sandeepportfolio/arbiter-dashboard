"""Infra-401 / 5xx transient-retry tests for KalshiAdapter (P1 #5).

Root cause covered by this file
-------------------------------
The 8 observed "Kalshi API 401 authentication_error" failures are Kalshi-SIDE
infra responses (Envoy "service unavailable" / INTERNAL_SERVER_ERROR), NOT a
request-signing bug. They permanently burned the order leg because the
order-placement path RETURNED the status code instead of RAISING, so the
``@transient_retry`` / tenacity decorator never fired. The same flaw made the
kill-switch's order enumeration (``_list_all_open_orders``) return ``[]`` on a
transient 401 → UNDER-CANCEL during shutdown (resting orders left live).

These tests pin the corrected behaviour:

(a) infra-401 → retried, then succeeds.
(b) infra-503 → retried, then succeeds.
(c) signing-401 (INCORRECT_API_KEY_SIGNATURE) → immediate FAILED, NO retry.
(d) persistent infra-401 → fails fast within the wall-clock cap, breaker trips.
(e) ``_list_all_open_orders`` on transient 401 → retries, returns the real list.
(f) ``_list_all_open_orders`` persistent failure → RAISES (never silently []),
    so the kill switch fails CLOSED (``cancel_all`` logs list_failed).

Mock helpers mirror ``test_kalshi_adapter.py`` but add a *sequence* session so
the first attempt can differ from the retry.
"""
from __future__ import annotations

import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from arbiter.execution.adapters.kalshi import KalshiAdapter
from arbiter.execution.engine import OrderStatus


# ─── Fixtures ─────────────────────────────────────────────────────────────


def _config():
    cfg = SimpleNamespace()
    cfg.kalshi = SimpleNamespace(
        base_url="https://api.elections.kalshi.test/trade-api/v2",
    )
    return cfg


def _auth(authenticated: bool = True):
    auth = MagicMock()
    auth.is_authenticated = authenticated
    auth.get_headers = MagicMock(return_value={"Authorization": "test-sig"})
    return auth


def _circuit(can_execute: bool = True):
    circuit = MagicMock()
    circuit.can_execute = MagicMock(return_value=can_execute)
    circuit.record_success = MagicMock()
    circuit.record_failure = MagicMock()
    return circuit


def _rate_limiter():
    rl = MagicMock()
    rl.acquire = AsyncMock(return_value=None)
    rl.apply_retry_after = MagicMock(return_value=3.0)
    return rl


def _resp(status: int, body_text: str, headers=None):
    resp = MagicMock()
    resp.status = status
    resp.text = AsyncMock(return_value=body_text)
    resp.headers = headers or {}
    return resp


def _seq_session_post(responses):
    """Session whose successive .post() calls return ``responses`` in order.

    ``responses`` is a list of (status, body_text) tuples. The Nth POST returns
    the Nth response; the final response repeats for any extra calls.
    """
    session = MagicMock()
    state = {"i": 0}

    def _post(*args, **kwargs):
        idx = min(state["i"], len(responses) - 1)
        state["i"] += 1
        status, body = responses[idx]
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=_resp(status, body))
        cm.__aexit__ = AsyncMock(return_value=False)
        return cm

    session.post = MagicMock(side_effect=_post)
    session._call_state = state
    return session


def _seq_session_get(responses):
    session = MagicMock()
    state = {"i": 0}

    def _get(*args, **kwargs):
        idx = min(state["i"], len(responses) - 1)
        state["i"] += 1
        status, body = responses[idx]
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=_resp(status, body))
        cm.__aexit__ = AsyncMock(return_value=False)
        return cm

    session.get = MagicMock(side_effect=_get)
    session._call_state = state
    return session


def _make_adapter(session, *, authenticated=True, can_execute=True):
    return KalshiAdapter(
        config=_config(),
        session=session,
        auth=_auth(authenticated),
        rate_limiter=_rate_limiter(),
        circuit=_circuit(can_execute),
    )


# Body fixtures matching the wire shapes seen from Kalshi/Envoy.
_INFRA_401_BODY = json.dumps({
    "error": {"code": "authentication_error", "message": "service unavailable"},
})
_INFRA_401_ENVOY_BODY = (
    "upstream connect error or disconnect/reset before headers. "
    "reset reason: connection failure"
)
_SIGNING_401_BODY = json.dumps({
    "error": {"code": "authentication_error", "message": "INCORRECT_API_KEY_SIGNATURE"},
})
_FILLED_BODY = json.dumps({
    "order": {
        "order_id": "K-OK",
        "status": "executed",
        "fill_count_fp": "10.00",
        "yes_price_dollars": "0.5500",
    },
})


# ─── (a) infra-401 → retried then succeeds ───────────────────────────────


@pytest.mark.asyncio
async def test_post_order_infra_401_retries_then_succeeds():
    session = _seq_session_post([(401, _INFRA_401_BODY), (200, _FILLED_BODY)])
    adapter = _make_adapter(session)
    order = await adapter.place_fok("ARB-INFRA-401", "T", "C", "yes", 0.55, 10)

    assert order.status == OrderStatus.FILLED, (
        f"infra-401 must be retried and succeed on attempt 2; got {order.status} "
        f"error={order.error!r}"
    )
    assert session.post.call_count == 2, (
        f"expected exactly 2 POSTs (1 infra-401 + 1 success), got {session.post.call_count}"
    )
    adapter.circuit.record_success.assert_called()


@pytest.mark.asyncio
async def test_post_order_envoy_503_text_body_retries_then_succeeds():
    """Envoy upstream-connect text body (5xx) is infra — must retry."""
    session = _seq_session_post([(503, _INFRA_401_ENVOY_BODY), (200, _FILLED_BODY)])
    adapter = _make_adapter(session)
    order = await adapter.place_fok("ARB-ENVOY", "T", "C", "yes", 0.55, 10)
    assert order.status == OrderStatus.FILLED
    assert session.post.call_count == 2


@pytest.mark.asyncio
async def test_post_order_reuses_same_client_order_id_across_retry():
    """Capital-safety invariant: the retried POST must reuse the SAME
    client_order_id (Kalshi dedups on it → no double-place)."""
    session = _seq_session_post([(401, _INFRA_401_BODY), (200, _FILLED_BODY)])
    adapter = _make_adapter(session)
    await adapter.place_fok("ARB-DEDUP", "T", "C", "yes", 0.55, 10)

    bodies = [c.kwargs["json"] for c in session.post.call_args_list]
    assert len(bodies) == 2
    assert bodies[0]["client_order_id"] == bodies[1]["client_order_id"], (
        "retry must reuse the same client_order_id so Kalshi dedups "
        f"(no double-place); got {bodies[0]['client_order_id']!r} then "
        f"{bodies[1]['client_order_id']!r}"
    )


# ─── (b) infra-503 → retried ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_post_order_infra_503_retries_then_succeeds():
    session = _seq_session_post([(503, "service unavailable"), (200, _FILLED_BODY)])
    adapter = _make_adapter(session)
    order = await adapter.place_fok("ARB-503", "T", "C", "yes", 0.55, 10)
    assert order.status == OrderStatus.FILLED
    assert session.post.call_count == 2


@pytest.mark.asyncio
async def test_post_order_500_retries_then_succeeds():
    session = _seq_session_post([(500, "INTERNAL_SERVER_ERROR"), (200, _FILLED_BODY)])
    adapter = _make_adapter(session)
    order = await adapter.place_fok("ARB-500", "T", "C", "yes", 0.55, 10)
    assert order.status == OrderStatus.FILLED
    assert session.post.call_count == 2


# ─── (c) signing-401 → immediate FAILED, no retry ────────────────────────


@pytest.mark.asyncio
async def test_post_order_signing_401_is_not_retried():
    """A genuine signing 401 (INCORRECT_API_KEY_SIGNATURE) is NON-transient:
    retrying cannot help and only wastes the naked window. Must FAIL on the
    first attempt with no retry."""
    session = _seq_session_post([(401, _SIGNING_401_BODY), (200, _FILLED_BODY)])
    adapter = _make_adapter(session)
    order = await adapter.place_fok("ARB-SIGN", "T", "C", "yes", 0.55, 10)

    assert order.status == OrderStatus.FAILED
    assert session.post.call_count == 1, (
        f"signing-401 must NOT be retried, got {session.post.call_count} POSTs"
    )
    adapter.circuit.record_failure.assert_called()


@pytest.mark.asyncio
async def test_post_order_forbidden_403_is_not_retried():
    """403 forbidden is a genuine auth/permission failure — not transient."""
    body = json.dumps({"error": {"code": "forbidden", "message": "forbidden"}})
    session = _seq_session_post([(403, body), (200, _FILLED_BODY)])
    adapter = _make_adapter(session)
    order = await adapter.place_fok("ARB-403", "T", "C", "yes", 0.55, 10)
    assert order.status == OrderStatus.FAILED
    assert session.post.call_count == 1


# ─── (d) persistent infra-401 → fails fast within wall-clock cap ─────────


@pytest.mark.asyncio
async def test_post_order_persistent_infra_401_fails_fast_and_trips_breaker():
    """A persistent infra-401 must fail fast (within the wall-clock cap, not
    spin forever) and record a circuit failure so the breaker can trip."""
    session = _seq_session_post([(401, _INFRA_401_BODY)])  # always 401
    adapter = _make_adapter(session)

    t0 = time.monotonic()
    order = await adapter.place_fok("ARB-PERSIST", "T", "C", "yes", 0.55, 10)
    elapsed = time.monotonic() - t0

    assert order.status == OrderStatus.FAILED
    # Bounded attempts (2-3), not unbounded.
    assert 1 < session.post.call_count <= 3, (
        f"expected 2-3 bounded attempts, got {session.post.call_count}"
    )
    # Wall-clock cap: a persistent 401 must not block longer than ~5s.
    assert elapsed < 5.0, (
        f"persistent infra-401 must fail fast within the wall-clock cap; "
        f"took {elapsed:.2f}s"
    )
    adapter.circuit.record_failure.assert_called()


# ─── (e) _list_all_open_orders transient 401 → retries, returns real list ─


@pytest.mark.asyncio
async def test_list_all_open_orders_infra_401_retries_then_returns_list():
    """Kill-switch read path: a transient infra-401 on the order enumeration
    must be retried, not swallowed into an empty list (which would
    UNDER-CANCEL resting orders during shutdown)."""
    real_orders_body = json.dumps({
        "orders": [
            {
                "order_id": "K-RESTING-1",
                "client_order_id": "ARB-1-YES-aaa",
                "status": "resting",
                "ticker": "T",
                "side": "yes",
                "count_fp": "10",
                "yes_price_dollars": "0.5500",
            },
        ],
    })
    session = _seq_session_get([(401, _INFRA_401_BODY), (200, real_orders_body)])
    adapter = _make_adapter(session)

    orders = await adapter._list_all_open_orders()

    assert len(orders) == 1, (
        f"transient 401 must be retried and the real resting order returned; "
        f"got {orders!r}"
    )
    assert orders[0].order_id == "K-RESTING-1"
    assert session.get.call_count == 2


# ─── (f) _list_all_open_orders persistent failure → raises (fail closed) ──


@pytest.mark.asyncio
async def test_list_all_open_orders_persistent_infra_401_raises_not_empty():
    """Persistent infra-401 on the read path must RAISE rather than silently
    return [] — so cancel_all fails CLOSED (logs list_failed) instead of
    reporting "no resting orders" and leaving them live."""
    session = _seq_session_get([(401, _INFRA_401_BODY)])  # always 401
    adapter = _make_adapter(session)

    with pytest.raises(Exception):
        await adapter._list_all_open_orders()

    assert 1 < session.get.call_count <= 3, (
        f"expected 2-3 bounded retries before raising, got {session.get.call_count}"
    )


@pytest.mark.asyncio
async def test_cancel_all_fails_closed_on_persistent_list_error():
    """End-to-end fail-closed: when _list_all_open_orders raises persistently,
    cancel_all must NOT silently report success — it returns [] only via the
    list_failed branch (an error signal), never via the 'no open orders'
    happy path. We assert the raised error reaches cancel_all's guard."""
    session = _seq_session_get([(401, _INFRA_401_BODY)])
    adapter = _make_adapter(session)

    # cancel_all wraps the list call in try/except and logs list_failed; it
    # must reach that branch (i.e. _list_all_open_orders raised), not the
    # "if not open_orders: return []" happy path that would mean a clean
    # empty book.
    raised = {"hit": False}
    orig = adapter._list_all_open_orders

    async def _wrapped():
        try:
            return await orig()
        except Exception:
            raised["hit"] = True
            raise

    adapter._list_all_open_orders = _wrapped
    result = await adapter.cancel_all()
    assert raised["hit"], (
        "cancel_all must invoke _list_all_open_orders and let it RAISE on "
        "persistent failure (fail closed), not receive a silent empty list"
    )
    assert result == []
