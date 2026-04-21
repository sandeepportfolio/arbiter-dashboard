"""Tests for Task 18 — Telegram heartbeat + Prometheus metrics endpoint.

Covers:
  - Heartbeat silent when AUTO_EXECUTE_ENABLED != true.
  - Heartbeat posts when AUTO_EXECUTE_ENABLED=true.
  - All 9 new Task-18 metric names are present in /api/metrics.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from arbiter.notifiers.heartbeat import HeartbeatStatus, run_heartbeat, _format_message


# ─── Heartbeat silent test ────────────────────────────────────────────────────


async def test_heartbeat_silent_when_auto_exec_off(monkeypatch):
    """When AUTO_EXECUTE_ENABLED is unset, run_heartbeat must not call notifier.send.

    We run a single iteration (interval_sec=0 so sleep is instant) and verify
    the notifier is never called.
    """
    monkeypatch.delenv("AUTO_EXECUTE_ENABLED", raising=False)

    notifier = MagicMock()
    notifier.send = AsyncMock(return_value=True)

    call_count = 0

    def _status() -> HeartbeatStatus:
        nonlocal call_count
        call_count += 1
        return HeartbeatStatus(realized_pnl=42.0, open_order_count=3)

    # Cancel after one sleep cycle so the loop terminates.
    async def _run():
        task = asyncio.create_task(
            run_heartbeat(notifier, interval_sec=0, get_status=_status)
        )
        # Yield to let the sleep(0) complete and the loop body execute.
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    await _run()

    notifier.send.assert_not_called()


# ─── Heartbeat posts test ─────────────────────────────────────────────────────


async def test_heartbeat_posts_when_auto_exec_on(monkeypatch):
    """When AUTO_EXECUTE_ENABLED=true, run_heartbeat calls notifier.send with expected shape."""
    monkeypatch.setenv("AUTO_EXECUTE_ENABLED", "true")

    notifier = MagicMock()
    notifier.send = AsyncMock(return_value=True)

    expected_pnl = 123.45
    expected_orders = 7

    def _status() -> HeartbeatStatus:
        return HeartbeatStatus(realized_pnl=expected_pnl, open_order_count=expected_orders)

    async def _run():
        task = asyncio.create_task(
            run_heartbeat(notifier, interval_sec=0, get_status=_status)
        )
        # Let the loop body execute at least once.
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    await _run()

    notifier.send.assert_called()
    call_args = notifier.send.call_args
    message = call_args[0][0]  # positional first arg
    assert "123.45" in message, f"realized_pnl not in message: {message}"
    assert "7" in message, f"open_order_count not in message: {message}"


# ─── Format message test ──────────────────────────────────────────────────────


def test_format_message_contains_pnl_and_orders():
    """_format_message includes realized_pnl and open_order_count."""
    status = HeartbeatStatus(realized_pnl=55.50, open_order_count=2)
    msg = _format_message(status)
    assert "55.50" in msg
    assert "2" in msg
    assert "Heartbeat" in msg


def test_format_message_includes_extra_fields():
    """Extra fields are included in the formatted message."""
    status = HeartbeatStatus(
        realized_pnl=0.0,
        open_order_count=0,
        extra={"scan_count": 42, "uptime_h": 1.5},
    )
    msg = _format_message(status)
    assert "scan_count" in msg
    assert "42" in msg
    assert "uptime_h" in msg


# ─── Metrics endpoint test ────────────────────────────────────────────────────


async def test_metrics_endpoint_exposes_new_metrics():
    """GET /api/metrics response must contain all 9 new Task-18 metric names."""
    from aiohttp.test_utils import TestClient, TestServer
    from aiohttp import web
    from unittest.mock import MagicMock

    # Build the minimal ArbiterAPI instance needed to render the metrics handler.
    from arbiter.api import ArbiterAPI
    from arbiter.utils.price_store import PriceStore
    from arbiter.scanner.arbitrage import ArbitrageScanner
    from arbiter.execution.engine import ExecutionEngine
    from arbiter.monitor.balance import BalanceMonitor
    from arbiter.config.settings import ArbiterConfig

    price_store = MagicMock(spec=PriceStore)
    scanner = MagicMock(spec=ArbitrageScanner)
    scanner.stats = {}
    engine = MagicMock(spec=ExecutionEngine)
    engine.stats = {}
    monitor = MagicMock(spec=BalanceMonitor)
    config = MagicMock(spec=ArbiterConfig)

    api = ArbiterAPI(
        price_store=price_store,
        scanner=scanner,
        engine=engine,
        monitor=monitor,
        config=config,
    )

    # Create a minimal aiohttp app with just the metrics route.
    app = web.Application()
    app.router.add_get("/api/metrics", api.handle_metrics)

    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/api/metrics")
        assert resp.status == 200
        body = await resp.text()

    expected_metric_names = [
        "polymarket_us_rest_latency_p99_ms",
        "polymarket_us_ws_reconnects_total",
        "matched_pair_stream_events_total",
        "matcher_backpressure_drops_total",
        "matched_pair_latency_seconds",
        "auto_discovery_candidates_pending",
        "auto_promote_rejections_total",
        "ed25519_sign_failures_total",
        "ws_subscription_count",
    ]

    missing = [name for name in expected_metric_names if name not in body]
    assert not missing, (
        f"Missing metric names in /api/metrics response: {missing}\n"
        f"Response body (first 2000 chars):\n{body[:2000]}"
    )
