"""Unit tests for /api/metrics handler (Phase 6 Plan 06-04).

Builds a thin ArbiterAPI with fake scanner/engine/safety and asserts the
Prometheus-text response shape + the counter/gauge lines we promise to
external scrapers.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from arbiter.api import ArbiterAPI


class _FakeScanner:
    stats = {
        "scan_count": 42,
        "active_opportunities": 3,
        "best_edge_cents": 7.5,
        "last_scan_ms": 1.2,
    }


class _FakeEngine:
    stats = {
        "live": 2,
        "simulated": 5,
        "manual": 1,
        "incidents": 0,
        "recoveries": 0,
        "aborted": 0,
        "total_pnl": 0.42,
    }


class _FakeSafety:
    is_armed = False


class _FakeCircuit:
    state = "closed"


class _FakeLimiter:
    available_tokens = 8
    remaining_penalty_seconds = 0.0


class _FakeCollector:
    circuit = _FakeCircuit()
    rate_limiter = _FakeLimiter()


def _build_api(with_auto_executor: bool = False) -> ArbiterAPI:
    config = SimpleNamespace(scanner=SimpleNamespace(dry_run=True))
    api = ArbiterAPI(
        price_store=MagicMock(),
        scanner=_FakeScanner(),
        engine=_FakeEngine(),
        monitor=MagicMock(),
        config=config,
        collectors={"kalshi": _FakeCollector(), "polymarket": _FakeCollector()},
        safety=_FakeSafety(),
    )
    if with_auto_executor:
        stats = SimpleNamespace(
            considered=10,
            executed=3,
            skipped_disabled=4,
            skipped_armed=1,
            skipped_requires_manual=0,
            skipped_not_allowed=1,
            skipped_duplicate=0,
            skipped_over_cap=0,
            skipped_bootstrap_full=1,
            failures=0,
        )
        api.auto_executor = SimpleNamespace(stats=stats)
    return api


@pytest.mark.asyncio
async def test_metrics_response_is_prometheus_text():
    api = _build_api()
    resp = await api.handle_metrics(make_mocked_request("GET", "/api/metrics"))
    assert isinstance(resp, web.Response)
    assert resp.content_type == "text/plain"
    body = resp.text
    assert "# HELP arbiter_build_info" in body
    assert "# TYPE arbiter_scanner_scans_total counter" in body


@pytest.mark.asyncio
async def test_metrics_includes_scanner_stats():
    api = _build_api()
    resp = await api.handle_metrics(make_mocked_request("GET", "/api/metrics"))
    body = resp.text
    assert "arbiter_scanner_scans_total 42" in body
    assert "arbiter_scanner_active_opportunities 3" in body
    assert "arbiter_scanner_best_edge_cents 7.5" in body
    assert "arbiter_scanner_last_scan_ms 1.2" in body


@pytest.mark.asyncio
async def test_metrics_includes_execution_counters():
    api = _build_api()
    resp = await api.handle_metrics(make_mocked_request("GET", "/api/metrics"))
    body = resp.text
    assert 'arbiter_executions_total{status="live"} 2' in body
    assert 'arbiter_executions_total{status="simulated"} 5' in body
    assert 'arbiter_executions_total{status="manual"} 1' in body
    assert "arbiter_pnl_total 0.42" in body


@pytest.mark.asyncio
async def test_metrics_includes_kill_switch_state():
    api = _build_api()
    resp = await api.handle_metrics(make_mocked_request("GET", "/api/metrics"))
    assert "arbiter_kill_switch_armed 0" in resp.text

    api.safety.is_armed = True
    resp2 = await api.handle_metrics(make_mocked_request("GET", "/api/metrics"))
    assert "arbiter_kill_switch_armed 1" in resp2.text


@pytest.mark.asyncio
async def test_metrics_includes_per_platform_circuit_and_limiter():
    api = _build_api()
    resp = await api.handle_metrics(make_mocked_request("GET", "/api/metrics"))
    body = resp.text
    assert 'arbiter_circuit_state{platform="kalshi"} 0' in body
    assert 'arbiter_circuit_state{platform="polymarket"} 0' in body
    assert 'arbiter_rate_limiter_tokens{platform="kalshi"} 8' in body
    assert 'arbiter_rate_limiter_penalty_seconds{platform="kalshi"} 0.0' in body


@pytest.mark.asyncio
async def test_metrics_auto_executor_stats_when_attached():
    api = _build_api(with_auto_executor=True)
    resp = await api.handle_metrics(make_mocked_request("GET", "/api/metrics"))
    body = resp.text
    assert "arbiter_auto_executor_considered 10" in body
    assert "arbiter_auto_executor_executed 3" in body
    assert 'arbiter_auto_executor_skipped{reason="disabled"} 4' in body
    assert 'arbiter_auto_executor_skipped{reason="bootstrap_full"} 1' in body


@pytest.mark.asyncio
async def test_metrics_auto_executor_absent_is_safe():
    api = _build_api(with_auto_executor=False)
    resp = await api.handle_metrics(make_mocked_request("GET", "/api/metrics"))
    body = resp.text
    # No auto-executor lines should appear when not attached
    assert "arbiter_auto_executor_considered" not in body


@pytest.mark.asyncio
async def test_metrics_circuit_open_maps_to_2():
    api = _build_api()
    api.collectors["kalshi"].circuit.state = "open"
    resp = await api.handle_metrics(make_mocked_request("GET", "/api/metrics"))
    assert 'arbiter_circuit_state{platform="kalshi"} 2' in resp.text
