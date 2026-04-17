"""Integration-ish tests for POST /api/kill-switch + GET /api/safety/status.

Uses aiohttp.test_utils.TestServer with a hand-rolled web.Application wired
directly to a real SafetySupervisor (with mocked adapters/notifier/store) —
so we exercise the actual ArbiterAPI handlers without spinning up a subprocess.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from arbiter.api import ArbiterAPI
from arbiter.config.settings import ArbiterConfig, SafetyConfig
from arbiter.safety.supervisor import SafetySupervisor


async def _make_app_and_api(cooldown_seconds: float = 30.0):
    """Build a minimal web.Application exposing just the kill-switch routes."""
    config = ArbiterConfig()
    config.safety = SafetyConfig()
    config.safety.min_cooldown_seconds = cooldown_seconds

    notifier = AsyncMock()
    notifier.send = AsyncMock(return_value=True)

    kalshi_adapter = AsyncMock()
    kalshi_adapter.cancel_all = AsyncMock(return_value=["k1"])
    poly_adapter = AsyncMock()
    poly_adapter.cancel_all = AsyncMock(return_value=["p1"])

    engine = SimpleNamespace(stats={"audit": {}}, execution_history=[], manual_positions=[], incidents=[], equity_curve=[])
    safety = SafetySupervisor(
        config=config.safety,
        engine=engine,
        adapters={"kalshi": kalshi_adapter, "polymarket": poly_adapter},
        notifier=notifier,
        redis=None,
        store=None,
        safety_store=None,
    )

    scanner = SimpleNamespace(current_opportunities=[], stats={}, history=[])
    price_store = SimpleNamespace()

    async def _get_all_prices():
        return {}

    price_store.get_all_prices = _get_all_prices
    monitor = SimpleNamespace(current_balances={})

    api = ArbiterAPI(
        price_store=price_store,
        scanner=scanner,
        engine=engine,
        monitor=monitor,
        config=config,
        safety=safety,
    )

    app = web.Application()
    app.router.add_post("/api/kill-switch", api.handle_kill_switch)
    app.router.add_get("/api/safety/status", api.handle_safety_status)
    app.router.add_get("/api/safety/events", api.handle_safety_events)
    # Auth helpers expect a login endpoint for test session issuance.
    app.router.add_post("/api/auth/login", api.handle_login)
    return app, api, safety


async def _login(client: TestClient, email: str, password: str):
    response = await client.post(
        "/api/auth/login", json={"email": email, "password": password},
    )
    return response


async def test_kill_switch_requires_auth():
    app, _api, _safety = await _make_app_and_api()
    async with TestClient(TestServer(app)) as client:
        response = await client.post(
            "/api/kill-switch", json={"action": "arm", "reason": "manual"},
        )
        assert response.status == 401


async def test_kill_switch_arm_with_auth(monkeypatch):
    # Inject a known test user into the auth module.
    from arbiter import api as api_module

    monkeypatch.setitem(
        api_module.UI_ALLOWED_USERS,
        "test@example.com",
        api_module._hash_password("testpw"),
    )
    app, _api, safety = await _make_app_and_api()
    async with TestClient(TestServer(app)) as client:
        login_resp = await _login(client, "test@example.com", "testpw")
        assert login_resp.status == 200
        response = await client.post(
            "/api/kill-switch",
            json={"action": "arm", "reason": "unit test"},
        )
        assert response.status == 200
        body = await response.json()
        assert body.get("armed") is True
        assert body.get("armed_by", "").startswith("operator:")
        assert safety._state.armed is True


async def test_kill_switch_reset_cooldown_denies(monkeypatch):
    from arbiter import api as api_module

    monkeypatch.setitem(
        api_module.UI_ALLOWED_USERS,
        "test@example.com",
        api_module._hash_password("testpw"),
    )
    # Long cooldown so reset is forced to 400.
    app, _api, safety = await _make_app_and_api(cooldown_seconds=3600.0)
    async with TestClient(TestServer(app)) as client:
        await _login(client, "test@example.com", "testpw")
        await client.post(
            "/api/kill-switch",
            json={"action": "arm", "reason": "arm-first"},
        )
        assert safety._state.armed is True
        response = await client.post(
            "/api/kill-switch",
            json={"action": "reset", "note": "too soon"},
        )
        assert response.status == 400
        body = await response.json()
        assert "cooldown" in body.get("error", "").lower()


async def test_kill_switch_unknown_action_rejected(monkeypatch):
    from arbiter import api as api_module

    monkeypatch.setitem(
        api_module.UI_ALLOWED_USERS,
        "test@example.com",
        api_module._hash_password("testpw"),
    )
    app, _api, _safety = await _make_app_and_api()
    async with TestClient(TestServer(app)) as client:
        await _login(client, "test@example.com", "testpw")
        response = await client.post(
            "/api/kill-switch",
            json={"action": "frobnicate"},
        )
        assert response.status == 400
        body = await response.json()
        assert "Unsupported kill-switch action" in body.get("error", "")
