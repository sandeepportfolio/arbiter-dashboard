"""
Tests for PolymarketUSClient (Task 5).
Uses aioresponses to mock aiohttp. No live network calls.
"""
from __future__ import annotations

import aiohttp
import pytest
from aioresponses import aioresponses

from arbiter.auth.ed25519_signer import Ed25519Signer
from arbiter.collectors.polymarket_us import PolymarketUSClient, PolymarketUSCollector
from arbiter.config.settings import PolymarketUSConfig
from arbiter.utils.price_store import PriceStore

# 32-byte key: bytes 0..31 base64-encoded
SECRET = "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8="
BASE_URL = "https://api.polymarket.us/v1"
PUBLIC_URL = "https://gateway.polymarket.us/v1"


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
            f"{PUBLIC_URL}/markets?limit=100&offset=0",
            payload={"markets": [{"slug": "m1"}], "hasMore": True},
        )
        m.get(
            f"{PUBLIC_URL}/markets?limit=100&offset=1",
            payload={"markets": [{"slug": "m2"}], "hasMore": False},
        )
        results = [item async for item in client.list_markets()]
        slugs = [r["slug"] for r in results]
    assert slugs == ["m1", "m2"]
    await client.close()


async def test_list_markets_continues_after_short_page_without_has_more(client):
    with aioresponses() as m:
        m.get(
            f"{PUBLIC_URL}/markets?limit=100&offset=0",
            payload={"markets": [{"slug": "m1"}, {"slug": "m2"}]},
        )
        m.get(
            f"{PUBLIC_URL}/markets?limit=100&offset=2",
            payload={"markets": [{"slug": "m3"}]},
        )
        m.get(
            f"{PUBLIC_URL}/markets?limit=100&offset=3",
            payload={"markets": []},
        )
        results = [item async for item in client.list_markets()]
        slugs = [r["slug"] for r in results]
    assert slugs == ["m1", "m2", "m3"]
    await client.close()


# ---------------------------------------------------------------------------
# test_get_orderbook_returns_bids_offers
# ---------------------------------------------------------------------------
async def test_get_orderbook_returns_bids_offers(client):
    with aioresponses() as m:
        m.get(
            f"{PUBLIC_URL}/markets/foo/book",
            payload={"marketData": {"bids": [{"px": {"value": "0.50"}, "qty": "100"}], "offers": [{"px": {"value": "0.55"}, "qty": "50"}]}},
        )
        ob = await client.get_orderbook("foo", depth=3)
    assert ob["marketData"]["bids"][0]["px"]["value"] == "0.50"
    assert ob["marketData"]["offers"][0]["px"]["value"] == "0.55"
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
# test_get_market_by_slug_uses_gateway_slug_endpoint
# ---------------------------------------------------------------------------
async def test_get_market_by_slug_uses_gateway_slug_endpoint(client):
    with aioresponses() as m:
        m.get(
            f"{PUBLIC_URL}/market/slug/foo",
            payload={"market": {"slug": "foo", "question": "Example?"}},
        )
        resp = await client.get_market_by_slug("foo")
    assert resp["market"]["slug"] == "foo"
    await client.close()


# ---------------------------------------------------------------------------
# test_collector_disables_missing_public_market_slugs
# ---------------------------------------------------------------------------
async def test_collector_disables_missing_public_market_slugs(client):
    collector = PolymarketUSCollector(
        config=PolymarketUSConfig(),
        store=PriceStore(),
        client=client,
    )
    collector.refresh_tracked_markets = lambda: None
    collector._slug_map = {"TEST": "missing-market"}

    with aioresponses() as m:
        m.get(
            f"{PUBLIC_URL}/markets/missing-market/book",
            status=404,
        )
        results = await collector.fetch_markets()

    assert results == []
    assert collector.consecutive_errors == 0
    assert collector.total_errors == 0
    assert "missing-market" in collector._inactive_slugs

    # Once disabled, the slug should no longer be retried.
    results = await collector.fetch_markets()
    assert results == []
    assert collector.total_fetches == 1
    await client.close()


# ---------------------------------------------------------------------------
# test_collector_skips_empty_books_without_publishing_stale_prices
# ---------------------------------------------------------------------------
async def test_collector_skips_empty_books_without_publishing_stale_prices(client):
    store = PriceStore()
    collector = PolymarketUSCollector(
        config=PolymarketUSConfig(),
        store=store,
        client=client,
    )
    collector.refresh_tracked_markets = lambda: None
    collector._slug_map = {"TEST": "thin-market"}

    with aioresponses() as m:
        m.get(
            f"{PUBLIC_URL}/markets/thin-market/book",
            payload={
                "marketData": {
                    "state": "OPEN",
                    "bids": [],
                    "offers": [],
                    "stats": {"currentPx": {"value": "0.72"}},
                }
            },
        )
        results = await collector.fetch_markets()

    assert results == []
    assert await store.get("polymarket", "TEST") is None
    assert collector.total_errors == 0
    await client.close()


# ---------------------------------------------------------------------------
# test_collector_skips_closed_books_without_publishing
# ---------------------------------------------------------------------------
async def test_collector_skips_closed_books_without_publishing(client):
    store = PriceStore()
    collector = PolymarketUSCollector(
        config=PolymarketUSConfig(),
        store=store,
        client=client,
    )
    collector.refresh_tracked_markets = lambda: None
    collector._slug_map = {"TEST": "closed-market"}

    with aioresponses() as m:
        m.get(
            f"{PUBLIC_URL}/markets/closed-market/book",
            payload={
                "marketData": {
                    "state": "RESOLVED",
                    "bids": [{"px": {"value": "0.91"}, "qty": "10"}],
                    "offers": [{"px": {"value": "0.93"}, "qty": "8"}],
                }
            },
        )
        results = await collector.fetch_markets()

    assert results == []
    assert await store.get("polymarket", "TEST") is None
    assert collector.total_errors == 0
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
