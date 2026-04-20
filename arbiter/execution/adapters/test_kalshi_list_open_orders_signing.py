"""Signature-message regression tests for KalshiAdapter._list_orders (Plan 04-09 G-1).

G-1 production bug (2026-04-20 demo UAT Test 6): KalshiAdapter._list_orders signs
the PATH with the querystring attached (``/portfolio/orders?status=resting``),
which Kalshi PSS signature verification rejects with HTTP 401
INCORRECT_API_KEY_SIGNATURE. The downstream impact: SAFE-01 kill-switch against
real demo/prod silently returns 0 from cancel_all because _list_all_open_orders
fails upstream.

The fix: sign the querystring-FREE path (``/portfolio/orders``) while still
sending the request to the querystring-bearing URL. This mirrors what
``_post_order`` (line 285) already does correctly.

These tests assert the signed-message shape directly. They should FAIL before
the fix is applied (RED) and PASS after (GREEN). Test 3 already passes as a
regression guard so future refactors cannot accidentally introduce the same
bug on other call sites.

Mock helpers are copy-adapted (NOT imported) from
``test_kalshi_place_resting_limit.py`` so this file stays standalone and the
upstream helpers remain private.
"""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from arbiter.execution.adapters.kalshi import KalshiAdapter
from arbiter.execution.engine import OrderStatus


# --- Fixture helpers (copy-adapted from test_kalshi_place_resting_limit.py) ---


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


def _session_with_get(status: int, body_text: str, headers=None):
    """Mock session whose GET returns the given status+body. Records call_args."""
    session = MagicMock()
    resp = MagicMock()
    resp.status = status
    resp.text = AsyncMock(return_value=body_text)
    resp.headers = headers or {}
    session.get.return_value.__aenter__ = AsyncMock(return_value=resp)
    session.get.return_value.__aexit__ = AsyncMock(return_value=False)
    return session


def _session_with_post(status: int, body_text: str, headers=None):
    session = MagicMock()
    resp = MagicMock()
    resp.status = status
    resp.text = AsyncMock(return_value=body_text)
    resp.headers = headers or {}
    session.post.return_value.__aenter__ = AsyncMock(return_value=resp)
    session.post.return_value.__aexit__ = AsyncMock(return_value=False)
    return session


def _make_adapter(session, *, authenticated: bool = True, can_execute: bool = True):
    return KalshiAdapter(
        config=_config(),
        session=session,
        auth=_auth(authenticated),
        rate_limiter=_rate_limiter(),
        circuit=_circuit(can_execute),
    )


# --- Tests -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_all_open_orders_signs_without_querystring():
    """G-1 regression guard: _list_all_open_orders() must sign the bare
    ``/trade-api/v2/portfolio/orders`` path, NOT the querystring-bearing form.
    The querystring belongs on the URL (for routing) but MUST be stripped from
    the signed message (Kalshi PSS signature verification).
    """
    body = json.dumps({"orders": []})
    session = _session_with_get(200, body)
    adapter = _make_adapter(session)

    result = await adapter._list_all_open_orders()

    # Sanity: adapter returned empty list for empty response.
    assert result == []

    # CORE G-1 ASSERTION: the signed path must NOT contain a querystring.
    assert adapter.auth.get_headers.called, (
        "auth.get_headers was never called — test setup is wrong"
    )
    called_args = adapter.auth.get_headers.call_args.args
    assert called_args == ("GET", "/trade-api/v2/portfolio/orders"), (
        f"G-1 signature bug: auth.get_headers was called with {called_args!r}; "
        f"expected ('GET', '/trade-api/v2/portfolio/orders') — the querystring "
        f"(?status=...) must NOT appear in the signed path."
    )

    # The URL that session.get received MUST include ?status=resting (the
    # querystring is for request routing, not signature).
    assert session.get.called
    get_args, _get_kwargs = session.get.call_args
    url_arg = get_args[0]
    assert "?status=resting" in url_arg, (
        f"URL must carry ?status=resting so Kalshi knows what to return; "
        f"got {url_arg!r}"
    )


@pytest.mark.asyncio
async def test_list_open_orders_by_client_id_signs_without_querystring():
    """G-1 regression guard: list_open_orders_by_client_id also routes through
    _list_orders and MUST sign the querystring-free path.
    """
    body = json.dumps({"orders": []})
    session = _session_with_get(200, body)
    adapter = _make_adapter(session)

    await adapter.list_open_orders_by_client_id("ARB-TEST-")

    assert adapter.auth.get_headers.called
    called_args = adapter.auth.get_headers.call_args.args
    # The path argument (second positional) MUST NOT contain a '?'.
    assert len(called_args) >= 2, (
        f"auth.get_headers expected (method, path[, body]); got {called_args!r}"
    )
    signed_path = called_args[1]
    assert "?" not in signed_path, (
        f"G-1 signature bug: signed path must be querystring-free; "
        f"got {signed_path!r}"
    )
    assert signed_path == "/trade-api/v2/portfolio/orders", (
        f"Unexpected signed path: {signed_path!r}"
    )


@pytest.mark.asyncio
async def test_post_order_still_signs_bare_orders_path():
    """Regression sibling: place_fok -> _post_order already signs the bare
    ``/trade-api/v2/portfolio/orders`` path (no querystring). This test exists
    so any future refactor that accidentally introduces querystring-in-signed-
    path on POST is caught here next to the G-1 fix.
    """
    body = json.dumps({
        "order": {
            "order_id": "K-FOK-SIG-1",
            "status": "executed",
            "fill_count_fp": "3.00",
            "yes_price_dollars": "0.5500",
        },
    })
    session = _session_with_post(200, body)
    adapter = _make_adapter(session)

    order = await adapter.place_fok(
        "ARB-SIG-1", "TICKER", "CAN", "yes", 0.55, 3,
    )

    assert order.status == OrderStatus.FILLED
    assert adapter.auth.get_headers.called
    # Scan every call to get_headers and assert NONE carry a querystring in path.
    for call in adapter.auth.get_headers.call_args_list:
        args = call.args
        if len(args) >= 2:
            signed_path = args[1]
            assert "?" not in signed_path, (
                f"POST signed path contains querystring (forbidden); "
                f"got {signed_path!r}"
            )
    # Additionally, explicit shape check on the specific call (the POST one).
    signed_paths = [c.args[1] for c in adapter.auth.get_headers.call_args_list if len(c.args) >= 2]
    assert "/trade-api/v2/portfolio/orders" in signed_paths, (
        f"Expected at least one signed path == '/trade-api/v2/portfolio/orders'; "
        f"got {signed_paths!r}"
    )
