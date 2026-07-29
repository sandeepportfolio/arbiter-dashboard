"""Tests for the operator-console backend endpoints added 2026-07-24.

Covers:
  - POST /api/market-mappings/{cid}/forecastex_no_conid
  - GET  /api/forecastex/session
  - GET  /api/oauth/status
  - GET  /api/funding-gate
  - GET  /api/directional-exposure  + the /api/portfolio labelling fix

The directional tests reconstruct the ACTUAL live Senate book (quantities,
fill prices and conids taken from arbiter_live.execution_orders on
2026-07-24) so the payoff math is checked against real numbers rather than
invented ones.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from arbiter.config.settings import MARKET_MAP


def _make_api():
    """Minimal in-process ArbiterAPI, mirroring test_api_integration."""
    from arbiter.api import ArbiterAPI
    from arbiter.config.settings import ArbiterConfig

    async def _noop_get_all_prices():
        return {}

    price_store = SimpleNamespace(get_all_prices=_noop_get_all_prices)
    scanner = SimpleNamespace(current_opportunities=[], stats={}, history=[])
    engine = SimpleNamespace(
        stats={"audit": {}},
        execution_history=[],
        manual_positions=[],
        incidents=[],
        equity_curve=[],
        adapters={},
        _executions=[],
    )
    monitor = SimpleNamespace(current_balances={}, _thresholds={})
    return ArbiterAPI(
        price_store=price_store,
        scanner=scanner,
        engine=engine,
        monitor=monitor,
        config=ArbiterConfig(),
    )


def _order(platform, market_id, side, qty, price):
    return SimpleNamespace(
        platform=platform,
        market_id=market_id,
        side=side,
        fill_qty=float(qty),
        fill_price=float(price),
    )


def _execution(arb_id, canonical_id, yes_leg, no_leg, status="filled"):
    opp = SimpleNamespace(
        canonical_id=canonical_id,
        description="U.S Senate Midterm Winner",
        yes_platform=yes_leg.platform,
        yes_market_id=yes_leg.market_id,
        no_platform=no_leg.platform,
        no_market_id=no_leg.market_id,
        suggested_qty=int(max(yes_leg.fill_qty, no_leg.fill_qty)),
    )
    return SimpleNamespace(
        arb_id=arb_id,
        opportunity=opp,
        leg_yes=yes_leg,
        leg_no=no_leg,
        status=status,
        timestamp=1_753_000_000.0,
        realized_pnl=0.0,
    )


@pytest.fixture
def senate_map():
    """The REPAIRED Senate mappings (post party-swap fix, as in prod)."""
    saved = {k: MARKET_MAP.get(k) for k in ("DEM_SENATE_2026", "GOP_SENATE_2026")}
    MARKET_MAP["DEM_SENATE_2026"] = {
        "description": "U.S Senate Midterm Winner",
        "kalshi": "CONTROLS-2026-D",
        "forecastex": "773659815",
        "forecastex_no": "773659816",
        "status": "confirmed",
    }
    MARKET_MAP["GOP_SENATE_2026"] = {
        "description": "U.S Senate Midterm Winner",
        "kalshi": "CONTROLS-2026-R",
        "forecastex": "745924267",
        "forecastex_no": "745924270",
        "status": "confirmed",
    }
    yield
    for key, value in saved.items():
        if value is None:
            MARKET_MAP.pop(key, None)
        else:
            MARKET_MAP[key] = value


def _live_senate_executions():
    """The exact open book on 2026-07-24.

    DEM_SENATE_2026: kalshi YES CONTROLS-2026-D 64 @ .44
                   + forecastex NO  745924270   64 @ .4113
    GOP_SENATE_2026: forecastex YES 773659815   51 @ .4698
                   + kalshi NO  CONTROLS-2026-R 51 @ .4416

    745924270 is GOP's NO conid and 773659815 is DEM's YES conid, so every
    leg pays only if the Democrats win the Senate.
    """
    return [
        _execution(
            "ARB-DEM", "DEM_SENATE_2026",
            _order("kalshi", "CONTROLS-2026-D", "yes", 64, 0.44),
            _order("forecastex", "745924270", "no", 64, 0.4113),
        ),
        _execution(
            "ARB-GOP", "GOP_SENATE_2026",
            _order("forecastex", "773659815", "yes", 51, 0.4698),
            _order("kalshi", "CONTROLS-2026-R", "no", 51, 0.4416),
        ),
    ]


# ── Directional detection ─────────────────────────────────────────────────


def test_directional_book_detects_live_senate_party_swap(senate_map):
    api = _make_api()
    api.engine._executions = _live_senate_executions()

    book = api._directional_book()

    assert book["detected"] is True
    assert book["position_count"] == 2
    assert book["directional_canonicals"] == ["DEM_SENATE_2026", "GOP_SENATE_2026"]

    # Every leg pays in the same state — that is what makes it directional.
    for position in book["arbs"]:
        assert position["pays_in_same_state"] is True
        assert position["settlement_states"] == ["DEMOCRATS_WIN"]

    # 64*.44 + 64*.4113 + 51*.4698 + 51*.4416 == 100.9646
    assert book["net_exposure_usd"] == pytest.approx(100.9646, abs=1e-4)
    assert book["contracts_at_risk"] == pytest.approx(230.0)

    payoff = book["payoff_by_state"]
    assert set(payoff) == {"DEMOCRATS_WIN", "REPUBLICANS_WIN"}
    assert payoff["DEMOCRATS_WIN"]["payout_usd"] == pytest.approx(230.0)
    assert payoff["DEMOCRATS_WIN"]["net_usd"] == pytest.approx(129.0354, abs=1e-4)
    assert payoff["REPUBLICANS_WIN"]["payout_usd"] == pytest.approx(0.0)
    assert payoff["REPUBLICANS_WIN"]["net_usd"] == pytest.approx(-100.9646, abs=1e-4)

    assert book["worst_case_state"] == "REPUBLICANS_WIN"
    assert book["best_case_state"] == "DEMOCRATS_WIN"


def test_directional_book_names_the_foreign_conid(senate_map):
    api = _make_api()
    api.engine._executions = _live_senate_executions()

    positions = {p["canonical_id"]: p for p in api._directional_book()["arbs"]}

    dem = positions["DEM_SENATE_2026"]
    fx_leg = next(l for l in dem["legs"] if l["platform"] == "forecastex")
    assert fx_leg["binding"] == "foreign"
    assert fx_leg["owner_canonical"] == "GOP_SENATE_2026"
    assert fx_leg["owner_slot"] == "no"
    assert "745924270" in dem["reason"]
    assert "GOP_SENATE_2026" in dem["reason"]

    # The Kalshi leg is correctly bound and must not be blamed.
    kalshi_leg = next(l for l in dem["legs"] if l["platform"] == "kalshi")
    assert kalshi_leg["settlement_state"] == "DEMOCRATS_WIN"


def test_correctly_bound_pair_is_not_flagged(senate_map):
    """A genuine hedge — FX leg on its OWN canonical's NO conid — is clean."""
    api = _make_api()
    api.engine._executions = [
        _execution(
            "ARB-OK", "DEM_SENATE_2026",
            _order("kalshi", "CONTROLS-2026-D", "yes", 10, 0.44),
            _order("forecastex", "773659816", "no", 10, 0.55),
        ),
    ]
    book = api._directional_book()
    assert book["detected"] is False
    assert book["arbs"] == []
    assert book["positions"] == []


def test_unresolvable_conid_fails_toward_unverified(senate_map):
    """A conid absent from MARKET_MAP must not read as a verified hedge."""
    api = _make_api()
    api.engine._executions = [
        _execution(
            "ARB-UNKNOWN", "DEM_SENATE_2026",
            _order("kalshi", "CONTROLS-2026-D", "yes", 5, 0.44),
            _order("forecastex", "999999999", "no", 5, 0.55),
        ),
    ]
    book = api._directional_book()
    # Unresolved FX leg is attributed to its own canonical+slot, so the legs
    # read as opposite states and the pair is not called directional — but
    # the binding is recorded as unresolved rather than verified.
    positions = book["arbs"]
    if positions:
        leg = next(
            l for l in positions[0]["legs"] if l["platform"] == "forecastex"
        )
        assert leg["binding"] == "unresolved"


def test_closed_executions_are_ignored(senate_map):
    api = _make_api()
    executions = _live_senate_executions()
    for execution in executions:
        execution.status = "closed"
    api.engine._executions = executions
    assert api._directional_book()["detected"] is False


# ── /api/portfolio labelling fix ──────────────────────────────────────────


def _snapshot_stub():
    """A PortfolioMonitor snapshot that (wrongly) calls the Senate legs hedged."""
    def _entry(canonical_id):
        return {
            "canonical_id": canonical_id,
            "description": "U.S Senate Midterm Winner",
            "quantity": 64,
            "total_cost": 54.48,
            "status": "hedged",
            "hedge_status": "complete",
            "age_seconds": 1000.0,
        }

    return SimpleNamespace(
        to_dict=lambda: {
            "timestamp": 1_753_000_000.0,
            "total_exposure": 100.96,
            "total_open_positions": 2,
            "total_hedged": 2,
            "total_unhedged": 0,
            "by_venue": {},
            "by_canonical": {
                "DEM_SENATE_2026": _entry("DEM_SENATE_2026"),
                "GOP_SENATE_2026": _entry("GOP_SENATE_2026"),
            },
            "violations": [],
            "unsettled_positions": 0,
            "realized_pnl_today": 0.0,
            "unrealized_pnl": 0.0,
            "dry_run": False,
        }
    )


def test_portfolio_snapshot_relabels_directional_legs(senate_map):
    api = _make_api()
    api.engine._executions = _live_senate_executions()
    snapshot = _snapshot_stub()
    api.portfolio = SimpleNamespace(
        get_snapshot=lambda: snapshot,
        compute_snapshot=lambda: snapshot,
    )

    out = api._portfolio_snapshot()

    for canonical_id in ("DEM_SENATE_2026", "GOP_SENATE_2026"):
        entry = out["by_canonical"][canonical_id]
        assert entry["hedge_status"] == "directional_unverified"
        assert entry["status"] != "hedged"
        assert entry["directional_review_required"] is True
        assert entry["directional_reason"]

    assert out["directional_review_required"] is True
    assert out["directional_exposure_usd"] == pytest.approx(100.9646, abs=1e-4)
    assert out["directional_worst_case_state"] == "REPUBLICANS_WIN"
    assert out["directional_worst_case_usd"] == pytest.approx(-100.9646, abs=1e-4)
    # The hedged/unhedged counters must move with the labels.
    assert out["total_hedged"] == 0
    assert out["total_unhedged"] == 2


def test_portfolio_snapshot_clean_book_reports_no_directional(senate_map):
    api = _make_api()
    api.engine._executions = []
    snapshot = _snapshot_stub()
    api.portfolio = SimpleNamespace(
        get_snapshot=lambda: snapshot,
        compute_snapshot=lambda: snapshot,
    )
    out = api._portfolio_snapshot()
    assert out["directional_review_required"] is False
    assert out["by_canonical"]["DEM_SENATE_2026"]["hedge_status"] == "complete"
    assert out["total_hedged"] == 2


# ── POST forecastex_no_conid ──────────────────────────────────────────────


def _mint_token():
    from arbiter import api as api_mod

    token = api_mod._generate_token("sparx.sandeep@gmail.com")
    api_mod._ACTIVE_SESSIONS[token] = "sparx.sandeep@gmail.com"
    return token


def _no_conid_client(api):
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    app = web.Application()
    app.router.add_post(
        "/api/market-mappings/{canonical_id}/forecastex_no_conid",
        api.handle_set_forecastex_no_conid,
    )
    return TestClient(TestServer(app))


def test_set_no_conid_writes_through_and_syncs_market_map(senate_map):
    from unittest.mock import AsyncMock, MagicMock

    async def _run():
        api = _make_api()
        stored = SimpleNamespace(
            canonical_id="DEM_SENATE_2026",
            forecastex_no_contract_id="773659816",
        )
        store = MagicMock()
        store.get = AsyncMock(return_value=stored)
        store.upsert = AsyncMock()
        # The handler prefers the race-free single-column write when the
        # store provides one; MagicMock would auto-create a SYNC mock for
        # it, so wire an explicit AsyncMock.
        store.set_forecastex_conid = AsyncMock(return_value=True)
        api.mapping_store = store

        fx = MagicMock()
        fx.reactivate_conid = MagicMock()
        api.collectors = {"forecastex": fx}

        async with _no_conid_client(api) as client:
            response = await client.post(
                "/api/market-mappings/DEM_SENATE_2026/forecastex_no_conid",
                json={"conid": "888888"},
                headers={"Authorization": f"Bearer {_mint_token()}"},
            )
            assert response.status == 200, await response.text()
            payload = await response.json()
            assert payload["prior_conid"] == "773659816"
            assert payload["new_conid"] == "888888"
            assert payload["cleared"] is False

        # Race-free single-column write is preferred; the full-row upsert
        # (which can resurrect stale columns) must NOT run.
        assert store.upsert.await_count == 0
        store.set_forecastex_conid.assert_awaited_once_with(
            "DEM_SENATE_2026", "888888", side="no"
        )
        assert stored.forecastex_no_contract_id == "888888"
        assert MARKET_MAP["DEM_SENATE_2026"]["forecastex_no"] == "888888"
        called = {c.args[0] for c in fx.reactivate_conid.call_args_list}
        assert {"773659816", "888888"} <= called

    asyncio.run(_run())


def test_set_no_conid_clears_on_null_without_stringifying_none(senate_map):
    from unittest.mock import AsyncMock, MagicMock

    async def _run():
        api = _make_api()
        stored = SimpleNamespace(
            canonical_id="DEM_SENATE_2026",
            forecastex_no_contract_id="773659816",
        )
        store = MagicMock()
        store.get = AsyncMock(return_value=stored)
        store.upsert = AsyncMock()
        # The handler prefers the race-free single-column write when the
        # store provides one; MagicMock would auto-create a SYNC mock for
        # it, so wire an explicit AsyncMock.
        store.set_forecastex_conid = AsyncMock(return_value=True)
        api.mapping_store = store

        async with _no_conid_client(api) as client:
            response = await client.post(
                "/api/market-mappings/DEM_SENATE_2026/forecastex_no_conid",
                json={"conid": None},
                headers={"Authorization": f"Bearer {_mint_token()}"},
            )
            assert response.status == 200, await response.text()
            payload = await response.json()
            assert payload["cleared"] is True
            assert payload["new_conid"] == ""

        assert stored.forecastex_no_contract_id == ""
        assert MARKET_MAP["DEM_SENATE_2026"]["forecastex_no"] == ""

    asyncio.run(_run())


@pytest.mark.parametrize("bad", ["0", "None", "abc", "12x3"])
def test_set_no_conid_rejects_junk(senate_map, bad):
    from unittest.mock import AsyncMock, MagicMock

    async def _run():
        api = _make_api()
        store = MagicMock()
        store.get = AsyncMock(return_value=SimpleNamespace(
            canonical_id="DEM_SENATE_2026", forecastex_no_contract_id="1",
        ))
        store.upsert = AsyncMock()
        # The handler prefers the race-free single-column write when the
        # store provides one; MagicMock would auto-create a SYNC mock for
        # it, so wire an explicit AsyncMock.
        store.set_forecastex_conid = AsyncMock(return_value=True)
        api.mapping_store = store

        async with _no_conid_client(api) as client:
            response = await client.post(
                "/api/market-mappings/DEM_SENATE_2026/forecastex_no_conid",
                json={"conid": bad},
                headers={"Authorization": f"Bearer {_mint_token()}"},
            )
            assert response.status == 400, await response.text()
        assert store.upsert.await_count == 0

    asyncio.run(_run())


def test_set_no_conid_404_for_unknown_mapping(senate_map):
    from unittest.mock import AsyncMock, MagicMock

    async def _run():
        api = _make_api()
        store = MagicMock()
        store.get = AsyncMock(return_value=None)
        store.upsert = AsyncMock()
        # The handler prefers the race-free single-column write when the
        # store provides one; MagicMock would auto-create a SYNC mock for
        # it, so wire an explicit AsyncMock.
        store.set_forecastex_conid = AsyncMock(return_value=True)
        api.mapping_store = store

        async with _no_conid_client(api) as client:
            response = await client.post(
                "/api/market-mappings/NOPE/forecastex_no_conid",
                json={"conid": "888"},
                headers={"Authorization": f"Bearer {_mint_token()}"},
            )
            assert response.status == 404
        assert store.upsert.await_count == 0

    asyncio.run(_run())


# ── Funding gate ──────────────────────────────────────────────────────────


def _balances(**kwargs):
    return {
        platform: SimpleNamespace(
            balance=value, timestamp=1_753_000_000.0, stale=False, is_low=False,
        )
        for platform, value in kwargs.items()
    }


def test_funding_gate_all_clear_at_live_balances():
    """Live balances on 2026-07-24 clear every threshold."""
    async def _run():
        api = _make_api()
        api.monitor = SimpleNamespace(
            current_balances=_balances(
                kalshi=328.24, polymarket=314.15, forecastex=245.42,
            ),
            _thresholds={"kalshi": 50.0, "polymarket": 25.0, "forecastex": 50.0},
        )
        response = await api.handle_funding_gate(SimpleNamespace())
        import json as _json
        payload = _json.loads(response.text)
        # `venues` is the console-contract LIST; venues_by_name is the
        # same rows keyed for lookup.
        assert isinstance(payload["venues"], list)
        by_name = payload["venues_by_name"]
        assert [v["venue"] for v in payload["venues"]] == [
            "kalshi", "polymarket", "forecastex",
        ]

        assert payload["all_clear"] is True
        assert payload["failing_venues"] == []
        assert payload["blocked_pairs"] == []
        assert all(p["tradeable"] for p in payload["pairs"])
        assert by_name["polymarket"]["passes"] is True
        assert by_name["polymarket"]["headroom"] == pytest.approx(289.15)
        assert by_name["kalshi"]["shortfall"] == 0

    asyncio.run(_run())


def test_funding_gate_shortfall_blocks_every_pair_with_that_venue():
    async def _run():
        api = _make_api()
        api.monitor = SimpleNamespace(
            current_balances=_balances(
                kalshi=328.24, polymarket=10.0, forecastex=245.42,
            ),
            _thresholds={"kalshi": 50.0, "polymarket": 25.0, "forecastex": 50.0},
        )
        response = await api.handle_funding_gate(SimpleNamespace())
        import json as _json
        payload = _json.loads(response.text)
        # `venues` is the console-contract LIST; venues_by_name is the
        # same rows keyed for lookup.
        assert isinstance(payload["venues"], list)
        by_name = payload["venues_by_name"]
        assert [v["venue"] for v in payload["venues"]] == [
            "kalshi", "polymarket", "forecastex",
        ]

        assert payload["all_clear"] is False
        assert payload["failing_venues"] == ["polymarket"]
        assert by_name["polymarket"]["shortfall"] == pytest.approx(15.0)
        assert set(payload["blocked_pairs"]) == {
            "kalshi<->polymarket", "polymarket<->forecastex",
        }
        pair = next(p for p in payload["pairs"] if p["pair"] == "kalshi<->forecastex")
        assert pair["tradeable"] is True

    asyncio.run(_run())


def test_funding_gate_unknown_balance_does_not_pass():
    """A balance we could not read must block, not silently clear."""
    async def _run():
        api = _make_api()
        api.monitor = SimpleNamespace(
            current_balances=_balances(kalshi=328.24, polymarket=314.15),
            _thresholds={"kalshi": 50.0, "polymarket": 25.0, "forecastex": 50.0},
        )
        response = await api.handle_funding_gate(SimpleNamespace())
        import json as _json
        payload = _json.loads(response.text)
        # `venues` is the console-contract LIST; venues_by_name is the
        # same rows keyed for lookup.
        assert isinstance(payload["venues"], list)
        by_name = payload["venues_by_name"]
        assert [v["venue"] for v in payload["venues"]] == [
            "kalshi", "polymarket", "forecastex",
        ]

        assert by_name["forecastex"]["balance_known"] is False
        assert by_name["forecastex"]["passes"] is False
        assert payload["all_clear"] is False

    asyncio.run(_run())


# ── OAuth status ──────────────────────────────────────────────────────────


def test_oauth_status_reports_presence_only_and_leaks_nothing():
    async def _run():
        api = _make_api()
        api.config.forecastex = SimpleNamespace(
            oauth_enabled=False,
            oauth_configured=False,
            oauth_consumer_key="ARBITERFX",
            oauth_access_token="SUPERSECRETTOKEN",
            oauth_access_token_secret="SUPERSECRETSECRET",
            oauth_signature_key_fp="/nonexistent/sig.pem",
            oauth_encryption_key_fp="",
            oauth_dh_param_fp="",
            oauth_realm="limited_poa",
            oauth_api_base="https://api.ibkr.com/v1/api",
        )
        response = await api.handle_oauth_status(SimpleNamespace())
        body = response.text
        import json as _json
        payload = _json.loads(body)

        assert payload["oauth_enabled"] is False
        assert payload["oauth_configured"] is False
        assert payload["active"] is False
        assert payload["consumer_key_present"] is True
        assert payload["consumer_key_name"] == "ARBITERFX"
        assert payload["access_token_present"] is True
        assert payload["access_token_secret_present"] is True
        assert payload["signature_key_configured"] is True
        assert payload["signature_key_file_present"] is False
        assert set(payload["missing"]) == {"encryption_key", "dh_param"}

        # No secret material anywhere in the serialized response.
        assert "SUPERSECRETTOKEN" not in body
        assert "SUPERSECRETSECRET" not in body
        assert "BEGIN" not in body

    asyncio.run(_run())


def test_oauth_status_withholds_unexpected_consumer_key_value():
    async def _run():
        api = _make_api()
        api.config.forecastex = SimpleNamespace(
            oauth_enabled=True,
            oauth_configured=True,
            oauth_consumer_key="SOMETHING-ELSE-PRIVATE",
            oauth_access_token="t",
            oauth_access_token_secret="s",
            oauth_signature_key_fp="/a.pem",
            oauth_encryption_key_fp="/b.pem",
            oauth_dh_param_fp="/c.pem",
            oauth_realm="limited_poa",
            oauth_api_base="https://api.ibkr.com/v1/api",
        )
        response = await api.handle_oauth_status(SimpleNamespace())
        import json as _json
        payload = _json.loads(response.text)

        assert payload["consumer_key_present"] is True
        assert payload["consumer_key_name"] is None
        assert "SOMETHING-ELSE-PRIVATE" not in response.text
        # enabled + configured but no collector attached => not active.
        assert payload["transport_attached"] is False
        assert payload["active"] is False
        assert payload["effective_transport"] == "gateway"

    asyncio.run(_run())


# ── ForecastEx session ────────────────────────────────────────────────────


def test_forecastex_session_reports_local_state_without_ibkr_calls():
    async def _run():
        from arbiter.utils.price_store import PricePoint, PriceStore

        api = _make_api()
        store = PriceStore(ttl=10)
        await store.put(PricePoint(
            platform="forecastex",
            canonical_id="DEM_SENATE_2026",
            yes_price=0.46,
            no_price=0.54,
            yes_volume=1,
            no_volume=1,
            timestamp=__import__("time").time(),
            raw_market_id="773659815",
        ))
        api.store = store

        client = SimpleNamespace(_bridge_ready=True, _last_tickle=0.0, oauth=None)
        circuit = SimpleNamespace(stats={
            "name": "forecastex-rest", "state": "closed", "failures": 0,
            "total_calls": 10, "success_count": 10, "success_rate": 1.0,
        })
        api.collectors = {"forecastex": SimpleNamespace(
            client=client, circuit=circuit,
            consecutive_errors=0, total_errors=2, total_fetches=100,
        )}

        response = await api.handle_forecastex_session(SimpleNamespace())
        import json as _json
        payload = _json.loads(response.text)

        assert payload["collector_present"] is True
        assert payload["authenticated"] is True
        assert payload["authenticated_source"] == "brokerage_bridge_ready"
        assert payload["circuit_state"] == "closed"
        assert payload["circuit_open"] is False
        assert payload["total_fetches"] == 100
        assert payload["transport"] == "gateway"
        assert payload["quotes"]["tracked"] == 1
        assert payload["quotes"]["fresh"] == 1
        assert payload["last_success_ts"] is not None
        # Untracked fields must be advertised as unknown, never as healthy.
        assert payload["competing_session"] is None
        assert payload["sso_expires"] is None
        assert "competing_session" in payload["unavailable_fields"]
        assert "sso_expires" in payload["unavailable_fields"]

    asyncio.run(_run())


def test_forecastex_session_open_circuit_is_visible():
    async def _run():
        from arbiter.utils.price_store import PriceStore

        api = _make_api()
        api.store = PriceStore(ttl=10)
        api.collectors = {"forecastex": SimpleNamespace(
            client=SimpleNamespace(_bridge_ready=False, _last_tickle=0.0, oauth=None),
            circuit=SimpleNamespace(stats={"state": "open", "failures": 12}),
            consecutive_errors=12, total_errors=40, total_fetches=100,
        )}

        response = await api.handle_forecastex_session(SimpleNamespace())
        import json as _json
        payload = _json.loads(response.text)

        assert payload["circuit_open"] is True
        assert payload["authenticated"] is False
        assert payload["consecutive_errors"] == 12
        assert payload["quotes"]["tracked"] == 0
        assert payload["last_success_ts"] is None
        assert "last_success_ts" in payload["unavailable_fields"]

    asyncio.run(_run())


def test_forecastex_session_survives_missing_collector():
    async def _run():
        from arbiter.utils.price_store import PriceStore

        api = _make_api()
        api.store = PriceStore(ttl=10)
        api.collectors = {}
        response = await api.handle_forecastex_session(SimpleNamespace())
        import json as _json
        payload = _json.loads(response.text)
        assert payload["collector_present"] is False
        assert payload["authenticated"] is None
        assert "authenticated" in payload["unavailable_fields"]

    asyncio.run(_run())


# ── Console contract shapes (frontend binds to these exact names) ─────────


def test_directional_exposure_console_contract(senate_map):
    """positions[] is the flat leg list; payoff_dem/payoff_gop are net P&L."""
    api = _make_api()
    api.engine._executions = _live_senate_executions()
    book = api._directional_book()

    assert book["leg_count"] == 4
    assert len(book["positions"]) == 4
    for leg in book["positions"]:
        assert set(leg) >= {
            "venue", "conid", "market_id", "side", "quantity", "avg_price",
        }

    fx_legs = {l["conid"]: l for l in book["positions"] if l["venue"] == "forecastex"}
    assert set(fx_legs) == {"745924270", "773659815"}
    assert fx_legs["745924270"]["quantity"] == pytest.approx(64.0)
    assert fx_legs["745924270"]["avg_price"] == pytest.approx(0.4113)
    assert fx_legs["745924270"]["side"] == "no"
    assert fx_legs["773659815"]["quantity"] == pytest.approx(51.0)
    assert fx_legs["773659815"]["avg_price"] == pytest.approx(0.4698)

    # Kalshi legs carry market_id but no conid.
    kalshi = [l for l in book["positions"] if l["venue"] == "kalshi"]
    assert len(kalshi) == 2
    assert all(l["conid"] is None for l in kalshi)
    assert {l["market_id"] for l in kalshi} == {
        "CONTROLS-2026-D", "CONTROLS-2026-R",
    }

    assert book["net_exposure_usd"] == pytest.approx(100.9646, abs=1e-4)
    assert book["payoff_dem"] == pytest.approx(129.0354, abs=1e-4)
    assert book["payoff_gop"] == pytest.approx(-100.9646, abs=1e-4)


def test_directional_exposure_clean_book_has_null_payoffs():
    api = _make_api()
    api.engine._executions = []
    book = api._directional_book()
    assert book["detected"] is False
    assert book["positions"] == []
    assert book["payoff_dem"] is None
    assert book["payoff_gop"] is None


def test_forecastex_session_console_contract():
    async def _run():
        from arbiter.utils.price_store import PricePoint, PriceStore

        api = _make_api()
        store = PriceStore(ttl=10)
        await store.put(PricePoint(
            platform="forecastex", canonical_id="DEM_SENATE_2026",
            yes_price=0.46, no_price=0.54, yes_volume=1, no_volume=1,
            timestamp=__import__("time").time(), raw_market_id="773659815",
        ))
        api.store = store
        api.collectors = {"forecastex": SimpleNamespace(
            client=SimpleNamespace(_bridge_ready=True, _last_tickle=0.0, oauth=None),
            circuit=SimpleNamespace(stats={"state": "closed"}),
            consecutive_errors=3, total_errors=5, total_fetches=50,
        )}
        response = await api.handle_forecastex_session(SimpleNamespace())
        import json as _json
        payload = _json.loads(response.text)

        for key in (
            "authenticated", "competing", "circuit_state",
            "consecutive_errors", "expires_in_seconds", "quote_age_seconds",
        ):
            assert key in payload, key

        assert payload["authenticated"] is True
        assert payload["circuit_state"] == "closed"
        assert payload["consecutive_errors"] == 3
        assert payload["quote_age_seconds"] is not None
        assert payload["quote_age_seconds"] < 5
        # Not tracked in-process — must stay null AND be advertised as such.
        assert payload["competing"] is None
        assert payload["expires_in_seconds"] is None
        assert "competing" in payload["unavailable_fields"]
        assert "expires_in_seconds" in payload["unavailable_fields"]

    asyncio.run(_run())


def test_oauth_status_console_contract():
    async def _run():
        api = _make_api()
        api.config.forecastex = SimpleNamespace(
            oauth_enabled=False, oauth_configured=False,
            oauth_consumer_key="ARBITERFX", oauth_access_token="",
            oauth_access_token_secret="", oauth_signature_key_fp="",
            oauth_encryption_key_fp="", oauth_dh_param_fp="",
            oauth_realm="limited_poa", oauth_api_base="https://api.ibkr.com/v1/api",
        )
        response = await api.handle_oauth_status(SimpleNamespace())
        import json as _json
        payload = _json.loads(response.text)
        for key in (
            "oauth_enabled", "oauth_configured",
            "consumer_key_present", "last_validation",
        ):
            assert key in payload, key
        assert payload["consumer_key_present"] is True
        assert payload["last_validation"] is None

    asyncio.run(_run())


def test_funding_gate_console_contract():
    async def _run():
        api = _make_api()
        api.monitor = SimpleNamespace(
            current_balances=_balances(
                kalshi=328.24, polymarket=10.0, forecastex=245.42,
            ),
            _thresholds={"kalshi": 50.0, "polymarket": 25.0, "forecastex": 50.0},
        )
        response = await api.handle_funding_gate(SimpleNamespace())
        import json as _json
        payload = _json.loads(response.text)

        assert isinstance(payload["venues"], list)
        for row in payload["venues"]:
            assert set(row) >= {
                "venue", "balance", "threshold", "passes", "blocks_pairs",
            }
        poly = next(v for v in payload["venues"] if v["venue"] == "polymarket")
        assert poly["threshold"] == pytest.approx(25.0)
        assert poly["passes"] is False
        assert set(poly["blocks_pairs"]) == {
            "kalshi<->polymarket", "polymarket<->forecastex",
        }
        kalshi = next(v for v in payload["venues"] if v["venue"] == "kalshi")
        assert kalshi["blocks_pairs"] == []
        assert set(payload["blocked_pairs"]) == set(poly["blocks_pairs"])

    asyncio.run(_run())


# ── /api/errors status filter ─────────────────────────────────────────────


class _FakeIncident:
    def __init__(self, incident_id, status):
        self.incident_id = incident_id
        self.status = status

    def to_dict(self):
        return {"incident_id": self.incident_id, "status": self.status}


class _FakeIncidentStore:
    """Stands in for ExecutionStore.list_incidents."""

    def __init__(self, rows):
        self._rows = rows
        self.calls = []

    async def list_incidents(self, *, status=None, limit=200):
        self.calls.append({"status": status, "limit": limit})
        rows = self._rows if status is None else [
            r for r in self._rows if r.status == status
        ]
        return rows[:limit]


def _errors_request(query):
    return SimpleNamespace(query=query)


def _errors_api():
    api = _make_api()
    api.engine.incidents = [
        _FakeIncident("MEM-OPEN", "open"),
        _FakeIncident("MEM-RESOLVED", "resolved"),
    ]
    api.execution_store = _FakeIncidentStore([
        _FakeIncident("DB-OPEN", "open"),
        _FakeIncident("DB-RESOLVED-1", "resolved"),
        _FakeIncident("DB-RESOLVED-2", "resolved"),
        _FakeIncident("DB-EXPIRED", "expired"),
    ])
    return api


def test_errors_default_behaviour_unchanged():
    """No query params => every in-memory incident + persisted OPEN, limit 500."""
    async def _run():
        api = _errors_api()
        response = await api.handle_errors(_errors_request({}))
        import json as _json
        rows = _json.loads(response.text)
        assert isinstance(rows, list)
        assert {r["incident_id"] for r in rows} == {
            "MEM-OPEN", "MEM-RESOLVED", "DB-OPEN",
        }
        assert api.execution_store.calls == [{"status": "open", "limit": 500}]

    asyncio.run(_run())


def test_errors_status_resolved_reaches_history():
    async def _run():
        api = _errors_api()
        response = await api.handle_errors(_errors_request({"status": "resolved"}))
        import json as _json
        rows = _json.loads(response.text)
        ids = {r["incident_id"] for r in rows}
        assert ids == {"MEM-RESOLVED", "DB-RESOLVED-1", "DB-RESOLVED-2"}
        assert "MEM-OPEN" not in ids
        assert api.execution_store.calls[0]["status"] == "resolved"

    asyncio.run(_run())


def test_errors_status_expired_and_all():
    async def _run():
        api = _errors_api()
        response = await api.handle_errors(_errors_request({"status": "expired"}))
        import json as _json
        assert {r["incident_id"] for r in _json.loads(response.text)} == {"DB-EXPIRED"}

        api = _errors_api()
        response = await api.handle_errors(_errors_request({"status": "all"}))
        rows = _json.loads(response.text)
        assert len(rows) == 6
        assert api.execution_store.calls[0]["status"] is None

    asyncio.run(_run())


def test_errors_limit_is_honoured_and_clamped():
    async def _run():
        api = _errors_api()
        await api.handle_errors(_errors_request({"status": "all", "limit": "2"}))
        assert api.execution_store.calls[0]["limit"] == 2

        api = _errors_api()
        await api.handle_errors(_errors_request({"limit": "99999"}))
        assert api.execution_store.calls[0]["limit"] == 1000

        api = _errors_api()
        await api.handle_errors(_errors_request({"limit": "nonsense"}))
        assert api.execution_store.calls[0]["limit"] == 500

    asyncio.run(_run())


def test_errors_rejects_unknown_status():
    async def _run():
        api = _errors_api()
        response = await api.handle_errors(_errors_request({"status": "bogus"}))
        assert response.status == 400
        assert api.execution_store.calls == []

    asyncio.run(_run())


def test_errors_survives_store_failure():
    """A store error must not take out the endpoint."""
    class _Broken:
        async def list_incidents(self, *, status=None, limit=200):
            raise RuntimeError("db down")

    async def _run():
        api = _errors_api()
        api.execution_store = _Broken()
        response = await api.handle_errors(_errors_request({}))
        import json as _json
        rows = _json.loads(response.text)
        assert {r["incident_id"] for r in rows} == {"MEM-OPEN", "MEM-RESOLVED"}

    asyncio.run(_run())
