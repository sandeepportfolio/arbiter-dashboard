"""Tests for ForecastExChildResolver.

The resolver re-runs IBKR's child-conid resolution for parent conids
the collector has disabled. Tests exercise: candidate selection,
success path (DB upsert + collector reactivate), IBKR 503/400 buckets,
no-children, dry-run, env config.
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


def _confirmed(cid: str, fx_conid: str):
    return {
        "canonical_id": cid,
        "status": "confirmed",
        "forecastex": fx_conid,
        "kalshi": f"K-{cid}",
        "polymarket": f"P-{cid}",
        "allow_auto_trade": True,
    }


def _stub_mapping(cid: str, fx_conid: str):
    """Stand-in for what mapping_store.get returns — only needs the
    forecastex_contract_id slot the resolver writes back."""
    return SimpleNamespace(canonical_id=cid, forecastex_contract_id=fx_conid)


async def test_run_once_picks_up_inactive_parents_and_attaches_children():
    settings_module.MARKET_MAP["DEM_HOUSE_2026"] = _confirmed("DEM_HOUSE_2026", "733131966")
    settings_module.MARKET_MAP["GAME_X"] = _confirmed("GAME_X", "999999")

    # Collector: parent 733131966 is inactive, 999999 is NOT — so only
    # one candidate should be resolved.
    collector = MagicMock()
    collector._inactive_conids = {"733131966"}
    collector.reactivate_conid = MagicMock()

    client = MagicMock()
    client.resolve_event_children = AsyncMock(return_value=[
        {"conid": "888888", "right": "Y", "source": "secdef/info:POST:NOV26"},
        {"conid": "888889", "right": "N", "source": "secdef/info:POST:NOV26"},
    ])

    store = MagicMock()
    store.get = AsyncMock(return_value=_stub_mapping("DEM_HOUSE_2026", "733131966"))
    store.upsert = AsyncMock()

    res = ForecastExChildResolver(
        forecastex_client=client,
        forecastex_collector=collector,
        mapping_store=store,
    )

    snap = await res.run_once()
    assert snap.candidates_count == 1, "only inactive-parent mappings are candidates"
    assert snap.resolved_count == 1
    assert snap.failed_count == 0
    # Mapping store was called with the YES child conid
    assert store.upsert.await_count == 1
    upserted = store.upsert.await_args.args[0]
    assert upserted.forecastex_contract_id == "888888"
    # Collector was reactivated for BOTH old parent (just in case) and new child
    assert collector.reactivate_conid.call_count == 2
    args = {c.args[0] for c in collector.reactivate_conid.call_args_list}
    assert "733131966" in args and "888888" in args


async def test_run_once_skips_mappings_without_inactive_parent():
    """Mappings whose conid is still being polled successfully (not in
    the inactive set) MUST NOT be re-resolved — they're already working.
    """
    settings_module.MARKET_MAP["WORKING"] = _confirmed("WORKING", "111")
    collector = MagicMock()
    collector._inactive_conids = set()  # nothing disabled
    client = MagicMock()
    client.resolve_event_children = AsyncMock()
    store = MagicMock()

    res = ForecastExChildResolver(
        forecastex_client=client, forecastex_collector=collector, mapping_store=store,
    )
    snap = await res.run_once()
    assert snap.candidates_count == 0
    assert client.resolve_event_children.await_count == 0


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
    """Weekend 503s on IBKR's FORECASTX endpoints get their own
    outcome bucket so the ops UI can clearly say "waiting on venue"
    vs. "code is broken".
    """
    import aiohttp
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
        {"conid": "222", "right": "Y"},
    ])
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


async def test_falls_back_to_first_child_when_no_right_field():
    settings_module.MARKET_MAP["A"] = _confirmed("A", "111")
    collector = MagicMock()
    collector._inactive_conids = {"111"}
    collector.reactivate_conid = MagicMock()
    client = MagicMock()
    # No "right" or "Y" — resolver must still pick something.
    client.resolve_event_children = AsyncMock(return_value=[
        {"conid": "first", "right": ""},
        {"conid": "second", "right": ""},
    ])
    store = MagicMock()
    store.get = AsyncMock(return_value=_stub_mapping("A", "111"))
    store.upsert = AsyncMock()

    res = ForecastExChildResolver(
        forecastex_client=client, forecastex_collector=collector, mapping_store=store,
    )
    snap = await res.run_once()
    assert snap.resolved_count == 1
    assert snap.attempts[0]["child_conid"] == "first"


def test_resolver_from_env_returns_none_when_wiring_missing(monkeypatch):
    """Dev/test contexts without ForecastEx wired must not crash."""
    assert resolver_from_env(
        forecastex_client=None, forecastex_collector=None, mapping_store=None,
    ) is None


def test_resolver_from_env_clamps_short_interval(monkeypatch):
    """An overly short interval would burn IBKR's rate-limit budget;
    enforce a 60s floor.
    """
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
