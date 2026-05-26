"""Tests for ForecastExChildResolver.

The resolver uses IBKR's documented sectype=OPT discovery flow
(secdef/strikes → secdef/info per strike) to find tradeable YES/NO
child contracts under FORECASTX event parents. Tests exercise the
full pipeline: candidate selection, strike-aware Call (YES) picking,
tradeability snapshot warm-up, no-children handling, IBKR error
buckets, dry-run, env config, and the short-circuit cap.
"""
from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from arbiter.config import settings as settings_module
from arbiter.recovery.forecastex_resolver import (
    ForecastExChildResolver,
    ResolveAttempt,
    resolver_from_env,
)


@pytest.fixture(autouse=True)
def _seed_market_map():
    """Snapshot + restore MARKET_MAP around each test."""
    original = dict(settings_module.MARKET_MAP)
    settings_module.MARKET_MAP.clear()
    yield settings_module.MARKET_MAP
    settings_module.MARKET_MAP.clear()
    settings_module.MARKET_MAP.update(original)


def _confirmed(cid: str, fx_conid: str, not_available: bool = False):
    return {
        "canonical_id": cid,
        "status": "confirmed",
        "forecastex": fx_conid,
        "forecastex_not_available": not_available,
        "kalshi": f"K-{cid}",
        "polymarket": f"P-{cid}",
        "allow_auto_trade": True,
    }


def _stub_mapping(cid: str, fx_conid: str, not_available: bool = False):
    """Mapping-store record stub — exposes the fields the resolver writes."""
    return SimpleNamespace(
        canonical_id=cid,
        forecastex_contract_id=fx_conid,
        forecastex_not_available=not_available,
    )


def _tradeable_snapshot():
    """A snapshot with bid (84), ask (86) — passes is_tradeable_snapshot."""
    return {"31": "C0.78", "84": "0.78", "86": "0.81"}


def _empty_snapshot():
    """A snapshot with no bid/ask — fails is_tradeable_snapshot."""
    return {"conid": "x", "conidEx": "x", "7295": "0.00"}


def _parent_then_tradeable_snapshot(parent_conid: str):
    """AsyncMock side_effect: empty for the parent conid (so the resolver's
    self-heal short-circuit moves past warmup) and tradeable for any other
    conid (so the downstream child tradeability warm-up succeeds).
    """
    async def _side(conid, *args, **kwargs):
        if str(conid) == str(parent_conid):
            return _empty_snapshot()
        return _tradeable_snapshot()
    return _side


async def test_picks_call_yes_child_and_attaches_when_tradeable(monkeypatch):
    """Happy path: resolver picks the Call (YES) child, snapshots it,
    confirms tradeability (bid/ask > 0), and persists the conid to the
    mapping store.
    """
    settings_module.MARKET_MAP["DEM_HOUSE_2026"] = _confirmed(
        "DEM_HOUSE_2026", "733131966",
    )
    collector = MagicMock()
    collector._inactive_conids = {"733131966"}
    collector.reactivate_conid = MagicMock()

    client = MagicMock()
    # Two children: Call (YES) at strike 1, Put (NO) at strike 1.
    client.resolve_event_children = AsyncMock(return_value=[
        {"conid": "762089343", "right": "C", "strike": 1.0,
         "source": "secdef/info:OPT:NOV26:strike=1.0", "desc": "NOV26 1 YES @FORECASTX"},
        {"conid": "762089344", "right": "P", "strike": 1.0,
         "source": "secdef/info:OPT:NOV26:strike=1.0", "desc": "NOV26 1 NO @FORECASTX"},
    ])
    client.market_snapshot = AsyncMock(side_effect=_parent_then_tradeable_snapshot("733131966"))

    store = MagicMock()
    store.get = AsyncMock(return_value=_stub_mapping("DEM_HOUSE_2026", "733131966"))
    store.upsert = AsyncMock()

    res = ForecastExChildResolver(
        forecastex_client=client, forecastex_collector=collector, mapping_store=store,
    )

    snap = await res.run_once()
    assert snap.resolved_count == 1
    assert snap.failed_count == 0
    assert snap.attempts[0]["outcome"] == "resolved"
    assert snap.attempts[0]["child_conid"] == "762089343"
    # Persisted with the Call conid AND not_available reset
    assert store.upsert.await_count == 1
    upserted = store.upsert.await_args.args[0]
    assert upserted.forecastex_contract_id == "762089343"
    assert upserted.forecastex_not_available is False
    # Collector reactivate fired for parent + child
    args = {c.args[0] for c in collector.reactivate_conid.call_args_list}
    assert "733131966" in args and "762089343" in args


async def test_dem_canonical_id_picks_strike_1_call(monkeypatch):
    """Political-control hint: DEM_HOUSE_2026 must pick the Dem-side
    Call (strike 1) when both strikes' Calls are present.
    """
    settings_module.MARKET_MAP["DEM_HOUSE_2026"] = _confirmed(
        "DEM_HOUSE_2026", "733131966",
    )
    collector = MagicMock()
    collector._inactive_conids = {"733131966"}
    client = MagicMock()
    client.resolve_event_children = AsyncMock(return_value=[
        {"conid": "DEM_CALL", "right": "C", "strike": 1.0},
        {"conid": "DEM_PUT",  "right": "P", "strike": 1.0},
        {"conid": "REP_CALL", "right": "C", "strike": 2.0},
        {"conid": "REP_PUT",  "right": "P", "strike": 2.0},
    ])
    client.market_snapshot = AsyncMock(side_effect=_parent_then_tradeable_snapshot("733131966"))
    store = MagicMock()
    store.get = AsyncMock(return_value=_stub_mapping("DEM_HOUSE_2026", "733131966"))
    store.upsert = AsyncMock()

    res = ForecastExChildResolver(
        forecastex_client=client, forecastex_collector=collector, mapping_store=store,
    )
    snap = await res.run_once()
    assert snap.attempts[0]["child_conid"] == "DEM_CALL"


async def test_gop_canonical_id_picks_strike_2_call(monkeypatch):
    """The mirror case: GOP_HOUSE_2026 must pick the Rep-side Call
    (strike 2). Without this hint, both party mappings would attach
    to the same Call conid and the cross-platform arb math breaks.
    """
    settings_module.MARKET_MAP["GOP_HOUSE_2026"] = _confirmed(
        "GOP_HOUSE_2026", "733131966",
    )
    collector = MagicMock()
    collector._inactive_conids = {"733131966"}
    client = MagicMock()
    client.resolve_event_children = AsyncMock(return_value=[
        {"conid": "DEM_CALL", "right": "C", "strike": 1.0},
        {"conid": "REP_CALL", "right": "C", "strike": 2.0},
    ])
    client.market_snapshot = AsyncMock(side_effect=_parent_then_tradeable_snapshot("733131966"))
    store = MagicMock()
    store.get = AsyncMock(return_value=_stub_mapping("GOP_HOUSE_2026", "733131966"))
    store.upsert = AsyncMock()

    res = ForecastExChildResolver(
        forecastex_client=client, forecastex_collector=collector, mapping_store=store,
    )
    snap = await res.run_once()
    assert snap.attempts[0]["child_conid"] == "REP_CALL"


async def test_marks_unavailable_when_child_not_tradeable():
    """When the resolved Call child returns empty bid/ask after 3
    snapshot warm-up attempts (the MLB→CPI mis-mapping case), the
    resolver must mark the mapping as ``forecastex_not_available`` so
    it stops being a candidate. Without this the resolver loops
    forever on the same parent.
    """
    # Use a non-sports canonical_id so the sports-quarantine pre-filter
    # does not intercept it before the snapshot-warmup path runs. That
    # quarantine is tested separately (see
    # ``test_quarantines_sports_tagged_candidates``).
    settings_module.MARKET_MAP["FED_RATE_HIKE_X"] = _confirmed("FED_RATE_HIKE_X", "831072197")
    collector = MagicMock()
    collector._inactive_conids = {"831072197"}
    collector.reactivate_conid = MagicMock()

    client = MagicMock()
    client.resolve_event_children = AsyncMock(return_value=[
        {"conid": "882350597", "right": "C", "strike": 305.0,
         "desc": "MAY26 305 YES @FORECASTX"},
    ])
    # Snapshot returns empty fields → not tradeable across all warm-ups
    client.market_snapshot = AsyncMock(return_value=_empty_snapshot())

    store = MagicMock()
    store.get = AsyncMock(return_value=_stub_mapping("FED_RATE_HIKE_X", "831072197"))
    store.upsert = AsyncMock()

    res = ForecastExChildResolver(
        forecastex_client=client, forecastex_collector=collector, mapping_store=store,
    )
    snap = await res.run_once()
    assert snap.attempts[0]["outcome"] == "marked_unavailable"
    # mapping_store.upsert was called with forecastex_not_available=True
    assert store.upsert.await_count == 1
    upserted = store.upsert.await_args.args[0]
    assert upserted.forecastex_not_available is True


async def test_quarantines_sports_tagged_candidates():
    """Sports-tagged mappings (MLB/NFL/NBA/NHL game IDs etc.) MUST be
    quarantined before the resolver burns IBKR rate budget on them.

    The 2026-05-25 incident saw 39 wrong-domain attachments (Kalshi
    sports games auto-bound to FORECASTX regional-CPI events because
    they shared a city-name token). The pre-flight filter recognises
    sports tokens in the mapping and marks the conid unavailable
    immediately, with no IBKR calls fired.
    """
    settings_module.MARKET_MAP["KX_MLB_NYY_LAD"] = _confirmed("KX_MLB_NYY_LAD", "999000111")
    collector = MagicMock()
    collector._inactive_conids = {"999000111"}
    collector.reactivate_conid = MagicMock()

    client = MagicMock()
    # Both IBKR calls below should NEVER be invoked because we filter
    # the sports candidate out before the strikes/info path runs.
    client.resolve_event_children = AsyncMock(return_value=[])
    client.market_snapshot = AsyncMock(return_value=_empty_snapshot())

    store = MagicMock()
    store.get = AsyncMock(return_value=_stub_mapping("KX_MLB_NYY_LAD", "999000111"))
    store.upsert = AsyncMock()

    res = ForecastExChildResolver(
        forecastex_client=client, forecastex_collector=collector, mapping_store=store,
    )
    snap = await res.run_once()
    # No IBKR strikes/info call — that's the whole point of the filter.
    assert client.resolve_event_children.await_count == 0
    # Mapping was marked unavailable so it stops re-appearing.
    assert store.upsert.await_count == 1
    upserted = store.upsert.await_args.args[0]
    assert upserted.forecastex_not_available is True
    # Cycle had zero remaining candidates after the quarantine.
    assert snap.candidates_count == 0


async def test_skips_mappings_flagged_not_available():
    """forecastex_not_available=true mappings MUST NOT re-enter the
    candidate pool — otherwise marked_unavailable becomes a perpetual
    re-mark cycle.
    """
    settings_module.MARKET_MAP["WAS_BAD"] = _confirmed(
        "WAS_BAD", "111", not_available=True,
    )
    collector = MagicMock()
    collector._inactive_conids = {"111"}
    client = MagicMock()
    client.resolve_event_children = AsyncMock()
    store = MagicMock()

    res = ForecastExChildResolver(
        forecastex_client=client, forecastex_collector=collector, mapping_store=store,
    )
    snap = await res.run_once()
    assert snap.candidates_count == 0
    assert client.resolve_event_children.await_count == 0


async def test_no_call_in_children_marks_unavailable():
    """A child set with only Puts (no Call) means there's no YES
    contract for us to attach. Mark unavailable instead of looping.
    """
    settings_module.MARKET_MAP["A"] = _confirmed("A", "111")
    collector = MagicMock()
    collector._inactive_conids = {"111"}
    client = MagicMock()
    client.resolve_event_children = AsyncMock(return_value=[
        {"conid": "P1", "right": "P", "strike": 1.0},
        {"conid": "P2", "right": "P", "strike": 2.0},
    ])
    client.market_snapshot = AsyncMock(return_value=_empty_snapshot())
    store = MagicMock()
    store.get = AsyncMock(return_value=_stub_mapping("A", "111"))
    store.upsert = AsyncMock()

    res = ForecastExChildResolver(
        forecastex_client=client, forecastex_collector=collector, mapping_store=store,
    )
    snap = await res.run_once()
    assert snap.attempts[0]["outcome"] == "marked_unavailable"


async def test_run_once_skips_mappings_without_inactive_parent():
    settings_module.MARKET_MAP["WORKING"] = _confirmed("WORKING", "111")
    collector = MagicMock()
    collector._inactive_conids = set()
    client = MagicMock()
    client.resolve_event_children = AsyncMock()
    store = MagicMock()

    res = ForecastExChildResolver(
        forecastex_client=client, forecastex_collector=collector, mapping_store=store,
    )
    snap = await res.run_once()
    assert snap.candidates_count == 0


async def test_skips_non_confirmed_mappings():
    settings_module.MARKET_MAP["CANDIDATE"] = {
        **_confirmed("CANDIDATE", "555"),
        "status": "candidate",
    }
    collector = MagicMock()
    collector._inactive_conids = {"555"}
    client = MagicMock()
    client.resolve_event_children = AsyncMock()
    store = MagicMock()

    res = ForecastExChildResolver(
        forecastex_client=client, forecastex_collector=collector, mapping_store=store,
    )
    snap = await res.run_once()
    assert snap.candidates_count == 0


async def test_no_children_returned_records_attempt_but_does_not_upsert():
    settings_module.MARKET_MAP["A"] = _confirmed("A", "111")
    collector = MagicMock()
    collector._inactive_conids = {"111"}
    client = MagicMock()
    client.resolve_event_children = AsyncMock(return_value=[])
    store = MagicMock()
    store.upsert = AsyncMock()

    res = ForecastExChildResolver(
        forecastex_client=client, forecastex_collector=collector, mapping_store=store,
    )
    snap = await res.run_once()
    assert snap.resolved_count == 0
    assert snap.failed_count == 1
    assert store.upsert.await_count == 0
    assert snap.attempts[0]["outcome"] == "no_children"


async def test_ibkr_503_classified_distinct_from_other_errors():
    from aiohttp import ClientResponseError, RequestInfo
    from yarl import URL

    settings_module.MARKET_MAP["A"] = _confirmed("A", "111")
    collector = MagicMock()
    collector._inactive_conids = {"111"}

    req_info = RequestInfo(
        url=URL("https://ibkr/info"), method="GET", headers={}, real_url=URL("https://ibkr/info"),
    )
    exc = ClientResponseError(req_info, (), status=503, message="Service Unavailable")
    client = MagicMock()
    client.resolve_event_children = AsyncMock(side_effect=exc)
    store = MagicMock()

    res = ForecastExChildResolver(
        forecastex_client=client, forecastex_collector=collector, mapping_store=store,
    )
    snap = await res.run_once()
    assert snap.attempts[0]["outcome"] == "ibkr_503"
    assert snap.failed_count == 1


async def test_dry_run_does_not_call_upsert():
    settings_module.MARKET_MAP["A"] = _confirmed("A", "111")
    collector = MagicMock()
    collector._inactive_conids = {"111"}
    collector.reactivate_conid = MagicMock()
    client = MagicMock()
    client.resolve_event_children = AsyncMock(return_value=[
        {"conid": "222", "right": "C", "strike": 1.0},
    ])
    client.market_snapshot = AsyncMock(side_effect=_parent_then_tradeable_snapshot("111"))
    store = MagicMock()
    store.upsert = AsyncMock()

    res = ForecastExChildResolver(
        forecastex_client=client, forecastex_collector=collector,
        mapping_store=store, dry_run=True,
    )
    snap = await res.run_once()
    assert snap.attempts[0]["outcome"] == "dry_run"
    assert snap.attempts[0]["child_conid"] == "222"
    assert store.upsert.await_count == 0
    assert collector.reactivate_conid.call_count == 0


async def test_self_heal_reactivates_tradeable_child_without_re_resolving():
    """Regression — if a previously-resolved child conid (e.g. 762089343
    DEM_HOUSE) is wrongly added to the collector's inactive set during
    an IBKR snapshot warmup race, the resolver must:
      1. Detect the conid is ALREADY a tradeable child (snapshot returns
         bid/ask) BEFORE calling resolve_event_children.
      2. Reactivate it in the collector and return outcome=
         ``reactivated_child`` — NOT consume IBKR rate-limit budget on
         a futile parent→child enumeration cycle.

    Without this, the resolver would call resolve_event_children on a
    child OPT conid (which IBKR returns no strikes for), record
    ``no_children`` forever, and the wrongly-disabled child would
    remain inactive across resolver cycles.
    """
    settings_module.MARKET_MAP["DEM_HOUSE_2026"] = _confirmed(
        "DEM_HOUSE_2026", "762089343",
    )
    collector = MagicMock()
    collector._inactive_conids = {"762089343"}
    collector.reactivate_conid = MagicMock()

    client = MagicMock()
    # The candidate conid itself returns a tradeable snapshot —
    # signalling it's a child that was wrongly disabled, not a parent.
    client.market_snapshot = AsyncMock(return_value=_tradeable_snapshot())
    client.resolve_event_children = AsyncMock()

    store = MagicMock()
    store.upsert = AsyncMock()

    res = ForecastExChildResolver(
        forecastex_client=client, forecastex_collector=collector, mapping_store=store,
    )
    snap = await res.run_once()

    assert snap.resolved_count == 1
    assert snap.attempts[0]["outcome"] == "reactivated_child"
    assert snap.attempts[0]["child_conid"] == "762089343"
    # Critical: resolve_event_children must NOT have been called for
    # the already-tradeable child — that would waste IBKR rate-limit
    # budget and uselessly probe an OPT conid's nonexistent option chain.
    assert client.resolve_event_children.await_count == 0
    # Collector reactivation MUST fire so the next collector cycle
    # actually picks the conid back up.
    collector.reactivate_conid.assert_called_once_with("762089343")
    # No mapping mutation — we kept the existing child_id intact.
    assert store.upsert.await_count == 0


async def test_self_heal_falls_through_to_resolution_when_snapshot_empty():
    """Inverse of the self-heal test: when the candidate conid returns
    an empty snapshot (true parent), the resolver must proceed with the
    normal resolve_event_children flow rather than short-circuiting.
    """
    settings_module.MARKET_MAP["A"] = _confirmed("A", "111")
    collector = MagicMock()
    collector._inactive_conids = {"111"}
    collector.reactivate_conid = MagicMock()

    client = MagicMock()
    # Parent (111) snapshots empty → self-heal does NOT short-circuit.
    # Child (222) snapshots tradeable → tradeability check passes.
    client.market_snapshot = AsyncMock(side_effect=_parent_then_tradeable_snapshot("111"))
    client.resolve_event_children = AsyncMock(return_value=[
        {"conid": "222", "right": "C", "strike": 1.0,
         "source": "secdef/info:OPT:NOV26:strike=1.0"},
    ])

    store = MagicMock()
    store.get = AsyncMock(return_value=_stub_mapping("A", "111"))
    store.upsert = AsyncMock()

    res = ForecastExChildResolver(
        forecastex_client=client, forecastex_collector=collector, mapping_store=store,
    )
    snap = await res.run_once()
    assert snap.attempts[0]["outcome"] == "resolved"
    assert client.resolve_event_children.await_count == 1


def test_resolver_from_env_returns_none_when_wiring_missing(monkeypatch):
    """Dev/test contexts without ForecastEx wired must not crash."""
    assert resolver_from_env(
        forecastex_client=None, forecastex_collector=None, mapping_store=None,
    ) is None


def test_resolver_from_env_clamps_short_interval(monkeypatch):
    monkeypatch.setenv("FX_CHILD_RESOLVE_INTERVAL_S", "5")
    res = resolver_from_env(
        forecastex_client=object(),
        forecastex_collector=object(),
        mapping_store=object(),
    )
    assert res is not None
    assert res._interval_s == 60.0
    assert res._dry_run is False


def test_resolver_from_env_honors_dry_run(monkeypatch):
    monkeypatch.setenv("FX_RESOLVER_DRY_RUN", "true")
    res = resolver_from_env(
        forecastex_client=object(),
        forecastex_collector=object(),
        mapping_store=object(),
    )
    assert res is not None
    assert res._dry_run is True


async def test_run_once_short_circuits_after_three_consecutive_503s():
    """When IBKR's /iserver/secdef/* endpoint is down, the resolver
    must short-circuit after 3 consecutive failures so the cycle
    doesn't block the asyncio loop for hours."""
    from aiohttp import ClientResponseError, RequestInfo
    from yarl import URL

    for i in range(10):
        settings_module.MARKET_MAP[f"C{i}"] = _confirmed(f"C{i}", f"P{i}")
    collector = MagicMock()
    collector._inactive_conids = {f"P{i}" for i in range(10)}

    req_info = RequestInfo(
        url=URL("https://ibkr/info"), method="GET", headers={}, real_url=URL("https://ibkr/info"),
    )
    exc = ClientResponseError(req_info, (), status=503, message="SU")
    client = MagicMock()
    client.resolve_event_children = AsyncMock(side_effect=exc)
    store = MagicMock()

    res = ForecastExChildResolver(
        forecastex_client=client, forecastex_collector=collector, mapping_store=store,
    )
    snap = await res.run_once()
    assert snap.candidates_count == 10
    assert snap.failed_count == 10
    assert client.resolve_event_children.await_count == 3
    outcomes = {a["outcome"] for a in snap.attempts}
    assert outcomes == {"ibkr_503"}


async def test_wait_for_trigger_returns_early_when_event_fires(monkeypatch):
    """The event-driven wakeup primitive must return well before the
    periodic interval. This is the production bug: a fixed sleep made
    cycle 1 race past the collector's first disable batch and the next
    cycle was 30 min away."""
    # Stub the debounce sleep so the test doesn't actually wait 60s.
    monkeypatch.setattr(
        "arbiter.recovery.forecastex_resolver.asyncio.sleep",
        AsyncMock(),
    )
    res = ForecastExChildResolver(
        forecastex_client=MagicMock(),
        forecastex_collector=MagicMock(),
        mapping_store=MagicMock(),
        interval_s=3600.0,  # 1 hour — far longer than the test
    )
    # Fire the trigger right before the wait so wait_for sees the
    # event already set and returns immediately.
    res.trigger()
    t0 = time.monotonic()
    await res._wait_for_trigger_or_timeout(3600.0)
    elapsed = time.monotonic() - t0
    # Should return in well under a second despite the 3600s timeout.
    assert elapsed < 1.0
    # Event was cleared before the debounce so the next iteration
    # waits fresh.
    assert not res._wake_event.is_set()


async def test_wait_for_trigger_honors_timeout_when_no_trigger(monkeypatch):
    """If no trigger fires, the wait must time out cleanly (not raise)
    and return — that's the periodic-tick path."""
    monkeypatch.setattr(
        "arbiter.recovery.forecastex_resolver.asyncio.sleep",
        AsyncMock(),
    )
    res = ForecastExChildResolver(
        forecastex_client=MagicMock(),
        forecastex_collector=MagicMock(),
        mapping_store=MagicMock(),
    )
    # No trigger before the wait. A very short timeout completes
    # without anyone setting the event — verifies the TimeoutError
    # path is swallowed.
    await res._wait_for_trigger_or_timeout(0.01)
    assert not res._wake_event.is_set()


async def test_strikes_cache_hits_avoid_re_querying_same_parent(monkeypatch):
    """Two canonical_ids sharing the same parent conid must only cost
    ONE strikes call. Verified live 2026-05-26: HORC parent serves
    both DEM_HOUSE and GOP_HOUSE, so the cache cuts strikes calls in
    half whenever a political-control event spans both party sides.
    """
    settings_module.MARKET_MAP["DEM_HOUSE_2026"] = _confirmed("DEM_HOUSE_2026", "733131966")
    settings_module.MARKET_MAP["GOP_HOUSE_2026"] = _confirmed("GOP_HOUSE_2026", "733131966")
    collector = MagicMock()
    collector._inactive_conids = {"733131966"}
    collector.reactivate_conid = MagicMock()

    client = MagicMock()
    client.resolve_event_children = AsyncMock(return_value=[
        {"conid": "DEM_CALL", "right": "C", "strike": 1.0},
        {"conid": "REP_CALL", "right": "C", "strike": 2.0},
    ])
    client.market_snapshot = AsyncMock(side_effect=_parent_then_tradeable_snapshot("733131966"))
    store = MagicMock()
    store.get = AsyncMock(side_effect=[
        _stub_mapping("DEM_HOUSE_2026", "733131966"),
        _stub_mapping("GOP_HOUSE_2026", "733131966"),
    ])
    store.upsert = AsyncMock()

    # Stub the pacing sleep so the test stays under a second.
    monkeypatch.setattr(
        "arbiter.recovery.forecastex_resolver.asyncio.sleep", AsyncMock(),
    )

    res = ForecastExChildResolver(
        forecastex_client=client, forecastex_collector=collector, mapping_store=store,
    )
    snap = await res.run_once()

    # Both candidates resolved, but ONE strikes call (cache hit on the
    # second).
    assert snap.resolved_count == 2
    assert client.resolve_event_children.await_count == 1
    assert "733131966" in res._strikes_cache


async def test_pacing_waits_between_distinct_parent_strikes_calls(monkeypatch):
    """Successive strikes calls for DIFFERENT parents must be paced.
    The wrapper enforces ``_strikes_min_gap_s`` between back-to-back
    /iserver/secdef/strikes hits so IBKR's bucket has time to refill —
    without it, all 41 candidates 429'd in one prod cycle.
    """
    settings_module.MARKET_MAP["A"] = _confirmed("A", "P_A")
    settings_module.MARKET_MAP["B"] = _confirmed("B", "P_B")
    collector = MagicMock()
    collector._inactive_conids = {"P_A", "P_B"}
    collector.reactivate_conid = MagicMock()

    client = MagicMock()
    # Empty result so nothing gets cached and both candidates pay
    # the pacing toll.
    client.resolve_event_children = AsyncMock(return_value=[])
    client.market_snapshot = AsyncMock(side_effect=lambda c: _empty_snapshot())
    store = MagicMock()

    sleep_calls: list[float] = []

    async def _record_sleep(delay):
        sleep_calls.append(delay)

    monkeypatch.setattr(
        "arbiter.recovery.forecastex_resolver.asyncio.sleep", _record_sleep,
    )

    res = ForecastExChildResolver(
        forecastex_client=client, forecastex_collector=collector, mapping_store=store,
    )
    res._strikes_min_gap_s = 3.0
    await res.run_once()

    # At least one sleep in [2.5, 3.0] (the pacing gap between A and B).
    pacing_sleeps = [s for s in sleep_calls if 2.5 <= s <= 3.0]
    assert pacing_sleeps, (
        f"expected a pacing sleep ~3s between distinct strikes calls; "
        f"got sleeps={sleep_calls}"
    )


async def test_empty_strikes_result_is_not_cached(monkeypatch):
    """An empty children list (parent has no FX option chain, OR a
    transient 503/429-exhaustion) must NOT poison the cache —
    subsequent cycles need to retry rather than be told permanently
    that this parent has no children."""
    settings_module.MARKET_MAP["X"] = _confirmed("X", "P_X")
    collector = MagicMock()
    collector._inactive_conids = {"P_X"}
    collector.reactivate_conid = MagicMock()

    client = MagicMock()
    client.resolve_event_children = AsyncMock(return_value=[])
    client.market_snapshot = AsyncMock(side_effect=lambda c: _empty_snapshot())
    store = MagicMock()

    monkeypatch.setattr(
        "arbiter.recovery.forecastex_resolver.asyncio.sleep", AsyncMock(),
    )

    res = ForecastExChildResolver(
        forecastex_client=client, forecastex_collector=collector, mapping_store=store,
    )
    await res.run_once()
    assert "P_X" not in res._strikes_cache


def test_trigger_is_idempotent_and_safe_before_loop_start():
    """trigger() must be safe to call before start() — wiring code may
    register it during initialization, before the loop is running."""
    res = ForecastExChildResolver(
        forecastex_client=MagicMock(),
        forecastex_collector=MagicMock(),
        mapping_store=MagicMock(),
    )
    # Multiple calls before any loop runs must not raise.
    res.trigger()
    res.trigger()
    res.trigger()
    assert res._wake_event.is_set()
