"""
Tests for ForecastExClient and ForecastExCollector.

Uses aioresponses to mock the local IBKR Client Portal Web API gateway. No
live network calls or real IBKR credentials are required.
"""
from __future__ import annotations

import re

import aiohttp
import pytest
from aioresponses import aioresponses

from arbiter.collectors.forecastex import (
    ForecastExClient,
    ForecastExCollector,
    _amount_value,
)
from arbiter.config import settings as settings_mod
from arbiter.config.settings import ForecastExConfig
from arbiter.utils.price_store import PriceStore


GATEWAY = "https://localhost:5000/v1/api"
ACCOUNT = "DU1234567"


@pytest.fixture
def client():
    return ForecastExClient(
        gateway_url=GATEWAY,
        account_id=ACCOUNT,
        verify_ssl=False,
        paper_trading=True,
    )


# ── _amount_value normalization ────────────────────────────────────────────


def test_amount_value_handles_strings_and_dicts():
    assert _amount_value("12.5") == 12.5
    # IBKR sometimes wraps the price in {"value": "..."}; _amount_value extracts that.
    assert _amount_value({"value": "33.4"}) == 33.4
    assert _amount_value("C45") == 45.0
    assert _amount_value("H7.2") == 7.2
    assert _amount_value(None) == 0.0
    assert _amount_value("xyz") == 0.0
    assert _amount_value({"value": "bad"}) == 0.0


# ── ForecastExClient HTTP plumbing ─────────────────────────────────────────


async def test_client_account_balance_prefers_available_funds(client):
    with aioresponses() as m:
        m.get(re.compile(r".*/portfolio/accounts.*"), payload=[{"id": ACCOUNT}])
        m.get(
            re.compile(rf".*/portfolio/{ACCOUNT}/summary.*"),
            payload={
                "availablefunds": {"amount": "123.45", "currency": "USD"},
                "totalcashvalue": {"amount": "999.99"},
            },
        )
        bal = await client.account_balance()
    assert bal == 123.45
    await client.close()


async def test_client_account_balance_falls_back_to_total_cash(client):
    with aioresponses() as m:
        m.get(re.compile(r".*/portfolio/accounts.*"), payload=[])
        m.get(
            re.compile(rf".*/portfolio/{ACCOUNT}/summary.*"),
            payload={"totalcashvalue": {"amount": "77.0"}},
        )
        bal = await client.account_balance()
    assert bal == 77.0
    await client.close()


async def test_client_account_balance_returns_zero_when_no_keys(client):
    with aioresponses() as m:
        m.get(re.compile(r".*/portfolio/accounts.*"), payload=[])
        m.get(re.compile(rf".*/portfolio/{ACCOUNT}/summary.*"), payload={})
        bal = await client.account_balance()
    assert bal == 0.0
    await client.close()


async def test_client_account_balance_requires_account_id():
    c = ForecastExClient(gateway_url=GATEWAY, account_id="")
    with pytest.raises(RuntimeError, match="IBKR_ACCOUNT_ID is not configured"):
        await c.account_balance()
    await c.close()


async def test_client_market_snapshot_normalizes_list_response(client):
    with aioresponses() as m:
        m.get(
            re.compile(r".*/iserver/marketdata/snapshot.*"),
            payload=[
                {"31": "45", "84": "42", "86": "47", "7295": "10", "7296": "12"},
            ],
        )
        snap = await client.market_snapshot("12345")
    assert snap["86"] == "47"
    await client.close()


async def test_client_market_snapshot_returns_empty_when_warming(client):
    with aioresponses() as m:
        m.get(re.compile(r".*/iserver/marketdata/snapshot.*"), payload=[])
        snap = await client.market_snapshot("99999")
    # Empty list payload wraps to {"items": []}; collector treats missing
    # bid/ask as no quote and returns None — both behaviors are tested
    # separately. Here we just confirm we don't blow up.
    assert isinstance(snap, dict)
    assert not snap.get("31")
    await client.close()


async def test_client_place_order_buy_only(client):
    with aioresponses() as m:
        m.post(
            re.compile(rf".*/iserver/account/{ACCOUNT}/orders.*"),
            payload={"order_id": "abc123", "order_status": "Submitted"},
        )
        resp = await client.place_order(
            conid="111", side="BUY", price=0.55, quantity=10, tif="IOC",
        )
    assert resp["order_id"] == "abc123"
    await client.close()


async def test_client_place_order_rejects_sell(client):
    with pytest.raises(ValueError, match="only BUY is supported"):
        await client.place_order(
            conid="111", side="SELL", price=0.55, quantity=10,
        )
    await client.close()


async def test_client_auth_401_raises_with_reauth_hint(client):
    with aioresponses() as m:
        m.get(
            re.compile(r".*/portfolio/accounts.*"),
            status=401,
            body="session expired",
        )
        with pytest.raises(RuntimeError, match="re-authenticate via /sso"):
            await client.accounts()
    await client.close()


async def test_client_429_retries_then_succeeds(client):
    with aioresponses() as m:
        m.get(
            re.compile(r".*/portfolio/accounts.*"),
            status=429,
            headers={"Retry-After": "0"},
        )
        m.get(
            re.compile(r".*/portfolio/accounts.*"),
            payload=[{"id": ACCOUNT}],
        )
        result = await client.accounts()
    assert result == {"items": [{"id": ACCOUNT}]}
    await client.close()


async def test_client_429_exhausts_retries(client):
    with aioresponses() as m:
        for _ in range(3):
            m.get(
                re.compile(r".*/portfolio/accounts.*"),
                status=429,
                headers={"Retry-After": "0"},
            )
        with pytest.raises(RuntimeError, match="rate-limit retry exhausted"):
            await client.accounts()
    await client.close()


async def test_client_500_records_circuit_failure(client):
    initial_failures = client.circuit._failure_count
    with aioresponses() as m:
        m.get(
            re.compile(r".*/portfolio/accounts.*"),
            status=502,
            body="upstream broke",
        )
        with pytest.raises(aiohttp.ClientResponseError):
            await client.accounts()
    assert client.circuit._failure_count == initial_failures + 1
    await client.close()


async def test_client_cancel_order_sends_delete(client):
    with aioresponses() as m:
        m.delete(
            re.compile(rf".*/iserver/account/{ACCOUNT}/order/order-7.*"),
            payload={"ok": True},
        )
        resp = await client.cancel_order("order-7")
    assert resp == {"ok": True}
    await client.close()


async def test_client_get_order_404_returns_empty(client):
    with aioresponses() as m:
        m.get(
            re.compile(r".*/iserver/account/order/status/missing.*"),
            status=404,
            body="",
        )
        resp = await client.get_order("missing")
    assert resp == {}
    await client.close()


async def test_client_cancel_all_open_orders_iterates(client):
    with aioresponses() as m:
        m.get(
            re.compile(r".*/iserver/account/orders.*"),
            payload={"orders": [{"orderId": "1"}, {"orderId": "2"}]},
        )
        m.delete(
            re.compile(rf".*/iserver/account/{ACCOUNT}/order/1.*"),
            payload={},
        )
        m.delete(
            re.compile(rf".*/iserver/account/{ACCOUNT}/order/2.*"),
            payload={},
        )
        cancelled = await client.cancel_all_open_orders()
    assert sorted(cancelled) == ["1", "2"]
    await client.close()


# ── ForecastExCollector polling + parsing ─────────────────────────────────


@pytest.fixture
def patched_market_map(monkeypatch):
    fake = {
        "EXAMPLE_2026_OUTCOME": {
            "kalshi": "KX-EXAMPLE",
            "polymarket": "example-poly",
            "forecastex": "111222",
            "status": "confirmed",
            "mapping_score": 0.91,
        },
        "NO_FORECASTEX_HERE": {
            "kalshi": "KX-OTHER",
            "polymarket": "other-poly",
            "forecastex": "",
            "status": "confirmed",
        },
    }
    monkeypatch.setattr(settings_mod, "MARKET_MAP", fake)
    import arbiter.collectors.forecastex as fcst_mod

    monkeypatch.setattr(fcst_mod, "MARKET_MAP", fake)
    return fake


async def test_collector_tracks_only_mappings_with_forecastex_id(patched_market_map):
    store = PriceStore()
    client = ForecastExClient(gateway_url=GATEWAY, account_id=ACCOUNT)
    collector = ForecastExCollector(
        config=ForecastExConfig(), store=store, client=client,
    )
    assert "EXAMPLE_2026_OUTCOME" in collector._conid_map
    assert "NO_FORECASTEX_HERE" not in collector._conid_map
    await client.close()


async def test_collector_normalizes_cent_prices(patched_market_map):
    store = PriceStore()
    client = ForecastExClient(gateway_url=GATEWAY, account_id=ACCOUNT)
    collector = ForecastExCollector(
        config=ForecastExConfig(), store=store, client=client,
    )
    with aioresponses() as m:
        m.get(
            re.compile(r".*/iserver/marketdata/snapshot.*"),
            # Cent quotes; collector must rescale to [0, 1]
            payload=[{"31": "45", "84": "42", "86": "47", "7295": "5", "7296": "7"}],
        )
        results = await collector.fetch_markets()
    assert len(results) == 1
    pp = results[0]
    assert pp.platform == "forecastex"
    assert pp.canonical_id == "EXAMPLE_2026_OUTCOME"
    assert 0.46 < pp.yes_ask < 0.48
    assert 0.41 < pp.yes_bid < 0.43
    # NO ask is the complement of YES bid.
    assert abs(pp.no_ask - (1.0 - pp.yes_bid)) < 1e-9
    # Fee rate stored so position sizer doesn't re-compute.
    assert pp.fee_rate == 0.005
    await client.close()


async def test_collector_skips_empty_snapshot(patched_market_map):
    store = PriceStore()
    client = ForecastExClient(gateway_url=GATEWAY, account_id=ACCOUNT)
    collector = ForecastExCollector(
        config=ForecastExConfig(), store=store, client=client,
    )
    with aioresponses() as m:
        m.get(re.compile(r".*/iserver/marketdata/snapshot.*"), payload=[])
        results = await collector.fetch_markets()
    assert results == []
    await client.close()


async def test_collector_handles_dollar_prices(patched_market_map):
    """Some IBKR feeds quote already in dollars; collector must clamp [0,1]."""
    store = PriceStore()
    client = ForecastExClient(gateway_url=GATEWAY, account_id=ACCOUNT)
    collector = ForecastExCollector(
        config=ForecastExConfig(), store=store, client=client,
    )
    with aioresponses() as m:
        m.get(
            re.compile(r".*/iserver/marketdata/snapshot.*"),
            payload=[{"31": "0.5", "84": "0.48", "86": "0.52", "7295": "9", "7296": "9"}],
        )
        results = await collector.fetch_markets()
    assert len(results) == 1
    assert results[0].yes_ask == 0.52


async def test_collector_disables_404_conids(patched_market_map):
    store = PriceStore()
    client = ForecastExClient(gateway_url=GATEWAY, account_id=ACCOUNT)
    collector = ForecastExCollector(
        config=ForecastExConfig(), store=store, client=client,
    )
    with aioresponses() as m:
        m.get(re.compile(r".*/iserver/marketdata/snapshot.*"), status=404, body="")
        await collector.fetch_markets()
    assert "111222" in collector._inactive_conids
    await client.close()


def test_is_tradeable_snapshot_recognises_empty_parent_event(client):
    # FORECASTX parent event snapshot — has metadata but no bid/ask/last.
    parent = {
        "55": "HORC",
        "conidEx": "733131966",
        "conid": 733131966,
        "7295": "0.00",
        "7283": "N/A",
    }
    assert ForecastExClient.is_tradeable_snapshot(parent) is False

    # Tradable child — at least one of 31/84/86 is positive.
    child = {"31": "0.45", "84": "0.44", "86": "0.46", "7295": "10"}
    assert ForecastExClient.is_tradeable_snapshot(child) is True

    # Half-warmed — last is "C" (close marker, no value) but ask is set.
    half = {"31": "Cnone", "84": "0", "86": "0.50"}
    assert ForecastExClient.is_tradeable_snapshot(half) is True

    # Bad input types — must not raise.
    assert ForecastExClient.is_tradeable_snapshot({}) is False
    assert ForecastExClient.is_tradeable_snapshot(None) is False  # type: ignore[arg-type]


async def test_collector_disables_parent_event_conids_after_probes(patched_market_map):
    """Parent FORECASTX event conids return only metadata. Polling them
    forever burns rate-limit budget; after 3 non-tradeable snapshots the
    collector must mark the conid inactive and stop probing.
    """
    store = PriceStore()
    client = ForecastExClient(gateway_url=GATEWAY, account_id=ACCOUNT)
    collector = ForecastExCollector(
        config=ForecastExConfig(), store=store, client=client,
    )
    parent_snapshot = [{
        "55": "HORC",
        "conidEx": "733131966",
        "conid": 733131966,
        "7295": "0.00",
    }]
    with aioresponses() as m:
        for _ in range(4):
            m.get(
                re.compile(r".*/iserver/marketdata/snapshot.*"),
                payload=parent_snapshot,
            )
        for _ in range(3):
            await collector.fetch_markets()
    # The patched_market_map fixture binds conid "111222" to the canonical id.
    assert "111222" in collector._inactive_conids
    assert collector._parent_probe_counts["111222"] >= 3
    await client.close()


async def test_resolve_event_children_returns_empty_when_all_endpoints_fail(client):
    """IBKR returns 503 for FORECASTX EC info on weekends; the resolver must
    swallow those failures and return [] rather than raising.
    """
    with aioresponses() as m:
        # Every /iserver/secdef/info call returns 503.
        m.post(re.compile(r".*/iserver/secdef/info.*"), status=503, body="", repeat=True)
        m.get(re.compile(r".*/iserver/secdef/info.*"), status=503, body="", repeat=True)
        # /trsrv/secdef returns a parent entry with no ticker, so the search
        # fallback won't trigger either.
        m.get(re.compile(r".*/trsrv/secdef.*"), payload={"secdef": [{"conid": 733131966}]})
        # secdef/search fallback returns just the parent itself.
        m.post(
            re.compile(r".*/iserver/secdef/search.*"),
            payload=[{"conid": "733131966", "symbol": "HORC"}],
        )
        children = await client.resolve_event_children("733131966")
    assert children == []
    await client.close()


async def test_resolve_event_children_returns_children_when_secdef_info_works(client):
    """When IBKR's secdef/info returns children for a working month, the
    resolver returns them as conid+right+strike dicts.
    """
    with aioresponses() as m:
        # First few months 503, then NOV26 returns two children (Y and N).
        m.post(re.compile(r".*/iserver/secdef/info.*"), status=503, body="", repeat=True)
        m.get(
            re.compile(r".*/iserver/secdef/info.*"),
            payload={"items": [
                {"conid": "733131967", "right": "Y", "strike": 0.5},
                {"conid": "733131968", "right": "N", "strike": 0.5},
            ]},
            repeat=True,
        )
        children = await client.resolve_event_children("733131966", months=("NOV26",))
    # Resolver collects up to the first month that returns a non-empty list.
    assert len(children) == 2
    conids = {c["conid"] for c in children}
    assert conids == {"733131967", "733131968"}
    rights = {c["right"] for c in children}
    assert "Y" in rights and "N" in rights
    await client.close()


async def test_collector_fetch_balance_returns_available_funds(patched_market_map):
    store = PriceStore()
    client = ForecastExClient(gateway_url=GATEWAY, account_id=ACCOUNT)
    collector = ForecastExCollector(
        config=ForecastExConfig(account_id=ACCOUNT),
        store=store, client=client,
    )
    with aioresponses() as m:
        m.get(re.compile(r".*/portfolio/accounts.*"), payload=[])
        m.get(
            re.compile(rf".*/portfolio/{ACCOUNT}/summary.*"),
            payload={"availablefunds": {"amount": "215.00"}},
        )
        bal = await collector.fetch_balance()
    assert bal == 215.00
    await client.close()


async def test_collector_fetch_balance_returns_none_when_no_account():
    store = PriceStore()
    client = ForecastExClient(gateway_url=GATEWAY, account_id="")
    collector = ForecastExCollector(
        config=ForecastExConfig(account_id=""),
        store=store, client=client,
    )
    assert await collector.fetch_balance() is None
    await client.close()


async def test_collector_fetch_balance_propagates_errors(patched_market_map):
    store = PriceStore()
    client = ForecastExClient(gateway_url=GATEWAY, account_id=ACCOUNT)
    collector = ForecastExCollector(
        config=ForecastExConfig(account_id=ACCOUNT),
        store=store, client=client,
    )
    with aioresponses() as m:
        m.get(re.compile(r".*/portfolio/accounts.*"), status=500, body="boom")
        with pytest.raises(Exception):
            await collector.fetch_balance()
    await client.close()


def test_collector_balance_source_set():
    store = PriceStore()
    client = ForecastExClient(gateway_url=GATEWAY, account_id=ACCOUNT)
    collector = ForecastExCollector(
        config=ForecastExConfig(), store=store, client=client,
    )
    assert collector.balance_source == "forecastex:ibkr-gateway"
