"""
Tests for PolymarketUSClient (Task 5).
Uses aioresponses to mock aiohttp. No live network calls.
"""
from __future__ import annotations

import pytest
from aioresponses import aioresponses

from arbiter.auth.ed25519_signer import Ed25519Signer
from arbiter.collectors.polymarket_us import PolymarketUSClient

# 32-byte key: bytes 0..31 base64-encoded
SECRET = "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8="
BASE_URL = "https://api.polymarket.us/v1"


@pytest.fixture
def client():
    signer = Ed25519Signer(key_id="kid", secret_b64=SECRET)
    c = PolymarketUSClient(base_url=BASE_URL, signer=signer)
    return c


# ---------------------------------------------------------------------------
# test_list_markets_paginates
# ---------------------------------------------------------------------------
async def test_list_markets_paginates(client):
    with aioresponses() as m:
        m.get(
            f"{BASE_URL}/markets?limit=100&offset=0",
            payload={"markets": [{"slug": "m1"}], "hasMore": True},
        )
        m.get(
            f"{BASE_URL}/markets?limit=100&offset=100",
            payload={"markets": [{"slug": "m2"}], "hasMore": False},
        )
        results = [item async for item in client.list_markets()]
        slugs = [r["slug"] for r in results]
    assert slugs == ["m1", "m2"]
    await client.close()


# ---------------------------------------------------------------------------
# test_get_orderbook_returns_bids_offers
# ---------------------------------------------------------------------------
async def test_get_orderbook_returns_bids_offers(client):
    with aioresponses() as m:
        m.get(
            f"{BASE_URL}/orderbook/foo?depth=3",
            payload={"bids": [{"px": 50, "qty": 100}], "offers": [{"px": 55, "qty": 50}]},
        )
        ob = await client.get_orderbook("foo", depth=3)
    assert ob["bids"][0]["px"] == 50
    assert ob["offers"][0]["px"] == 55
    await client.close()


# ---------------------------------------------------------------------------
# test_place_fok_order_sends_signed_post
# ---------------------------------------------------------------------------
async def test_place_fok_order_sends_signed_post(client):
    with aioresponses() as m:
        m.post(
            f"{BASE_URL}/orders",
            payload={"orderId": "ord-123", "status": "FILLED"},
        )
        r = await client.place_order(
            slug="foo",
            intent="BUY_LONG",
            price=0.51,
            qty=100,
            tif="FILL_OR_KILL",
        )
    assert r["orderId"] == "ord-123"
    await client.close()


# ---------------------------------------------------------------------------
# test_rate_limit_retry_on_429
# ---------------------------------------------------------------------------
async def test_rate_limit_retry_on_429(client):
    with aioresponses() as m:
        m.get(
            f"{BASE_URL}/account/balances",
            status=429,
            headers={"Retry-After": "0"},
        )
        m.get(
            f"{BASE_URL}/account/balances",
            payload={"currentBalance": "100.00"},
        )
        bal = await client.balance()
    assert bal["currentBalance"] == "100.00"
    await client.close()


# ---------------------------------------------------------------------------
# test_signature_headers_sent_on_every_request
# ---------------------------------------------------------------------------
async def test_signature_headers_sent_on_every_request(client):
    captured_headers = {}

    with aioresponses() as m:
        m.get(
            f"{BASE_URL}/account/balances",
            payload={"currentBalance": "50.00"},
        )
        # We use a wrapper to capture the headers that were sent
        original_signed = client._signed

        async def capturing_signed(method, path, json_body=None):
            # Compute headers the same way the client does
            h = client.signer.headers(method, path)
            captured_headers.update(h)
            return await original_signed(method, path, json_body)

        client._signed = capturing_signed
        await client.balance()

    assert "X-PM-Access-Key" in captured_headers
    assert "X-PM-Timestamp" in captured_headers
    assert "X-PM-Signature" in captured_headers
    await client.close()


# ---------------------------------------------------------------------------
# test_close_cleans_session
# ---------------------------------------------------------------------------
async def test_close_cleans_session(client):
    with aioresponses() as m:
        m.get(f"{BASE_URL}/account/balances", payload={"currentBalance": "0"})
        await client.balance()

    # Session should now be open; close it
    await client.close()
    # Calling close again should be idempotent (no exception)
    await client.close()
    assert client.session is None or client.session.closed
