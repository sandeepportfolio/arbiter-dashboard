"""Regression tests for the continuous market-discovery loop.

The batch /api/discovery/status endpoint is separate from this loop; these tests
pin the runtime loop contract directly so discovery visibility cannot drift back
to "idle forever" while the background task is doing work.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import arbiter.main as main_module
from arbiter.main import run_market_discovery_loop


class _MappingStore:
    def __init__(self, pending: int = 0):
        self.pending = pending
        self.refresh_runtime_cache = AsyncMock()

    async def count_candidates(self) -> int:
        return self.pending


def _runtime(*, enabled: bool) -> dict:
    return {
        "auto_discovery_enabled": enabled,
        "auto_discovery_interval_seconds": 15.0,
        "auto_discovery_budget_rps": 2.0,
        "auto_discovery_min_score": 0.25,
        "auto_discovery_max_candidates": 500,
        "auto_promote_enabled": False,
        "auto_promote_min_score": 0.78,
        "auto_promote_daily_cap": 250,
        "auto_promote_advisory_scans": 0,
        "auto_promote_max_days": 400,
    }


@pytest.mark.asyncio
async def test_market_discovery_loop_skips_work_and_sleeps_when_disabled(monkeypatch):
    discover = AsyncMock(return_value=99)
    monkeypatch.setattr(main_module, "discover_market_mappings", discover)
    monkeypatch.setattr(main_module, "load_market_discovery_settings", lambda store: _runtime(enabled=False))
    monkeypatch.setattr(main_module, "OperatorSettingsStore", lambda: object())

    sleep_delays: list[float] = []

    async def fake_sleep(delay: float):
        sleep_delays.append(delay)
        raise asyncio.CancelledError

    monkeypatch.setattr(main_module.asyncio, "sleep", fake_sleep)
    metrics: dict = {"auto_discovery_last_written": 123}

    with pytest.raises(asyncio.CancelledError):
        await run_market_discovery_loop(
            SimpleNamespace(),
            SimpleNamespace(),
            _MappingStore(pending=17),
            metrics=metrics,
        )

    discover.assert_not_awaited()
    assert sleep_delays == [15.0]
    assert metrics["auto_discovery_last_written"] == 0


@pytest.mark.asyncio
async def test_market_discovery_loop_runs_pass_and_updates_metrics_when_enabled(monkeypatch):
    discover = AsyncMock(return_value=5)
    monkeypatch.setattr(main_module, "discover_market_mappings", discover)
    monkeypatch.setattr(main_module, "load_market_discovery_settings", lambda store: _runtime(enabled=True))
    monkeypatch.setattr(main_module, "OperatorSettingsStore", lambda: object())

    sleep_delays: list[float] = []

    async def fake_sleep(delay: float):
        sleep_delays.append(delay)
        raise asyncio.CancelledError

    monkeypatch.setattr(main_module.asyncio, "sleep", fake_sleep)
    mapping_store = _MappingStore(pending=42)
    kalshi = SimpleNamespace(refresh_tracked_markets=MagicMock())
    polymarket = SimpleNamespace(client=SimpleNamespace(), refresh_tracked_markets=MagicMock())
    metrics: dict = {}

    with pytest.raises(asyncio.CancelledError):
        await run_market_discovery_loop(
            kalshi,
            polymarket,
            mapping_store,
            metrics=metrics,
        )

    discover.assert_awaited_once()
    mapping_store.refresh_runtime_cache.assert_awaited_once()
    kalshi.refresh_tracked_markets.assert_called_once()
    polymarket.refresh_tracked_markets.assert_called_once()
    assert sleep_delays == [15.0]
    assert metrics["auto_discovery_last_written"] == 5
    assert metrics["auto_discovery_candidates_pending"] == 42
    assert metrics["auto_discovery_last_pass_at"] > 0


@pytest.mark.asyncio
async def test_market_discovery_loop_surfaces_kp_metrics_before_slow_fx_attachment(monkeypatch):
    discover = AsyncMock(return_value=7)

    async def fx_down(*args, **kwargs):
        assert metrics["auto_discovery_last_written"] == 7
        assert metrics["auto_discovery_candidates_pending"] == 31
        assert metrics["auto_discovery_last_pass_at"] > 0
        raise RuntimeError("secdef unavailable")

    monkeypatch.setattr(main_module, "discover_market_mappings", discover)
    monkeypatch.setattr(main_module, "discover_forecastex_mappings", fx_down)
    monkeypatch.setattr(main_module, "load_market_discovery_settings", lambda store: _runtime(enabled=True))
    monkeypatch.setattr(main_module, "OperatorSettingsStore", lambda: object())

    async def fake_sleep(delay: float):
        raise asyncio.CancelledError

    monkeypatch.setattr(main_module.asyncio, "sleep", fake_sleep)
    mapping_store = _MappingStore(pending=31)
    kalshi = SimpleNamespace(refresh_tracked_markets=MagicMock())
    polymarket = SimpleNamespace(client=SimpleNamespace(), refresh_tracked_markets=MagicMock())
    forecastex = SimpleNamespace(client=object(), refresh_tracked_markets=MagicMock())
    metrics: dict = {}

    with pytest.raises(asyncio.CancelledError):
        await run_market_discovery_loop(
            kalshi,
            polymarket,
            mapping_store,
            forecastex=forecastex,
            metrics=metrics,
        )

    discover.assert_awaited_once()
    mapping_store.refresh_runtime_cache.assert_awaited_once()
    assert metrics["auto_discovery_last_written"] == 7
    assert metrics["auto_discovery_candidates_pending"] == 31
    assert metrics["auto_discovery_last_pass_at"] > 0
