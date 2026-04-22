import asyncio
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

import aiohttp
import pytest


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind(("127.0.0.1", 0))
        except PermissionError as exc:
            pytest.skip(f"Local socket binding unavailable in this sandbox: {exc}")
        return sock.getsockname()[1]


def wait_for_server(port: int, timeout: float = 15.0) -> None:
    start = time.time()
    while time.time() - start < timeout:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=1) as response:
                if response.status == 200:
                    return
        except Exception:
            time.sleep(0.25)
    raise AssertionError(f"Server on port {port} did not become ready")


def test_api_and_dashboard_contracts():
    port = free_port()
    env = dict(os.environ)
    env["ARBITER_UI_SMOKE_SEED"] = "1"
    env["DRY_RUN"] = "true"
    env["OPS_EMAIL"] = "sparx.sandeep@gmail.com"
    env["OPS_PASSWORD"] = "letmein123"
    settings_path = os.path.join(tempfile.gettempdir(), f"arbiter-operator-settings-{time.time_ns()}.json")
    env["ARBITER_OPERATOR_SETTINGS_PATH"] = settings_path
    # Isolate the subprocess from the developer's .env — the contract test runs
    # arbiter.main in its in-memory fallback mode so it doesn't require a
    # live Postgres/Redis. Empty string (not pop) forces the fallback because
    # settings.py now uses load_dotenv(override=False), so an explicit empty
    # value in the subprocess env overrides the .env file's value.
    env["DATABASE_URL"] = ""
    env["REDIS_URL"] = ""
    proc = subprocess.Popen(
        [sys.executable, "-m", "arbiter.main", "--api-only", "--port", str(port)],
        cwd=os.getcwd(),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    try:
        wait_for_server(port)

        def get_json(path: str):
            with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=5) as response:
                return json.loads(response.read().decode("utf-8"))

        def post_json(path: str, payload: dict, headers: dict | None = None):
            request = urllib.request.Request(
                f"http://127.0.0.1:{port}{path}",
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json", **(headers or {})},
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=5) as response:
                return json.loads(response.read().decode("utf-8"))

        def options(path: str, headers: dict | None = None):
            request = urllib.request.Request(
                f"http://127.0.0.1:{port}{path}",
                headers=headers or {},
                method="OPTIONS",
            )
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, dict(response.headers)

        with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=5) as response:
            public_html = response.read().decode("utf-8")
        assert "ARBITER Live Desk" in public_html
        assert 'id="heroTitle"' in public_html
        assert 'id="connectionOverlay"' in public_html

        with urllib.request.urlopen(f"http://127.0.0.1:{port}/ops", timeout=5) as response:
            ops_html = response.read().decode("utf-8")
        assert "Arbiter Live" in ops_html
        assert 'id="equityChart"' in ops_html

        health = get_json("/api/health")
        assert health["status"] == "ok"
        assert health["probe"] == "liveness"
        assert health["service_ready"] is True
        assert "audit" in health
        assert "profitability" in health
        assert "readiness" in health
        assert "reconciliation" in health

        liveness = get_json("/health")
        assert liveness == {
            "status": "ok",
            "probe": "liveness",
            "mode": "dry-run",
            "uptime_seconds": liveness["uptime_seconds"],
        }

        ready = get_json("/ready")
        assert ready["status"] == "ready"
        assert ready["probe"] == "service_readiness"
        assert ready["ready"] is True
        assert ready["live_trading_endpoint"] == "/api/readiness"

        system = get_json("/api/system")
        assert system["mode"] == "dry-run"
        assert "scanner" in system
        assert "execution" in system
        assert "audit" in system
        assert "profitability" in system
        assert "readiness" in system
        assert "reconciliation" in system
        assert "settings" in system
        assert system["settings"]["mode"]["dry_run"] is True
        assert "counts" in system
        assert "series" in system
        assert "profitability" in system["series"]

        assert isinstance(get_json("/api/opportunities"), list)
        assert isinstance(get_json("/api/trades"), list)
        assert isinstance(get_json("/api/errors"), list)
        assert isinstance(get_json("/api/manual-positions"), list)
        profitability = get_json("/api/profitability")
        assert "verdict" in profitability
        assert "progress" in profitability
        readiness = get_json("/api/readiness")
        assert "ready_for_live_trading" in readiness
        assert isinstance(readiness["checks"], list)
        assert time.time() - readiness["timestamp"] < 5
        reconciliation = get_json("/api/reconciliation")
        assert reconciliation["configured"] is True
        assert reconciliation["reconciliation_count"] >= 1
        assert reconciliation["latest_report"] is not None
        assert isinstance(get_json("/api/manual-positions"), list)
        assert len(get_json("/api/errors")) >= 1

        portfolio = get_json("/api/portfolio")
        assert "dry_run" in portfolio
        assert portfolio["dry_run"] is True
        portfolio_positions = get_json("/api/portfolio/positions")
        assert isinstance(portfolio_positions["positions"], list)
        portfolio_violations = get_json("/api/portfolio/violations")
        assert "violations" in portfolio_violations
        portfolio_summary = get_json("/api/portfolio/summary")
        assert portfolio_summary["dry_run"] is True
        assert "realized_pnl" in portfolio_summary

        login = post_json("/api/auth/login", {"email": "sparx.sandeep@gmail.com", "password": "letmein123"})
        assert login["status"] == "ok"
        assert login["email"] == "sparx.sandeep@gmail.com"
        assert login["token"]
        auth_headers = {"Authorization": f"Bearer {login['token']}"}

        secure_request = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/auth/login",
            data=json.dumps({"email": "sparx.sandeep@gmail.com", "password": "letmein123"}).encode("utf-8"),
            headers={"Content-Type": "application/json", "X-Forwarded-Proto": "https"},
            method="POST",
        )
        with urllib.request.urlopen(secure_request, timeout=5) as response:
            set_cookie = response.headers.get("Set-Cookie", "")
        assert "Secure" in set_cookie
        assert "SameSite=lax" in set_cookie

        with urllib.request.urlopen(
            urllib.request.Request(f"http://127.0.0.1:{port}/api/auth/me", headers=auth_headers),
            timeout=5,
        ) as response:
            auth_me = json.loads(response.read().decode("utf-8"))
        assert auth_me["authenticated"] is True
        assert auth_me["email"] == "sparx.sandeep@gmail.com"

        preflight_status, preflight_headers = options(
            "/api/auth/login",
            headers={
                "Origin": "https://example.com",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "authorization, content-type",
            },
        )
        assert preflight_status == 204
        assert preflight_headers["Access-Control-Allow-Headers"] == "Authorization, Content-Type"

        settings = get_json("/api/settings")
        assert settings["mode"]["dry_run"] is True
        assert settings["auto_executor"]["enabled"] is False
        assert settings["scanner"]["min_edge_cents"] >= 0
        assert settings["mapping"]["auto_discovery_enabled"] is True
        assert settings["mapping"]["auto_discovery_max_candidates"] >= 1

        mappings = get_json("/api/market-mappings")
        assert isinstance(mappings, list)
        assert len(mappings) >= 1
        assert all("canonical_id" in row for row in mappings)
        assert any(row["canonical_id"] == "DEM_HOUSE_2026" for row in mappings)

        # After PredictIt removal, no seeded manual positions exist by default —
        # the seed fixture no longer produces them. The POST-action lifecycle
        # is exercised in arbiter/execution/test_engine.py; this contract test
        # just verifies the endpoint responds with a list.
        manual_positions = get_json("/api/manual-positions")
        assert isinstance(manual_positions, list)
        if manual_positions:
            entered = post_json(
                f"/api/manual-positions/{manual_positions[0]['position_id']}",
                {"action": "mark_entered"},
                headers=auth_headers,
            )
            assert entered["status"] == "entered"

        incidents = get_json("/api/errors")
        resolved = post_json(
            f"/api/errors/{incidents[0]['incident_id']}",
            {"action": "resolve"},
            headers=auth_headers,
        )
        assert resolved["status"] == "resolved"

        settings_update = post_json(
            "/api/settings",
            {
                "scanner": {"min_edge_cents": 4.2, "persistence_scans": 5},
                "alerts": {"kalshi_low": 75, "cooldown": 900},
                "auto_executor": {"enabled": True, "max_position_usd": 42},
                "mapping": {
                    "auto_discovery_enabled": False,
                    "auto_discovery_interval_seconds": 120,
                    "auto_discovery_budget_rps": 4.5,
                    "auto_discovery_min_score": 0.18,
                    "auto_discovery_max_candidates": 900,
                },
            },
            headers=auth_headers,
        )
        assert settings_update["scanner"]["min_edge_cents"] == 4.2
        assert settings_update["scanner"]["persistence_scans"] == 5
        assert settings_update["alerts"]["kalshi_low"] == 75.0
        assert settings_update["alerts"]["cooldown"] == 900.0
        assert settings_update["auto_executor"]["enabled"] is True
        assert settings_update["auto_executor"]["max_position_usd"] == 42.0
        assert settings_update["mapping"]["auto_discovery_enabled"] is False
        assert settings_update["mapping"]["auto_discovery_interval_seconds"] == 120.0
        assert settings_update["mapping"]["auto_discovery_budget_rps"] == 4.5
        assert settings_update["mapping"]["auto_discovery_min_score"] == 0.18
        assert settings_update["mapping"]["auto_discovery_max_candidates"] == 900
        persisted_settings = get_json("/api/settings")
        assert persisted_settings["scanner"]["min_edge_cents"] == 4.2
        assert persisted_settings["mapping"]["auto_discovery_enabled"] is False
        assert persisted_settings["meta"]["persisted"] is True

        mapping_update = post_json(
            "/api/market-mappings/DEM_HOUSE_2026",
            {"action": "confirm", "resolution_match_status": "identical"},
            headers=auth_headers,
        )
        assert mapping_update["status"] == "confirmed"
        auto_trade_enabled = post_json(
            "/api/market-mappings/DEM_HOUSE_2026",
            {"action": "enable_auto_trade"},
            headers=auth_headers,
        )
        assert auto_trade_enabled["status"] == "confirmed"
        assert auto_trade_enabled["allow_auto_trade"] is True

        async def check_ws():
            async with aiohttp.ClientSession() as session:
                async with session.ws_connect(f"http://127.0.0.1:{port}/ws") as ws:
                    message = await ws.receive(timeout=5)
                    assert message.type == aiohttp.WSMsgType.TEXT
                    payload = json.loads(message.data)
                    assert payload["type"] == "bootstrap"
                    assert "payload" in payload
                    await ws.send_json({"action": "ping"})
                    pong = await ws.receive(timeout=5)
                    pong_payload = json.loads(pong.data)
                    assert pong_payload["type"] == "heartbeat"

        asyncio.run(check_ws())
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        if os.path.exists(settings_path):
            os.unlink(settings_path)


# ─── SAFE-04: rate-limit broadcast + /api/system inclusion ───────────────


async def _make_rate_limit_api():
    """Build a minimal in-process ArbiterAPI with two adapters carrying real
    RateLimiter instances so the broadcast loop can publish rate_limit_state
    events and /api/system can include a `rate_limits` key.
    """
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from arbiter.api import ArbiterAPI
    from arbiter.config.settings import ArbiterConfig, SafetyConfig
    from arbiter.utils.retry import RateLimiter

    config = ArbiterConfig()
    config.safety = SafetyConfig()

    kalshi_rl = RateLimiter(name="kalshi-exec", max_requests=10, window_seconds=1.0)
    poly_rl = RateLimiter(name="poly-exec", max_requests=5, window_seconds=1.0)

    kalshi_adapter = SimpleNamespace(rate_limiter=kalshi_rl)
    poly_adapter = SimpleNamespace(rate_limiter=poly_rl)

    async def _noop_get_all_prices():
        return {}

    price_store = SimpleNamespace(get_all_prices=_noop_get_all_prices)
    scanner = SimpleNamespace(
        current_opportunities=[], stats={}, history=[],
    )
    engine = SimpleNamespace(
        stats={"audit": {}},
        execution_history=[],
        manual_positions=[],
        incidents=[],
        equity_curve=[],
        adapters={"kalshi": kalshi_adapter, "polymarket": poly_adapter},
    )
    monitor = SimpleNamespace(current_balances={})

    api = ArbiterAPI(
        price_store=price_store,
        scanner=scanner,
        engine=engine,
        monitor=monitor,
        config=config,
        safety=None,
    )
    return api


def test_direct_health_endpoints_are_unambiguous_without_socket_binding():
    async def _run():
        api = await _make_rate_limit_api()
        health_response = await api.handle_liveness(None)
        ready_response = await api.handle_service_ready(None)

        health = json.loads(health_response.text)
        ready = json.loads(ready_response.text)

        assert health["status"] == "ok"
        assert health["probe"] == "liveness"
        assert ready["status"] == "ready"
        assert ready["probe"] == "service_readiness"
        assert ready["live_trading_endpoint"] == "/api/readiness"

    asyncio.run(_run())


def test_rate_limit_ws_event_shape():
    """SAFE-04: Within 3s of WS connect, a `rate_limit_state` message arrives
    with {platform: stats_dict} payload. Each stats_dict must carry the three
    dashboard-consumable fields: available_tokens, max_requests,
    remaining_penalty_seconds.
    """
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    async def _run():
        api = await _make_rate_limit_api()
        app = web.Application()
        app.router.add_get("/ws", api.handle_websocket)
        # Start the periodic rate-limit broadcaster task
        loop_task = asyncio.create_task(api._rate_limit_broadcast_loop())
        try:
            async with TestClient(TestServer(app)) as client:
                async with client.ws_connect("/ws") as ws:
                    # First message is `bootstrap`; drain it.
                    first = await ws.receive(timeout=3.0)
                    assert first.type == aiohttp.WSMsgType.TEXT
                    first_payload = json.loads(first.data)
                    assert first_payload["type"] == "bootstrap"

                    # Wait up to 4s for a rate_limit_state event (loop emits every 2s).
                    deadline = time.time() + 4.5
                    rate_msg = None
                    while time.time() < deadline:
                        msg = await ws.receive(timeout=4.5)
                        if msg.type != aiohttp.WSMsgType.TEXT:
                            continue
                        data = json.loads(msg.data)
                        if data.get("type") == "rate_limit_state":
                            rate_msg = data
                            break
                    assert rate_msg is not None, (
                        "Did not receive rate_limit_state event within 4.5s"
                    )
                    payload = rate_msg["payload"]
                    assert isinstance(payload, dict)
                    assert "kalshi" in payload
                    assert "polymarket" in payload
                    for platform, stats in payload.items():
                        assert isinstance(stats, dict), (
                            f"{platform} stats must be a dict, got {type(stats)}"
                        )
                        for key in (
                            "available_tokens",
                            "max_requests",
                            "remaining_penalty_seconds",
                        ):
                            assert key in stats, (
                                f"stats for {platform} missing '{key}'; got {stats}"
                            )
        finally:
            loop_task.cancel()
            try:
                await loop_task
            except (asyncio.CancelledError, BaseException):
                pass

    free_port()
    asyncio.run(_run())


def test_system_endpoint_includes_rate_limits():
    """SAFE-04: GET /api/system JSON body includes a top-level `rate_limits`
    key whose value is a dict keyed by adapter platform name.
    """
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    async def _run():
        api = await _make_rate_limit_api()
        app = web.Application()
        app.router.add_get("/api/system", api.handle_system)
        async with TestClient(TestServer(app)) as client:
            response = await client.get("/api/system")
            assert response.status == 200
            body = await response.json()
            assert "rate_limits" in body, (
                f"/api/system response missing 'rate_limits'; keys={list(body)}"
            )
            assert isinstance(body["rate_limits"], dict)
            assert "kalshi" in body["rate_limits"]
            assert "polymarket" in body["rate_limits"]

    free_port()
    asyncio.run(_run())


# ─── SAFE-06: resolution_criteria on /api/market-mappings + mapping_state WS ──


async def _make_mapping_api():
    """Build a minimal in-process ArbiterAPI for market-mapping endpoints.

    Uses the real MARKET_MAP dict so tests exercise the actual persistence
    path through update_market_mapping.
    """
    from types import SimpleNamespace

    from arbiter.api import ArbiterAPI
    from arbiter.config.settings import ArbiterConfig

    config = ArbiterConfig()

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
    )
    monitor = SimpleNamespace(current_balances={})

    api = ArbiterAPI(
        price_store=price_store,
        scanner=scanner,
        engine=engine,
        monitor=monitor,
        config=config,
        safety=None,
    )
    return api


def test_market_mappings_returns_resolution_criteria():
    """SAFE-06 truth: GET /api/market-mappings includes resolution_criteria
    and resolution_match_status keys on every mapping (even when unset).
    """
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    free_port()

    async def _run():
        api = await _make_mapping_api()
        app = web.Application()
        app.router.add_get("/api/market-mappings", api.handle_market_mappings)
        async with TestClient(TestServer(app)) as client:
            response = await client.get("/api/market-mappings")
            assert response.status == 200
            payload = await response.json()
            assert isinstance(payload, list)
            assert len(payload) >= 1
            for row in payload:
                assert "resolution_criteria" in row, (
                    f"mapping {row.get('canonical_id')} missing resolution_criteria"
                )
                assert "resolution_match_status" in row, (
                    f"mapping {row.get('canonical_id')} missing resolution_match_status"
                )

    asyncio.run(_run())


def test_market_mappings_prefers_live_mapping_store():
    """The operator surface must expose the live durable mapping set when available."""
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    free_port()

    async def _run():
        api = await _make_mapping_api()

        class StubMapping:
            def to_dict(self):
                return {
                    "canonical_id": "AUTO_LIVE_001",
                    "description": "Live discovered mapping",
                    "status": "candidate",
                    "allow_auto_trade": False,
                    "kalshi": "KX-LIVE-001",
                    "polymarket": "pm-live-001",
                    "resolution_criteria": None,
                    "resolution_match_status": "pending_operator_review",
                }

        class StubStore:
            async def all(self, status=None, limit=500):
                assert status is None
                assert limit >= 1
                return [StubMapping()]

        api.mapping_store = StubStore()

        app = web.Application()
        app.router.add_get("/api/market-mappings", api.handle_market_mappings)
        async with TestClient(TestServer(app)) as client:
            response = await client.get("/api/market-mappings?limit=25")
            assert response.status == 200
            payload = await response.json()
            assert payload == [{
                "canonical_id": "AUTO_LIVE_001",
                "description": "Live discovered mapping",
                "status": "candidate",
                "allow_auto_trade": False,
                "kalshi": "KX-LIVE-001",
                "polymarket": "pm-live-001",
                "resolution_criteria": None,
                "resolution_match_status": "pending_operator_review",
            }]

    asyncio.run(_run())


def test_market_mapping_update_accepts_criteria(monkeypatch):
    """SAFE-06 truth: POST /api/market-mappings/{id} accepts a
    resolution_criteria body and persists it, returning the stored payload.
    """
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    from arbiter import api as api_mod

    free_port()

    # Auth fixture — allow a single test operator.
    test_email = "test-op@arbiter.local"
    test_password_hash = api_mod._hash_password("letmein")
    monkeypatch.setattr(api_mod, "UI_ALLOWED_USERS", {test_email: test_password_hash})

    async def _run():
        api = await _make_mapping_api()
        app = web.Application()
        app.router.add_post(
            "/api/market-mappings/{canonical_id}", api.handle_market_mapping_action,
        )
        app.router.add_post("/api/auth/login", api.handle_login)

        async with TestClient(TestServer(app)) as client:
            # Login to get a session cookie.
            login_resp = await client.post(
                "/api/auth/login",
                json={"email": test_email, "password": "letmein"},
            )
            assert login_resp.status == 200

            criteria = {
                "kalshi": {"rule": "X"},
                "polymarket": {"rule": "Y"},
                "criteria_match": "similar",
                "operator_note": "verified manually",
            }
            resp = await client.post(
                "/api/market-mappings/DEM_HOUSE_2026",
                json={"action": "review", "resolution_criteria": criteria},
            )
            assert resp.status == 200, (await resp.text())
            body = await resp.json()
            assert body.get("resolution_criteria", {}).get("criteria_match") == "similar"
            assert body.get("resolution_match_status") == "similar"

    asyncio.run(_run())


def test_market_mapping_update_rejects_invalid_criteria_match(monkeypatch):
    """Threat T-3-06-B: criteria_match outside the allowed enum returns 400."""
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    from arbiter import api as api_mod

    free_port()

    test_email = "test-op@arbiter.local"
    test_password_hash = api_mod._hash_password("letmein")
    monkeypatch.setattr(api_mod, "UI_ALLOWED_USERS", {test_email: test_password_hash})

    async def _run():
        api = await _make_mapping_api()
        app = web.Application()
        app.router.add_post(
            "/api/market-mappings/{canonical_id}", api.handle_market_mapping_action,
        )
        app.router.add_post("/api/auth/login", api.handle_login)

        async with TestClient(TestServer(app)) as client:
            login_resp = await client.post(
                "/api/auth/login",
                json={"email": test_email, "password": "letmein"},
            )
            assert login_resp.status == 200

            resp = await client.post(
                "/api/market-mappings/DEM_HOUSE_2026",
                json={
                    "action": "review",
                    "resolution_criteria": {
                        "criteria_match": "DROP TABLE mappings",
                    },
                },
            )
            assert resp.status == 400

    asyncio.run(_run())


def test_enable_auto_trade_requires_confirmed_identical_mapping(monkeypatch):
    """Auto-trade can only be enabled after explicit confirm + SAFE-06 identical."""
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    from arbiter import api as api_mod
    from arbiter.config.settings import MARKET_MAP

    free_port()

    test_email = "test-op@arbiter.local"
    test_password_hash = api_mod._hash_password("letmein")
    monkeypatch.setattr(api_mod, "UI_ALLOWED_USERS", {test_email: test_password_hash})
    original = dict(MARKET_MAP["DEM_HOUSE_2026"])

    async def _run():
        api = await _make_mapping_api()
        app = web.Application()
        app.router.add_post(
            "/api/market-mappings/{canonical_id}", api.handle_market_mapping_action,
        )
        app.router.add_post("/api/auth/login", api.handle_login)

        async with TestClient(TestServer(app)) as client:
            login_resp = await client.post(
                "/api/auth/login",
                json={"email": test_email, "password": "letmein"},
            )
            assert login_resp.status == 200

            resp = await client.post(
                "/api/market-mappings/DEM_HOUSE_2026",
                json={"action": "enable_auto_trade"},
            )
            assert resp.status == 400
            body = await resp.json()
            assert "confirmed" in body["error"]

            confirm = await client.post(
                "/api/market-mappings/DEM_HOUSE_2026",
                json={"action": "confirm"},
            )
            assert confirm.status == 200

            still_blocked = await client.post(
                "/api/market-mappings/DEM_HOUSE_2026",
                json={"action": "enable_auto_trade"},
            )
            assert still_blocked.status == 400
            body = await still_blocked.json()
            assert "resolution_match_status=identical" in body["error"]

    try:
        asyncio.run(_run())
    finally:
        MARKET_MAP["DEM_HOUSE_2026"] = original


def test_mapping_state_ws_event_fires_on_update(monkeypatch):
    """SAFE-06 truth: WebSocket mapping_state event fires within 2s after a
    POST update to a mapping's resolution_criteria.
    """
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    from arbiter import api as api_mod

    free_port()

    test_email = "test-op@arbiter.local"
    test_password_hash = api_mod._hash_password("letmein")
    monkeypatch.setattr(api_mod, "UI_ALLOWED_USERS", {test_email: test_password_hash})

    async def _run():
        api = await _make_mapping_api()
        app = web.Application()
        app.router.add_get("/ws", api.handle_websocket)
        app.router.add_post(
            "/api/market-mappings/{canonical_id}", api.handle_market_mapping_action,
        )
        app.router.add_post("/api/auth/login", api.handle_login)

        async with TestClient(TestServer(app)) as client:
            # Login.
            login_resp = await client.post(
                "/api/auth/login",
                json={"email": test_email, "password": "letmein"},
            )
            assert login_resp.status == 200

            async with client.ws_connect("/ws") as ws:
                # Drain bootstrap.
                first = await ws.receive(timeout=3.0)
                assert first.type == aiohttp.WSMsgType.TEXT
                first_payload = json.loads(first.data)
                assert first_payload["type"] == "bootstrap"

                # Trigger a mapping update.
                update_resp = await client.post(
                    "/api/market-mappings/DEM_HOUSE_2026",
                    json={
                        "action": "review",
                        "resolution_criteria": {
                            "kalshi": {"rule": "A"},
                            "polymarket": {"rule": "B"},
                            "criteria_match": "divergent",
                            "operator_note": "ws test",
                        },
                    },
                )
                assert update_resp.status == 200

                # Poll for mapping_state within 2s.
                deadline = time.time() + 2.5
                got_event = None
                while time.time() < deadline:
                    try:
                        msg = await ws.receive(timeout=2.0)
                    except asyncio.TimeoutError:
                        break
                    if msg.type != aiohttp.WSMsgType.TEXT:
                        continue
                    data = json.loads(msg.data)
                    if data.get("type") == "mapping_state":
                        got_event = data
                        break
                assert got_event is not None, "mapping_state event not received"
                payload = got_event["payload"]
                assert payload["canonical_id"] == "DEM_HOUSE_2026"
                assert (
                    payload["resolution_criteria"]["criteria_match"] == "divergent"
                )
                assert payload["resolution_match_status"] == "divergent"

    asyncio.run(_run())
