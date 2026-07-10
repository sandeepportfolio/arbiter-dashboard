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


async def test_client_place_order_rejects_invalid_side(client):
    with pytest.raises(ValueError, match="must be BUY or SELL"):
        await client.place_order(
            conid="111", side="EXERCISE", price=0.55, quantity=10,
        )
    await client.close()


async def test_client_place_order_sell_accepted(client):
    with aioresponses() as m:
        m.post(
            re.compile(rf".*/iserver/account/{ACCOUNT}/orders.*"),
            payload={"order_id": "sell-1", "order_status": "Filled",
                     "filled_quantity": 5, "avg_price": 0.42},
        )
        resp = await client.place_order(
            conid="222", side="SELL", price=0.42, quantity=5, tif="IOC",
        )
    assert resp["order_id"] == "sell-1"
    await client.close()


async def test_client_place_order_walks_confirmation_reply_chain(client):
    """IBKR sometimes responds with a confirmation prompt
    ``[{"id": "<uuid>", "message": [...]}]`` before delivering the order
    ack. The client should auto-POST ``{"confirmed": true}`` to
    ``/iserver/reply/<id>`` and return the eventual order record.
    """
    reply_id = "abc-reply-1"
    with aioresponses() as m:
        # Initial POST returns a confirmation prompt.
        m.post(
            re.compile(rf".*/iserver/account/{ACCOUNT}/orders.*"),
            payload=[{"id": reply_id, "message": ["Order exceeds size warning"]}],
        )
        # Reply POST returns the actual order ack.
        m.post(
            re.compile(rf".*/iserver/reply/{reply_id}.*"),
            payload=[{"order_id": "ord-after-reply", "order_status": "Submitted"}],
        )
        resp = await client.place_order(
            conid="333", side="SELL", price=0.30, quantity=2, tif="GTC",
        )
    items = resp.get("items") if isinstance(resp, dict) else None
    assert items is not None
    assert items[0]["order_id"] == "ord-after-reply"
    await client.close()


async def test_client_place_order_walks_two_chained_replies(client):
    """The gateway can chain prompts (size warning followed by price
    warning). The client must walk both before returning the order ack."""
    reply1 = "r1"
    reply2 = "r2"
    with aioresponses() as m:
        m.post(
            re.compile(rf".*/iserver/account/{ACCOUNT}/orders.*"),
            payload=[{"id": reply1, "message": ["size warning"]}],
        )
        m.post(
            re.compile(rf".*/iserver/reply/{reply1}.*"),
            payload=[{"id": reply2, "message": ["price warning"]}],
        )
        m.post(
            re.compile(rf".*/iserver/reply/{reply2}.*"),
            payload=[{"order_id": "ord-2-replies", "order_status": "Filled"}],
        )
        resp = await client.place_order(
            conid="444", side="SELL", price=0.05, quantity=1, tif="IOC",
        )
    items = resp.get("items") if isinstance(resp, dict) else None
    assert items is not None
    assert items[0]["order_id"] == "ord-2-replies"
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


async def test_strikes_429_uses_exponential_backoff(client, monkeypatch):
    """V19 regression: /iserver/secdef/strikes 429 retries MUST use the
    exponential ladder regardless of Retry-After (IBKR sometimes returns
    ``Retry-After: 1`` which floods straight back into another 429).

    Ladder bumped from 1/3/9s → 5/15/45s on 2026-05-26 after observing
    every one of 41 resolver candidates 429 in a single cycle — the
    prior ladder retried too aggressively to let IBKR's bucket refill.
    """
    sleeps: list[float] = []

    async def _record_sleep(delay):
        sleeps.append(float(delay))

    monkeypatch.setattr("arbiter.collectors.forecastex.asyncio.sleep", _record_sleep)

    # iserver/* paths require the brokerage bridge — return a 200 so the
    # pre-flight succeeds before the 429s start firing.
    with aioresponses() as m:
        m.post(re.compile(r".*/iserver/auth/ssodh/init.*"), payload={"connected": True})
        for _ in range(3):
            m.get(
                re.compile(r".*/iserver/secdef/strikes.*"),
                status=429,
                headers={"Retry-After": "1"},  # Ignored on the strikes path.
            )
        with pytest.raises(RuntimeError, match="rate-limit retry exhausted"):
            await client._request(
                "GET", "/iserver/secdef/strikes",
                params={"conid": "1", "exchange": "FORECASTX",
                        "sectype": "OPT", "month": "NOV26"},
            )
    # First two 429s should sleep 5s then 15s (we don't get a third sleep
    # because the 3rd retry exhausts and raises).
    assert sleeps[:2] == [5.0, 15.0], (
        f"expected exponential 5s,15s ladder on /iserver/secdef/strikes; got {sleeps[:2]}"
    )
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


# ── IBKR brokerage-bridge initialization (audit 2026-05) ──────────────────


async def test_client_iserver_call_initializes_brokerage_bridge(client):
    """First /iserver/* call must POST to /iserver/auth/ssodh/init so the
    brokerage bridge is established.  Audit 2026-05 found forecastex
    discovery returning 0 events because no code path ever initialized
    the bridge — every secdef/search came back as 400 ``no bridge``.
    """
    with aioresponses() as m:
        m.post(
            re.compile(r".*/iserver/auth/ssodh/init.*"),
            payload={"authenticated": True, "established": True},
        )
        m.post(
            re.compile(r".*/iserver/secdef/search.*"),
            payload=[{"conid": "111", "companyHeader": "TEAM A WINS (FORECASTX)"}],
        )
        result = await client._request(
            "POST", "/iserver/secdef/search",
            json_body={"symbol": "nba", "name": True},
        )
    # Bridge flag flipped on successful init.
    assert client._bridge_ready is True
    # Result wrapped as expected.
    assert result == {"items": [{"conid": "111", "companyHeader": "TEAM A WINS (FORECASTX)"}]}
    await client.close()


async def test_client_iserver_no_bridge_400_triggers_retry_with_init(client):
    """If the gateway has been alive longer than the brokerage bridge
    (e.g. the IBKR session timed out and reconnected), a /iserver/* call
    can still receive ``400 no bridge``.  The client must re-initialize
    the bridge and retry once before surfacing the error so transient
    bridge drops don't propagate to discovery / market snapshots.
    """
    # Pretend the bridge was previously initialized so we exercise the
    # retry path (not the upfront init path).
    client._bridge_ready = True

    with aioresponses() as m:
        # First call returns 400 no bridge.
        m.get(
            re.compile(r".*/iserver/marketdata/snapshot.*"),
            status=400,
            payload={"error": "Bad Request: no bridge", "statusCode": 400},
        )
        # Bridge re-init succeeds.
        m.post(
            re.compile(r".*/iserver/auth/ssodh/init.*"),
            payload={"authenticated": True, "established": True},
        )
        # Retry succeeds with real data.
        m.get(
            re.compile(r".*/iserver/marketdata/snapshot.*"),
            payload=[{"31": "0.55", "84": "0.54", "86": "0.56"}],
        )
        snap = await client.market_snapshot("99999")
    assert snap["86"] == "0.56"
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


async def test_collector_skips_duplicate_and_unavailable_forecastex_ids(monkeypatch):
    fake = {
        "NFL_NYJ": {
            "kalshi": "KX-NYJ",
            "forecastex": "882351367",
            "status": "confirmed",
        },
        "NFL_NYG": {
            "kalshi": "KX-NYG",
            "forecastex": "882351367",
            "status": "confirmed",
        },
        "FX_UNAVAILABLE": {
            "kalshi": "KX-OFF",
            "forecastex": "111222",
            "forecastex_not_available": True,
            "status": "confirmed",
        },
        "FX_GOOD": {
            "kalshi": "KX-GOOD",
            "forecastex": "333444",
            "status": "confirmed",
        },
    }
    monkeypatch.setattr(settings_mod, "MARKET_MAP", fake)
    import arbiter.collectors.forecastex as fcst_mod

    monkeypatch.setattr(fcst_mod, "MARKET_MAP", fake)

    store = PriceStore()
    client = ForecastExClient(gateway_url=GATEWAY, account_id=ACCOUNT)
    collector = ForecastExCollector(
        config=ForecastExConfig(), store=store, client=client,
    )

    assert collector._duplicate_conids == {"882351367": ["NFL_NYG", "NFL_NYJ"]}
    assert "NFL_NYJ" not in collector._conid_map
    assert "NFL_NYG" not in collector._conid_map
    assert "FX_UNAVAILABLE" not in collector._conid_map
    assert collector._conid_map["FX_GOOD"] == ("333444", "")
    await client.close()


async def test_collector_quarantines_yes_snapshot_500_without_opening_circuit(monkeypatch):
    fake = {
        "FX_BAD_CONID": {
            "kalshi": "KX-BAD",
            "forecastex": "762089343",
            "status": "confirmed",
        },
    }
    monkeypatch.setattr(settings_mod, "MARKET_MAP", fake)
    import arbiter.collectors.forecastex as fcst_mod

    monkeypatch.setattr(fcst_mod, "MARKET_MAP", fake)

    store = PriceStore()
    client = ForecastExClient(gateway_url=GATEWAY, account_id=ACCOUNT)
    collector = ForecastExCollector(
        config=ForecastExConfig(), store=store, client=client,
    )

    with aioresponses() as m:
        m.post(re.compile(r".*/iserver/auth/ssodh/init.*"),
               payload={"connected": True})
        m.get(
            re.compile(r".*/iserver/marketdata/snapshot\?.*conids=762089343.*"),
            status=500,
            payload={"error": "Internal Server Error"},
        )
        results = await collector.fetch_markets()

    assert results == []
    assert "762089343" in collector._inactive_conids
    assert collector.total_errors == 0
    assert collector.consecutive_errors == 0
    assert collector.circuit.stats["state"] == "closed"
    assert collector.circuit.stats["failures"] == 0
    await client.close()


async def test_collector_normalizes_cent_prices_and_emits_yes_only_when_no_unknown(
    patched_market_map,
):
    """ForecastEx YES snapshot rescales 0–100 cents → [0, 1] dollars.

    Mapping carries only the YES conid (forecastex_no=""), runtime NO
    sibling discovery is not mocked here so the collector emits a
    YES-only price point: no_market_id="" and no_ask=0 so the scanner
    cannot fabricate a cross-side opportunity.

    Regression for ARB-000695/699: the prior implementation synthesized
    ``no_ask = 1 - yes_bid`` AND set ``no_market_id = yes_conid`` —
    feeding a fake $0.53 NO ask against a dead NO book and silently
    routing NO orders to the YES contract.
    """
    store = PriceStore()
    client = ForecastExClient(gateway_url=GATEWAY, account_id=ACCOUNT)
    collector = ForecastExCollector(
        config=ForecastExConfig(), store=store, client=client,
    )
    with aioresponses() as m:
        # Mocking BOTH snapshot calls: YES (conid 111222 from fixture) and
        # the runtime NO-discovery contract_info call (which we leave to
        # fail — contract_info returns {} → no NO sibling discovered).
        m.get(
            re.compile(r".*/iserver/marketdata/snapshot.*"),
            # Cent quotes; collector must rescale to [0, 1].
            payload=[{"31": "45", "84": "42", "86": "47", "7295": "5", "7296": "7"}],
        )
        # contract_info returns empty so _attempt_no_conid_discovery
        # gives up and the NO side stays empty.
        m.post(re.compile(r".*/iserver/auth/ssodh/init.*"),
               payload={"connected": True})
        m.get(re.compile(r".*/iserver/contract/111222/info.*"), payload={})
        results = await collector.fetch_markets()
    assert len(results) == 1
    pp = results[0]
    assert pp.platform == "forecastex"
    assert pp.canonical_id == "EXAMPLE_2026_OUTCOME"
    assert 0.46 < pp.yes_ask < 0.48
    assert 0.41 < pp.yes_bid < 0.43
    # YES side IDs populated.
    assert pp.yes_market_id == "111222"
    # NO discovery failed → no NO snapshot → NO side is zero, NO id is empty.
    assert pp.no_ask == 0.0
    assert pp.no_bid == 0.0
    assert pp.no_market_id == ""
    # Critical regression assertion: NO market id MUST NOT equal YES conid.
    assert pp.no_market_id != pp.yes_market_id
    # Fee rate stored so position sizer doesn't re-compute.
    assert pp.fee_rate == 0.005
    await client.close()


async def test_collector_uses_real_no_snapshot_when_no_conid_mapped(monkeypatch):
    """When MARKET_MAP carries forecastex_no, the collector fetches the
    NO snapshot directly and uses its real bid/ask — never synthesizes
    from (1 - yes_bid).
    """
    fake = {
        "BINARY_HORC": {
            "kalshi": "KX-HORC",
            "polymarket": "horc-poly",
            "forecastex": "100000",       # YES conid (right=C)
            "forecastex_no": "100001",    # NO conid (right=P)
            "status": "confirmed",
            "mapping_score": 0.95,
        },
    }
    monkeypatch.setattr(settings_mod, "MARKET_MAP", fake)
    import arbiter.collectors.forecastex as fcst_mod
    monkeypatch.setattr(fcst_mod, "MARKET_MAP", fake)

    store = PriceStore()
    client = ForecastExClient(gateway_url=GATEWAY, account_id=ACCOUNT)
    collector = ForecastExCollector(
        config=ForecastExConfig(), store=store, client=client,
    )

    def _snapshot_payload(url, **kwargs):
        # aioresponses doesn't easily route by query string, so use a
        # match-by-substring approach via two endpoints in order.
        pass

    # YES snapshot returns 0.42/0.47 (cents). NO snapshot returns 0.48/0.55.
    # If the collector synthesized NO from (1 - yes_bid), we'd see ~0.58 ask;
    # the test asserts the REAL 0.55 ask is what lands in PricePoint.no_ask.
    yes_payload = [{"84": "42", "86": "47", "7295": "5", "7296": "9"}]
    no_payload = [{"84": "48", "86": "55", "7295": "11", "7296": "13"}]

    with aioresponses() as m:
        # Snapshot endpoint is queried by conids param; aioresponses
        # matches whichever URL was registered first that matches the
        # pattern. Register both with the specific conids param so the
        # match is unambiguous via url-query string.
        m.get(
            re.compile(r".*/iserver/marketdata/snapshot\?.*conids=100000.*"),
            payload=yes_payload,
        )
        m.get(
            re.compile(r".*/iserver/marketdata/snapshot\?.*conids=100001.*"),
            payload=no_payload,
        )
        results = await collector.fetch_markets()

    assert len(results) == 1
    pp = results[0]
    assert pp.yes_market_id == "100000"
    assert pp.no_market_id == "100001"      # NO conid populated from mapping
    assert pp.yes_market_id != pp.no_market_id
    # REAL NO ask is 0.55 from the NO snapshot — NOT 1 - yes_bid = 0.58.
    assert abs(pp.no_ask - 0.55) < 1e-6
    assert abs(pp.no_bid - 0.48) < 1e-6
    # The synthetic-fallback would produce ~0.58; real NO ask is 0.55,
    # so any value within 1e-6 of 0.58 is the bug regression.
    assert abs(pp.no_ask - 0.58) > 0.02
    await client.close()


async def test_collector_skips_no_when_no_snapshot_empty(monkeypatch):
    """If the mapped NO conid returns an empty/non-tradeable snapshot,
    the collector emits a YES-only quote rather than synthesizing
    from YES bid. no_market_id must be cleared so the scanner cannot
    route NO orders to a dead conid.
    """
    fake = {
        "PARTIAL_MARKET": {
            "kalshi": "KX-X",
            "polymarket": "x-poly",
            "forecastex": "200000",
            "forecastex_no": "200001",
            "status": "confirmed",
        },
    }
    monkeypatch.setattr(settings_mod, "MARKET_MAP", fake)
    import arbiter.collectors.forecastex as fcst_mod
    monkeypatch.setattr(fcst_mod, "MARKET_MAP", fake)

    store = PriceStore()
    client = ForecastExClient(gateway_url=GATEWAY, account_id=ACCOUNT)
    collector = ForecastExCollector(
        config=ForecastExConfig(), store=store, client=client,
    )

    yes_payload = [{"84": "40", "86": "45", "7295": "10", "7296": "10"}]
    # NO snapshot returns only metadata — no bid/ask fields.
    no_payload = [{"55": "FOO", "conidEx": "200001", "conid": 200001}]

    with aioresponses() as m:
        m.get(
            re.compile(r".*/iserver/marketdata/snapshot\?.*conids=200000.*"),
            payload=yes_payload,
        )
        # Both warmup and primary call need a match; register twice.
        m.get(
            re.compile(r".*/iserver/marketdata/snapshot\?.*conids=200001.*"),
            payload=no_payload,
        )
        m.get(
            re.compile(r".*/iserver/marketdata/snapshot\?.*conids=200001.*"),
            payload=no_payload,
        )
        results = await collector.fetch_markets()

    assert len(results) == 1
    pp = results[0]
    assert pp.yes_market_id == "200000"
    # NO conid was MAPPED but its snapshot was empty → cleared, not faked.
    assert pp.no_market_id == ""
    assert pp.no_ask == 0.0
    assert pp.no_bid == 0.0
    await client.close()


async def test_collector_attempts_runtime_no_discovery(monkeypatch):
    """When mapping has only YES conid (legacy), collector runs
    contract_info + resolve_event_children to find the NO sibling.
    """
    fake = {
        "LEGACY_MAPPING": {
            "kalshi": "KX-L",
            "polymarket": "l-poly",
            "forecastex": "300000",  # YES only
            # forecastex_no missing — collector should runtime-discover.
            "status": "confirmed",
        },
    }
    monkeypatch.setattr(settings_mod, "MARKET_MAP", fake)
    import arbiter.collectors.forecastex as fcst_mod
    monkeypatch.setattr(fcst_mod, "MARKET_MAP", fake)

    store = PriceStore()
    client = ForecastExClient(gateway_url=GATEWAY, account_id=ACCOUNT)
    collector = ForecastExCollector(
        config=ForecastExConfig(), store=store, client=client,
    )

    yes_payload = [{"84": "40", "86": "45", "7295": "10", "7296": "10"}]
    no_payload = [{"84": "52", "86": "58", "7295": "8", "7296": "8"}]
    # contract_info reports right=C, strike=1.0, parent=999999.
    info_payload = {
        "right": "C", "strike": "1.0",
        "underlying_con_id": "999999",
        "symbol": "HORC",
    }
    # strikes endpoint for the parent returns call/put strikes including 1.0.
    strikes_payload = {"call": [1.0], "put": [1.0]}
    # secdef/info for strike 1.0 returns both Call (300000=YES) and
    # Put (300001=NO).
    secdef_info_payload = [
        {"conid": "300000", "right": "C", "strike": 1.0,
         "maturityDate": "20271101"},
        {"conid": "300001", "right": "P", "strike": 1.0,
         "maturityDate": "20271101"},
    ]

    with aioresponses() as m:
        m.post(re.compile(r".*/iserver/auth/ssodh/init.*"),
               payload={"connected": True})
        m.get(
            re.compile(r".*/iserver/marketdata/snapshot\?.*conids=300000.*"),
            payload=yes_payload,
        )
        m.get(
            re.compile(r".*/iserver/contract/300000/info.*"),
            payload=info_payload,
        )
        m.get(
            re.compile(r".*/iserver/secdef/strikes\?.*"),
            payload=strikes_payload,
        )
        m.get(
            re.compile(r".*/iserver/secdef/info\?.*"),
            payload=secdef_info_payload,
        )
        m.get(
            re.compile(r".*/iserver/marketdata/snapshot\?.*conids=300001.*"),
            payload=no_payload,
        )
        results = await collector.fetch_markets()

    assert len(results) == 1
    pp = results[0]
    assert pp.yes_market_id == "300000"
    assert pp.no_market_id == "300001"  # discovered via sibling lookup
    # Cached for future cycles.
    assert collector._no_discovery_cache.get("300000") == "300001"
    # Real NO ask, not synthetic.
    assert abs(pp.no_ask - 0.58) < 1e-6
    await client.close()


async def test_no_conid_discovery_soft_blocks_transient_failures(monkeypatch):
    """Regression (CV-02, 2026-06-05): when ``resolve_event_children``
    raises (429 storm / circuit-open / timeout), the next poll cycle must
    NOT replay the contract_info + secdef/strikes lookup against the same
    YES conid — otherwise the 5s/15s/45s retry ladder repeats every poll
    and floods the IBKR gateway with 429s. The TTL soft-block stops that.
    """
    import time as time_mod

    fake = {
        "TRANSIENT_FAILING": {
            "kalshi": "KX-T",
            "polymarket": "t-poly",
            "forecastex": "700000",  # YES only — triggers discovery path
            "status": "confirmed",
        },
    }
    monkeypatch.setattr(settings_mod, "MARKET_MAP", fake)
    import arbiter.collectors.forecastex as fcst_mod
    monkeypatch.setattr(fcst_mod, "MARKET_MAP", fake)

    store = PriceStore()
    client = ForecastExClient(gateway_url=GATEWAY, account_id=ACCOUNT)
    collector = ForecastExCollector(
        config=ForecastExConfig(), store=store, client=client,
    )
    collector._first_cycle_discovery_throttle_s = 0.0

    # Count how many times resolve_event_children is invoked across calls.
    call_count = {"resolve": 0, "info": 0}
    original_resolve = client.resolve_event_children
    original_info = client.get_contract_info

    async def _failing_resolve(parent_conid, **kwargs):
        call_count["resolve"] += 1
        raise RuntimeError("transient 429/timeout/circuit-open")

    async def _fake_contract_info(yes_conid):
        call_count["info"] += 1
        return {
            "right": "C",
            "strike": "1.0",
            "underlying_con_id": "888888",
            "symbol": "TRANS",
        }

    monkeypatch.setattr(client, "resolve_event_children", _failing_resolve)
    monkeypatch.setattr(client, "get_contract_info", _fake_contract_info)

    # First call: resolve_event_children raises, soft-block set.
    result1 = await collector._attempt_no_conid_discovery("700000")
    assert result1 is None
    assert call_count["resolve"] == 1
    assert call_count["info"] == 1
    assert "700000" in collector._no_discovery_soft_block

    # Second call (next poll cycle): should be suppressed entirely. No
    # additional contract_info or resolve_event_children invocations.
    result2 = await collector._attempt_no_conid_discovery("700000")
    assert result2 is None
    assert call_count["resolve"] == 1, "soft-block must suppress retry"
    assert call_count["info"] == 1, "soft-block must short-circuit before info"

    # Permanent _no_discovery_failed must NOT be set — this is a soft TTL.
    assert "700000" not in collector._no_discovery_failed

    # Expire the TTL by rewinding the expiry timestamp into the past.
    collector._no_discovery_soft_block["700000"] = time_mod.time() - 1.0
    result3 = await collector._attempt_no_conid_discovery("700000")
    assert result3 is None  # still fails
    assert call_count["resolve"] == 2, "expired TTL must allow retry"
    assert call_count["info"] == 2

    monkeypatch.setattr(client, "resolve_event_children", original_resolve)
    monkeypatch.setattr(client, "get_contract_info", original_info)
    await client.close()


async def test_first_cycle_flag_starts_true_and_clears_after_fetch(monkeypatch):
    """_first_discovery_cycle starts True and is False after fetch_markets()."""
    fake = {
        "LEGACY_A": {
            "kalshi": "KX-A", "polymarket": "a-poly",
            "forecastex": "400000",  # YES only — triggers discovery path
            "status": "confirmed",
        },
    }
    monkeypatch.setattr(settings_mod, "MARKET_MAP", fake)
    import arbiter.collectors.forecastex as fcst_mod
    monkeypatch.setattr(fcst_mod, "MARKET_MAP", fake)

    store = PriceStore()
    client = ForecastExClient(gateway_url=GATEWAY, account_id=ACCOUNT)
    collector = ForecastExCollector(config=ForecastExConfig(), store=store, client=client)

    assert collector._first_discovery_cycle is True

    yes_payload = [{"84": "40", "86": "45", "7295": "10", "7296": "10"}]
    info_payload = {"right": "C", "strike": "1.0", "underlying_con_id": "999999", "symbol": "HORC"}
    strikes_payload = {"call": [1.0], "put": [1.0]}
    secdef_payload = [
        {"conid": "400000", "right": "C", "strike": 1.0, "maturityDate": "20271101"},
        {"conid": "400001", "right": "P", "strike": 1.0, "maturityDate": "20271101"},
    ]
    no_payload = [{"84": "52", "86": "58", "7295": "8", "7296": "8"}]

    with aioresponses() as m:
        m.post(re.compile(r".*/iserver/auth/ssodh/init.*"), payload={"connected": True})
        m.get(re.compile(r".*/iserver/marketdata/snapshot\?.*conids=400000.*"), payload=yes_payload)
        m.get(re.compile(r".*/iserver/contract/400000/info.*"), payload=info_payload)
        m.get(re.compile(r".*/iserver/secdef/strikes\?.*"), payload=strikes_payload)
        m.get(re.compile(r".*/iserver/secdef/info\?.*"), payload=secdef_payload)
        m.get(re.compile(r".*/iserver/marketdata/snapshot\?.*conids=400001.*"), payload=no_payload)
        await collector.fetch_markets()

    assert collector._first_discovery_cycle is False
    await client.close()


async def test_first_cycle_throttle_sleeps_once_per_cache_miss(monkeypatch):
    """During first cycle, asyncio.sleep is called once per conid with a cache miss."""
    import asyncio
    fake = {
        "LEGACY_A": {
            "kalshi": "KX-A", "polymarket": "a-poly",
            "forecastex": "500000", "status": "confirmed",
        },
        "LEGACY_B": {
            "kalshi": "KX-B", "polymarket": "b-poly",
            "forecastex": "600000", "status": "confirmed",
        },
    }
    monkeypatch.setattr(settings_mod, "MARKET_MAP", fake)
    import arbiter.collectors.forecastex as fcst_mod
    monkeypatch.setattr(fcst_mod, "MARKET_MAP", fake)

    sleep_calls: list[float] = []

    async def _fake_sleep(delay: float) -> None:
        sleep_calls.append(delay)

    monkeypatch.setattr(asyncio, "sleep", _fake_sleep)

    store = PriceStore()
    client = ForecastExClient(gateway_url=GATEWAY, account_id=ACCOUNT)
    collector = ForecastExCollector(config=ForecastExConfig(), store=store, client=client)
    collector._first_cycle_discovery_throttle_s = 0.2

    yes_a = [{"84": "40", "86": "45", "7295": "10", "7296": "10"}]
    yes_b = [{"84": "41", "86": "46", "7295": "9", "7296": "9"}]
    no_a = [{"84": "52", "86": "58", "7295": "8", "7296": "8"}]
    no_b = [{"84": "53", "86": "59", "7295": "7", "7296": "7"}]
    info_a = {"right": "C", "strike": "1.0", "underlying_con_id": "111111", "symbol": "HORA"}
    info_b = {"right": "C", "strike": "1.0", "underlying_con_id": "222222", "symbol": "HORB"}
    strikes = {"call": [1.0], "put": [1.0]}
    secdef_a = [
        {"conid": "500000", "right": "C", "strike": 1.0, "maturityDate": "20271101"},
        {"conid": "500001", "right": "P", "strike": 1.0, "maturityDate": "20271101"},
    ]
    secdef_b = [
        {"conid": "600000", "right": "C", "strike": 1.0, "maturityDate": "20271101"},
        {"conid": "600001", "right": "P", "strike": 1.0, "maturityDate": "20271101"},
    ]

    with aioresponses() as m:
        m.post(re.compile(r".*/iserver/auth/ssodh/init.*"), payload={"connected": True})
        m.get(re.compile(r".*/iserver/marketdata/snapshot\?.*conids=500000.*"), payload=yes_a)
        m.get(re.compile(r".*/iserver/marketdata/snapshot\?.*conids=600000.*"), payload=yes_b)
        m.get(re.compile(r".*/iserver/contract/500000/info.*"), payload=info_a)
        m.get(re.compile(r".*/iserver/contract/600000/info.*"), payload=info_b)
        m.get(re.compile(r".*/iserver/secdef/strikes\?.*conid=111111.*"), payload=strikes)
        m.get(re.compile(r".*/iserver/secdef/strikes\?.*conid=222222.*"), payload=strikes)
        m.get(re.compile(r".*/iserver/secdef/info\?.*conid=111111.*"), payload=secdef_a)
        m.get(re.compile(r".*/iserver/secdef/info\?.*conid=222222.*"), payload=secdef_b)
        m.get(re.compile(r".*/iserver/marketdata/snapshot\?.*conids=500001.*"), payload=no_a)
        m.get(re.compile(r".*/iserver/marketdata/snapshot\?.*conids=600001.*"), payload=no_b)
        await collector.fetch_markets()

    # One 2.0s sleep per YES conid that needed network discovery
    # (both 500000 and 600000 were cache misses, under per-cycle cap).
    assert len(sleep_calls) == 2
    assert all(d == 2.0 for d in sleep_calls)
    await client.close()


async def test_second_cycle_discovery_skips_sleep(monkeypatch):
    """No asyncio.sleep on the second fetch_markets() call — cache is warm."""
    import asyncio
    fake = {
        "LEGACY_A": {
            "kalshi": "KX-A", "polymarket": "a-poly",
            "forecastex": "700000", "status": "confirmed",
        },
    }
    monkeypatch.setattr(settings_mod, "MARKET_MAP", fake)
    import arbiter.collectors.forecastex as fcst_mod
    monkeypatch.setattr(fcst_mod, "MARKET_MAP", fake)

    sleep_calls: list[float] = []

    async def _fake_sleep(delay: float) -> None:
        sleep_calls.append(delay)

    monkeypatch.setattr(asyncio, "sleep", _fake_sleep)

    store = PriceStore()
    client = ForecastExClient(gateway_url=GATEWAY, account_id=ACCOUNT)
    collector = ForecastExCollector(config=ForecastExConfig(), store=store, client=client)

    yes_payload = [{"84": "40", "86": "45", "7295": "10", "7296": "10"}]
    no_payload = [{"84": "52", "86": "58", "7295": "8", "7296": "8"}]
    info_payload = {"right": "C", "strike": "1.0", "underlying_con_id": "888888", "symbol": "HORC"}
    strikes_payload = {"call": [1.0], "put": [1.0]}
    secdef_payload = [
        {"conid": "700000", "right": "C", "strike": 1.0, "maturityDate": "20271101"},
        {"conid": "700001", "right": "P", "strike": 1.0, "maturityDate": "20271101"},
    ]

    # First cycle — one sleep expected.
    with aioresponses() as m:
        m.post(re.compile(r".*/iserver/auth/ssodh/init.*"), payload={"connected": True})
        m.get(re.compile(r".*/iserver/marketdata/snapshot\?.*conids=700000.*"), payload=yes_payload)
        m.get(re.compile(r".*/iserver/contract/700000/info.*"), payload=info_payload)
        m.get(re.compile(r".*/iserver/secdef/strikes\?.*"), payload=strikes_payload)
        m.get(re.compile(r".*/iserver/secdef/info\?.*"), payload=secdef_payload)
        m.get(re.compile(r".*/iserver/marketdata/snapshot\?.*conids=700001.*"), payload=no_payload)
        await collector.fetch_markets()

    assert len(sleep_calls) == 1
    sleep_calls.clear()

    # Second cycle — cache hit, no sleep.
    with aioresponses() as m:
        m.post(re.compile(r".*/iserver/auth/ssodh/init.*"), payload={"connected": True})
        m.get(re.compile(r".*/iserver/marketdata/snapshot\?.*conids=700000.*"), payload=yes_payload)
        m.get(re.compile(r".*/iserver/marketdata/snapshot\?.*conids=700001.*"), payload=no_payload)
        await collector.fetch_markets()

    assert sleep_calls == [], "no sleep on second cycle — cache is warm"
    await client.close()


async def test_first_cycle_no_sleep_when_conid_already_in_failed_set(monkeypatch):
    """Pre-populated _no_discovery_failed suppresses the first-cycle sleep."""
    import asyncio
    fake = {
        "LEGACY_A": {
            "kalshi": "KX-A", "polymarket": "a-poly",
            "forecastex": "800000", "status": "confirmed",
        },
    }
    monkeypatch.setattr(settings_mod, "MARKET_MAP", fake)
    import arbiter.collectors.forecastex as fcst_mod
    monkeypatch.setattr(fcst_mod, "MARKET_MAP", fake)

    sleep_calls: list[float] = []

    async def _fake_sleep(delay: float) -> None:
        sleep_calls.append(delay)

    monkeypatch.setattr(asyncio, "sleep", _fake_sleep)

    store = PriceStore()
    client = ForecastExClient(gateway_url=GATEWAY, account_id=ACCOUNT)
    collector = ForecastExCollector(config=ForecastExConfig(), store=store, client=client)
    # Simulate: a previous partial run already flagged 800000 as a discovery failure.
    collector._no_discovery_failed.add("800000")

    yes_payload = [{"84": "40", "86": "45", "7295": "10", "7296": "10"}]

    with aioresponses() as m:
        m.post(re.compile(r".*/iserver/auth/ssodh/init.*"), payload={"connected": True})
        m.get(re.compile(r".*/iserver/marketdata/snapshot\?.*conids=800000.*"), payload=yes_payload)
        await collector.fetch_markets()

    assert sleep_calls == [], "already-failed conid must not trigger the throttle sleep"
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
    forever burns rate-limit budget; after enough non-tradeable
    snapshots the collector must mark the conid inactive and stop
    probing.

    Threshold is now ``_PROBE_DISABLE_THRESHOLD`` (8 by default) and
    each ``fetch_markets`` call issues two snapshot HTTP requests via
    ``_snapshot_with_warmup`` (initial + warmup retry). The test
    exercises one above-threshold cycle to confirm disable triggers.
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
    fetch_calls = collector._PROBE_DISABLE_THRESHOLD + 1
    with aioresponses() as m:
        # Each fetch_markets issues 2 snapshot HTTPs (initial + warmup),
        # so register 2× fetch_calls + a small cushion.
        for _ in range(fetch_calls * 2 + 2):
            m.get(
                re.compile(r".*/iserver/marketdata/snapshot.*"),
                payload=parent_snapshot,
            )
        for _ in range(fetch_calls):
            await collector.fetch_markets()
    # The patched_market_map fixture binds conid "111222" to the canonical id.
    assert "111222" in collector._inactive_conids
    assert (
        collector._parent_probe_counts["111222"]
        >= collector._PROBE_DISABLE_THRESHOLD
    )
    await client.close()


async def test_disable_callback_fires_when_parent_conid_disabled(patched_market_map):
    """The collector must invoke the disable callback the instant it
    moves a conid to ``_inactive_conids``. Without this hook, the
    child-conid resolver would wait up to ``interval_s`` (30 min by
    default) for its next periodic cycle to react — verified live
    2026-05-26 that cycle 1 races the first batch of disables and
    sees candidates=0.
    """
    store = PriceStore()
    client = ForecastExClient(gateway_url=GATEWAY, account_id=ACCOUNT)
    collector = ForecastExCollector(
        config=ForecastExConfig(), store=store, client=client,
    )
    fired_with: list[str] = []
    collector.set_disable_callback(lambda c: fired_with.append(c))

    parent_snapshot = [{
        "55": "HORC", "conidEx": "733131966", "conid": 733131966, "7295": "0.00",
    }]
    fetch_calls = collector._PROBE_DISABLE_THRESHOLD + 1
    with aioresponses() as m:
        for _ in range(fetch_calls * 2 + 2):
            m.get(
                re.compile(r".*/iserver/marketdata/snapshot.*"),
                payload=parent_snapshot,
            )
        for _ in range(fetch_calls):
            await collector.fetch_markets()
    assert "111222" in collector._inactive_conids
    assert "111222" in fired_with, (
        "set_disable_callback registered hook was not invoked when the "
        "collector moved the conid into _inactive_conids"
    )
    await client.close()


async def test_disable_callback_exception_does_not_break_fetch(patched_market_map):
    """A buggy callback (e.g. resolver attribute error during reload)
    must not propagate up and kill the fetch loop. The disable still
    happens — only the notification is dropped."""
    store = PriceStore()
    client = ForecastExClient(gateway_url=GATEWAY, account_id=ACCOUNT)
    collector = ForecastExCollector(
        config=ForecastExConfig(), store=store, client=client,
    )

    def _broken(_conid: str) -> None:
        raise RuntimeError("simulated resolver wiring error")

    collector.set_disable_callback(_broken)

    parent_snapshot = [{
        "55": "HORC", "conidEx": "733131966", "conid": 733131966, "7295": "0.00",
    }]
    fetch_calls = collector._PROBE_DISABLE_THRESHOLD + 1
    with aioresponses() as m:
        for _ in range(fetch_calls * 2 + 2):
            m.get(
                re.compile(r".*/iserver/marketdata/snapshot.*"),
                payload=parent_snapshot,
            )
        for _ in range(fetch_calls):
            await collector.fetch_markets()
    assert "111222" in collector._inactive_conids
    await client.close()


async def test_collector_warmup_retry_recovers_tradeable_child(patched_market_map):
    """Regression — IBKR's snapshot field cache warms lazily, so the
    first call to a freshly-subscribed child conid often returns only
    metadata keys (no 31/84/86) even though the contract IS tradeable.
    ``_snapshot_with_warmup`` retries once after a short sleep; the
    successful second call must produce a PricePoint AND avoid
    incrementing the parent-probe-counter that would otherwise disable
    the conid after ~8 cycles.

    This is the bug that disabled 762089343 (DEM_HOUSE) and 773659700
    (GOP_HOUSE) on 2026-05-26 despite IBKR returning live bid/ask on
    direct probe seconds later.
    """
    store = PriceStore()
    client = ForecastExClient(gateway_url=GATEWAY, account_id=ACCOUNT)
    collector = ForecastExCollector(
        config=ForecastExConfig(), store=store, client=client,
    )
    cold_snapshot = [{
        "conidEx": "111222",
        "conid": 111222,
        "_updated": 1779768000000,
    }]
    warm_snapshot = [{
        "conidEx": "111222",
        "conid": 111222,
        "31": "0.78",
        "84": "0.77",
        "86": "0.79",
        "7295": "12",
        "7296": "8",
    }]
    with aioresponses() as m:
        # First fetch: cold then warm (warmup retry kicks in).
        m.get(
            re.compile(r".*/iserver/marketdata/snapshot.*"),
            payload=cold_snapshot,
        )
        m.get(
            re.compile(r".*/iserver/marketdata/snapshot.*"),
            payload=warm_snapshot,
        )
        results = await collector.fetch_markets()

    assert len(results) == 1
    assert results[0].yes_bid == 0.77
    assert results[0].yes_ask == 0.79
    # Warmup-recovered conids must NOT accumulate probe-count.
    assert "111222" not in collector._inactive_conids
    assert collector._parent_probe_counts.get("111222", 0) == 0
    await client.close()


async def test_collector_clears_probe_count_on_tradeable_snapshot(patched_market_map):
    """If a conid was previously seen with a non-tradeable warmup
    response, a later tradeable snapshot must reset the probe counter
    so transient warmup blips don't slowly accumulate to the disable
    threshold over many cycles.
    """
    store = PriceStore()
    client = ForecastExClient(gateway_url=GATEWAY, account_id=ACCOUNT)
    collector = ForecastExCollector(
        config=ForecastExConfig(), store=store, client=client,
    )
    # Seed prior probe count to simulate earlier cold-cache cycles.
    collector._parent_probe_counts["111222"] = 5

    tradeable = [{
        "conidEx": "111222",
        "conid": 111222,
        "31": "0.50",
        "84": "0.49",
        "86": "0.51",
    }]
    with aioresponses() as m:
        m.get(re.compile(r".*/iserver/marketdata/snapshot.*"), payload=tradeable)
        results = await collector.fetch_markets()

    assert len(results) == 1
    assert "111222" not in collector._parent_probe_counts
    assert "111222" not in collector._inactive_conids
    await client.close()


async def test_resolve_event_children_returns_empty_when_all_endpoints_fail(client):
    """IBKR returns 503 for FORECASTX strikes on weekends; the resolver
    must swallow those failures across all probed months and return []
    rather than raising. This is the candidate-not-resolvable signal
    the upstream service uses to bucket attempts as ibkr_503.
    """
    with aioresponses() as m:
        # Every /iserver/secdef/strikes call returns 503.
        m.get(re.compile(r".*/iserver/secdef/strikes.*"), status=503, body="", repeat=True)
        children = await client.resolve_event_children("733131966", months=("NOV26",))
    assert children == []
    await client.close()


async def test_resolve_event_children_returns_children_when_secdef_info_works(client):
    """Documented IBKR FORECASTX flow (verified live 2026-05-25):
      1. GET /iserver/secdef/strikes?conid=X&exchange=FORECASTX&sectype=OPT&month=NOV26
         → {"call": [1.0, 2.0], "put": [1.0, 2.0]}
      2. GET /iserver/secdef/info?...&strike=1.0
         → items: [{conid, right=C|P, strike}]
    The resolver must call strikes first, then walk each unique strike
    via info and assemble the full child list.
    """
    with aioresponses() as m:
        # strikes endpoint returns two strikes
        m.get(
            re.compile(r".*/iserver/secdef/strikes.*"),
            payload={"call": [1.0, 2.0], "put": [1.0, 2.0]},
            repeat=True,
        )
        # info endpoint returns Call + Put per strike — return different
        # conid sets per call so we can verify both strikes were probed.
        # aioresponses matches in registration order.
        m.get(
            re.compile(r".*/iserver/secdef/info.*strike=1.*"),
            payload={"items": [
                {"conid": "1001", "right": "C", "strike": 1.0,
                 "desc2": "NOV26 1 YES @FORECASTX", "maturityDate": "20270104"},
                {"conid": "1002", "right": "P", "strike": 1.0,
                 "desc2": "NOV26 1 NO @FORECASTX", "maturityDate": "20270104"},
            ]},
            repeat=True,
        )
        m.get(
            re.compile(r".*/iserver/secdef/info.*strike=2.*"),
            payload={"items": [
                {"conid": "2001", "right": "C", "strike": 2.0,
                 "desc2": "NOV26 2 YES @FORECASTX", "maturityDate": "20270104"},
                {"conid": "2002", "right": "P", "strike": 2.0,
                 "desc2": "NOV26 2 NO @FORECASTX", "maturityDate": "20270104"},
            ]},
            repeat=True,
        )
        children = await client.resolve_event_children(
            "733131966", months=("NOV26",),
        )
    # 2 strikes × Call+Put = 4 children
    conids = {c["conid"] for c in children}
    assert conids == {"1001", "1002", "2001", "2002"}
    # Right enum is C/P verbatim (IBKR convention)
    rights = {c["right"] for c in children}
    assert "C" in rights and "P" in rights
    # source field carries strike + month for traceability
    assert all("strike=" in c.get("source", "") for c in children)
    await client.close()


async def test_resolve_event_children_uses_correct_sectype_opt(client):
    """Regression: previous implementation used sectype=EC which IBKR
    returns 400/503 for. The fix is sectype=OPT per docs.interactive
    brokers.com/campus/ibkr-api-page/event-contracts/. This test
    asserts the wire request carries the OPT enum.
    """
    captured: list[str] = []

    with aioresponses() as m:
        def _capture(url, **kwargs):
            captured.append(str(url))
            from aioresponses.core import CallbackResult
            return CallbackResult(payload={"call": [], "put": []})

        m.get(re.compile(r".*/iserver/secdef/strikes.*"), callback=_capture, repeat=True)
        await client.resolve_event_children("733131966", months=("NOV26",))

    assert captured, "should have hit /iserver/secdef/strikes"
    assert "sectype=OPT" in captured[0], (
        f"expected sectype=OPT in wire request; got: {captured[0]}"
    )
    assert "exchange=FORECASTX" in captured[0]
    assert "month=NOV26" in captured[0]
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


async def test_reactivate_conid_clears_inactive_state(patched_market_map):
    """FILL-FX01: when the resolver replaces a parent conid with its
    YES child, the collector must drop any in-memory inactive flag on
    BOTH conids so the next poll re-probes without stale state.
    """
    store = PriceStore()
    client = ForecastExClient(gateway_url=GATEWAY, account_id=ACCOUNT)
    collector = ForecastExCollector(
        config=ForecastExConfig(), store=store, client=client,
    )
    collector._inactive_conids = {"111222", "999999"}
    collector._parent_probe_counts = {"111222": 5, "999999": 3}

    collector.reactivate_conid("111222")
    assert "111222" not in collector._inactive_conids
    assert "111222" not in collector._parent_probe_counts
    # Other conid untouched
    assert "999999" in collector._inactive_conids
    assert collector._parent_probe_counts.get("999999") == 3

    # Idempotent — re-call against a not-tracked conid is harmless
    collector.reactivate_conid("777777")
    collector.reactivate_conid("111222")  # already cleared
    await client.close()


async def test_refresh_drops_inactive_for_conids_no_longer_referenced(
    patched_market_map, monkeypatch,
):
    """If a mapping's forecastex conid changes (resolver attached a child,
    or operator did a manual override), the OLD conid disappears from
    MARKET_MAP. The collector's inactive set must be pruned for any
    conid that's no longer referenced — otherwise restored conids stay
    flagged forever.
    """
    store = PriceStore()
    client = ForecastExClient(gateway_url=GATEWAY, account_id=ACCOUNT)
    collector = ForecastExCollector(
        config=ForecastExConfig(), store=store, client=client,
    )
    # Pre-condition: parent 111222 is the current conid AND it's inactive.
    collector._inactive_conids = {"111222", "stale-old"}
    collector._parent_probe_counts = {"111222": 3, "stale-old": 9}

    # Operator (or resolver) attaches a different child conid. MARKET_MAP
    # no longer references "stale-old" anywhere.
    new_map = dict(patched_market_map)
    new_map["EXAMPLE_2026_OUTCOME"] = {
        **new_map["EXAMPLE_2026_OUTCOME"], "forecastex": "child-222"
    }
    monkeypatch.setattr(settings_mod, "MARKET_MAP", new_map)
    import arbiter.collectors.forecastex as fcst_mod
    monkeypatch.setattr(fcst_mod, "MARKET_MAP", new_map)

    collector.refresh_tracked_markets()

    # stale-old wasn't referenced → dropped from inactive.
    assert "stale-old" not in collector._inactive_conids
    assert "stale-old" not in collector._parent_probe_counts
    # 111222 also wasn't referenced after the swap → dropped too.
    assert "111222" not in collector._inactive_conids
    # The new conid is tracked. (yes_conid, no_conid_or_empty) tuple shape.
    tracked = collector._conid_map.get("EXAMPLE_2026_OUTCOME")
    assert tracked is not None and tracked[0] == "child-222"
    await client.close()


# ── 2026-06-02 env-tunable circuit breaker ─────────────────────────────────


def test_circuit_breaker_uses_env_threshold(monkeypatch):
    """FORECASTEX_CB_FAILURE_THRESHOLD env overrides the default 10."""
    monkeypatch.setenv("FORECASTEX_CB_FAILURE_THRESHOLD", "25")
    monkeypatch.setenv("FORECASTEX_CB_RECOVERY_TIMEOUT_S", "180")
    c = ForecastExClient(
        gateway_url=GATEWAY, account_id=ACCOUNT, verify_ssl=False, paper_trading=True,
    )
    assert c.circuit.failure_threshold == 25
    assert c.circuit.recovery_timeout == 180


def test_circuit_breaker_defaults_when_env_unset(monkeypatch):
    """Defaults are 10/120 (looser than the historic 5/30 which flapped)."""
    monkeypatch.delenv("FORECASTEX_CB_FAILURE_THRESHOLD", raising=False)
    monkeypatch.delenv("FORECASTEX_CB_RECOVERY_TIMEOUT_S", raising=False)
    c = ForecastExClient(
        gateway_url=GATEWAY, account_id=ACCOUNT, verify_ssl=False, paper_trading=True,
    )
    assert c.circuit.failure_threshold == 10
    assert c.circuit.recovery_timeout == 120


def test_circuit_breaker_invalid_env_falls_back_to_min_one(monkeypatch):
    """Negative / zero env values clamp to 1 to keep the breaker functional."""
    monkeypatch.setenv("FORECASTEX_CB_FAILURE_THRESHOLD", "-3")
    monkeypatch.setenv("FORECASTEX_CB_RECOVERY_TIMEOUT_S", "0")
    c = ForecastExClient(
        gateway_url=GATEWAY, account_id=ACCOUNT, verify_ssl=False, paper_trading=True,
    )
    assert c.circuit.failure_threshold >= 1
    assert c.circuit.recovery_timeout >= 1


async def test_positions_invalidates_ibkr_cache_before_reading(client):
    """IBKR serves /portfolio/{acct}/positions/0 from a cache that goes
    stale/empty after the first read. Live 2026-07-10: the stranded
    reconciler saw the FX lots on cycle 1 and an empty book on every later
    cycle, pruning real positions from the tracker (paired count flapped
    3->1). Every read must POST the invalidate endpoint first; a failed
    invalidate must not fail the read."""
    calls: list[tuple] = []

    async def _record(method, path, **kw):
        calls.append((method, path))
        if path.endswith("/positions/0"):
            return [{"conid": 1, "position": 1.0}]
        return {}

    client.account_id = "U111"
    client._request = _record

    result = await client.positions()

    assert result == [{"conid": 1, "position": 1.0}]
    inv = ("POST", "/portfolio/U111/positions/invalidate")
    read = ("GET", "/portfolio/U111/positions/0")
    assert inv in calls and read in calls
    assert calls.index(inv) < calls.index(read)


async def test_positions_read_survives_invalidate_failure(client):
    calls: list[tuple] = []

    async def _record(method, path, **kw):
        calls.append((method, path))
        if "invalidate" in path:
            raise RuntimeError("akamai 411")
        if path.endswith("/positions/0"):
            return [{"conid": 2, "position": 3.0}]
        return {}

    client.account_id = "U111"
    client._request = _record

    result = await client.positions()
    assert result == [{"conid": 2, "position": 3.0}]


async def test_positions_retries_while_cache_warms_after_invalidate(client, monkeypatch):
    """After POST .../positions/invalidate, IBKR rebuilds the cache
    asynchronously — the immediately-following read returns EMPTY (live
    2026-07-10 22:20Z: even cycle 1 lost the FX lots once per-read
    invalidation landed). Retry the read briefly before accepting empty."""
    sleeps: list[float] = []

    async def _sleep(d):
        sleeps.append(float(d))

    monkeypatch.setattr("arbiter.collectors.forecastex.asyncio.sleep", _sleep)
    reads = {"n": 0}
    calls: list[tuple] = []

    async def _record(method, path, **kw):
        calls.append((method, path))
        if path.endswith("/positions/0"):
            reads["n"] += 1
            if reads["n"] == 1:
                return []          # cache still warming
            return [{"conid": 9, "position": 2.0}]
        return {}

    client.account_id = "U111"
    client._request = _record

    result = await client.positions()

    assert result == [{"conid": 9, "position": 2.0}]
    assert reads["n"] == 2
    assert sleeps, "expected a warm-up sleep between empty read and retry"
    assert ("POST", "/portfolio/U111/positions/invalidate") in calls
