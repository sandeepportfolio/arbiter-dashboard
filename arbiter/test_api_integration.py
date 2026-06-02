import asyncio
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from datetime import datetime, timezone

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
        assert "Arbiter" in public_html
        assert 'id="root"' in public_html
        assert "/api/discovery/status" in public_html
        assert "Operator desk" in public_html or "operator desk" in public_html

        with urllib.request.urlopen(f"http://127.0.0.1:{port}/ops", timeout=5) as response:
            ops_html = response.read().decode("utf-8")
        assert "Arbiter" in ops_html
        assert 'id="root"' in ops_html
        assert "__arbiterBatchDiscover" in ops_html

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

        # /api/logs — recent system events sourced from execution_incidents.
        # Must return the documented envelope regardless of whether the
        # store has rows; the dashboard polls this endpoint and a 404 broke
        # the alerts dropdown prior to this fix.
        logs_resp = get_json("/api/logs")
        assert isinstance(logs_resp, dict)
        assert "logs" in logs_resp
        assert isinstance(logs_resp["logs"], list)
        assert logs_resp["count"] == len(logs_resp["logs"])
        assert "source" in logs_resp

        # /api/alerts — aggregator feeding the ops console dropdown. Must
        # return the documented envelope even when the engine has no
        # incidents, trades, mappings, or safety events to draw from.
        alerts_resp = get_json("/api/alerts")
        assert isinstance(alerts_resp, dict)
        assert isinstance(alerts_resp.get("alerts"), list)
        assert "generated_at" in alerts_resp
        for item in alerts_resp["alerts"]:
            assert {"id", "sev", "kind", "title", "body", "ts"}.issubset(item.keys())
            assert item["sev"] in {"info", "warn", "err", "ok"}
            assert item["kind"] in {"incident", "trade", "mapping", "safety"}
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


def test_trades_endpoint_returns_complete_newest_first_expected_actual_ledger():
    async def _run():
        from types import SimpleNamespace

        api = await _make_rate_limit_api()

        def execution(arb_id, ts, status, expected, realized):
            opp = {
                "description": f"Market {arb_id}",
                "canonical_id": f"CAN-{arb_id}",
                "yes_price": 0.40,
                "no_price": 0.55,
                "suggested_qty": 10,
                "net_edge_cents": expected * 10,
                "max_profit_usd": expected,
            }
            return SimpleNamespace(
                to_dict=lambda: {
                    "arb_id": arb_id,
                    "opportunity": opp,
                    "leg_yes": {"status": "filled", "fill_qty": 10, "fill_price": 0.40},
                    "leg_no": {"status": "filled", "fill_qty": 10, "fill_price": 0.55},
                    "status": status,
                    "realized_pnl": realized,
                    "timestamp": ts,
                }
            )

        api.engine.execution_history = [
            execution("old-closed", 100, "closed", 1.25, 1.00),
            execution("new-pending", 300, "pending", 0.50, 0.00),
            execution("mid-failed", 200, "failed", 2.00, -0.25),
        ]

        class Req:
            query = {}

        response = await api.handle_trades(Req())
        payload = json.loads(response.text)

        assert [row["arb_id"] for row in payload] == ["new-pending", "mid-failed", "old-closed"]
        assert [row["status_group"] for row in payload] == ["open", "failed", "completed"]
        assert payload[0]["expected_profit"] == 0.5
        assert payload[1]["expected_vs_realized"] == -2.25
        assert payload[2]["expected_cost"] == 9.5

    asyncio.run(_run())


def test_balances_endpoint_surfaces_age_source_and_errors():
    """/api/balances must return age_seconds and per-platform errors so the
    UI can render staleness explicitly instead of letting operators trade
    against a silently-frozen balance.
    """
    async def _run():
        from types import SimpleNamespace
        from unittest.mock import AsyncMock

        from arbiter.monitor.balance import BalanceSnapshot

        api = await _make_rate_limit_api()
        now = time.time()
        snapshots = {
            "kalshi": BalanceSnapshot(
                platform="kalshi", balance=450.0, timestamp=now - 2.0,
                is_low=False, source="kalshi:portfolio",
            ),
            "polymarket": BalanceSnapshot(
                platform="polymarket", balance=120.0, timestamp=now - 1.0,
                is_low=False, source="polymarket-us:/account/balances",
            ),
        }
        last_errors = {
            "polymarket": {"message": "503 from /account/balances", "timestamp": now - 1.0},
        }
        api.monitor = SimpleNamespace(
            current_balances=snapshots,
            last_errors=last_errors,
            refresh_balances=AsyncMock(return_value=snapshots),
        )

        class Req:
            query = {}

        response = await api.handle_balances(Req())
        payload = json.loads(response.text)

        assert "platforms" in payload
        assert payload["platforms"]["kalshi"]["source"] == "kalshi:portfolio"
        assert payload["platforms"]["kalshi"]["age_seconds"] >= 0
        assert payload["platforms"]["polymarket"]["source"] == "polymarket-us:/account/balances"
        assert payload["platforms"]["polymarket"]["error"] == "503 from /account/balances"
        assert payload["errors"]["polymarket"]["message"] == "503 from /account/balances"
        # Legacy top-level shape preserved for older dashboards
        assert payload["kalshi"]["balance"] == 450.0
        assert payload["cache"]["force_refresh"] is False
        # No cache hit on first call (cache empty), so we hit refresh once.
        assert api.monitor.refresh_balances.await_count == 1

    asyncio.run(_run())


def test_balances_endpoint_uses_cache_under_ttl():
    """Repeat calls within the 5s window must NOT trigger another refresh —
    that's the whole point of the freshness cache.
    """
    async def _run():
        from types import SimpleNamespace
        from unittest.mock import AsyncMock

        from arbiter.monitor.balance import BalanceSnapshot

        api = await _make_rate_limit_api()
        snapshots = {
            "kalshi": BalanceSnapshot(
                platform="kalshi", balance=10.0,
                timestamp=time.time(), is_low=True, source="x",
            ),
        }
        api.monitor = SimpleNamespace(
            current_balances=snapshots,
            last_errors={},
            refresh_balances=AsyncMock(return_value=snapshots),
        )

        class Req:
            query = {}

        await api.handle_balances(Req())
        await api.handle_balances(Req())
        await api.handle_balances(Req())

        # First call refreshed; subsequent two used the cache.
        assert api.monitor.refresh_balances.await_count == 1

    asyncio.run(_run())


def test_balances_endpoint_force_refresh_bypasses_cache():
    """?force_refresh=1 must trigger a live re-fetch even when the cache is
    warm. This is the operator's escape hatch when they suspect a stale
    balance is wrong.
    """
    async def _run():
        from types import SimpleNamespace
        from unittest.mock import AsyncMock

        from arbiter.monitor.balance import BalanceSnapshot

        api = await _make_rate_limit_api()
        snapshots = {
            "kalshi": BalanceSnapshot(
                platform="kalshi", balance=10.0,
                timestamp=time.time(), is_low=True, source="x",
            ),
        }
        api.monitor = SimpleNamespace(
            current_balances=snapshots,
            last_errors={},
            refresh_balances=AsyncMock(return_value=snapshots),
        )

        class Req:
            def __init__(self, query):
                self.query = query

        # Warm the cache
        await api.handle_balances(Req({}))
        # Force a refresh
        await api.handle_balances(Req({"force_refresh": "1"}))

        assert api.monitor.refresh_balances.await_count == 2

    asyncio.run(_run())


def test_pnl_summary_excludes_deposits_when_ledger_has_no_movement():
    """A deposit alone (no trades) must NOT show up as profit/loss. Before
    this fix, the dashboard counted incoming deposits as P&L because the
    starting baseline lagged behind the deposit shift.
    """
    async def _run():
        from types import SimpleNamespace

        from arbiter.audit.pnl_reconciler import PnLReconciler

        api = await _make_rate_limit_api()
        reconciler = PnLReconciler(log_to_disk=False)
        reconciler.set_starting_balance("kalshi", 100.0)
        reconciler.set_starting_balance("polymarket", 200.0)
        # Operator deposits $500 to Kalshi. No trades happen.
        reconciler.record_deposit("kalshi", 500.0, balance_before=100.0, balance_after=600.0)

        api.reconciler = reconciler
        api.monitor.current_balances = {
            "kalshi": SimpleNamespace(balance=600.0),
            "polymarket": SimpleNamespace(balance=200.0),
        }

        response = await api.handle_pnl_summary(None)
        payload = json.loads(response.text)

        # The headline trading P&L must be ZERO — no trades have happened,
        # so the $500 deposit shouldn't show up as profit.
        assert payload["net_trading_pnl"] == 0.0
        assert payload["ledger_trading_pnl"] == 0.0
        assert payload["pnl_excludes_deposits"] is True
        assert payload["total_deposits"]["kalshi"] == 500.0
        # Balance method should also be zero — the starting baseline was
        # shifted forward by the deposit.
        assert payload["net_balance_change"] == 0.0
        # Original capital basis = pre-deposit baseline. With expected_settlement=0,
        # net_balance_change = (current+0) - adjusted_start = (800) - (600+200) = 0.

    asyncio.run(_run())


def test_pnl_summary_reports_ledger_pnl_after_trades():
    """When trades have executed, the headline P&L should come from the
    ledger (sum of recorded execution P&L) — not balance changes that can be
    polluted by deposits.
    """
    async def _run():
        from types import SimpleNamespace

        from arbiter.audit.pnl_reconciler import PnLReconciler

        api = await _make_rate_limit_api()
        reconciler = PnLReconciler(log_to_disk=False)
        reconciler.set_starting_balance("kalshi", 1000.0)
        reconciler.set_starting_balance("polymarket", 1000.0)
        # Two trades: +$15 win on kalshi, -$5 loss on polymarket. Net +$10.
        reconciler.record_execution_pnl("kalshi", 15.0)
        reconciler.record_execution_pnl("polymarket", -5.0)
        # Mid-way through, operator deposits $1000 more to kalshi.
        reconciler.record_deposit("kalshi", 1000.0, balance_before=1015.0, balance_after=2015.0)

        api.reconciler = reconciler
        # Balances reflect: kalshi=$2015, polymarket=$995. Total=$3010.
        # If P&L counted deposits it would be ~$1010. Real P&L = $10.
        api.monitor.current_balances = {
            "kalshi": SimpleNamespace(balance=2015.0),
            "polymarket": SimpleNamespace(balance=995.0),
        }

        response = await api.handle_pnl_summary(None)
        payload = json.loads(response.text)

        assert payload["ledger_trading_pnl"] == 10.0
        assert payload["net_trading_pnl"] == 10.0
        assert payload["pnl_method"] == "ledger"
        assert payload["total_deposits"]["kalshi"] == 1000.0
        # The naive "current - original_start" would be wildly wrong:
        #   (2015 - 1000) + (995 - 1000) = 1015 - 5 = 1010
        # Confirm we're not reporting that number.
        assert payload["net_trading_pnl"] != 1010.0

    asyncio.run(_run())


def test_pnl_summary_does_not_double_subtract_deposit_adjusted_baseline():
    async def _run():
        from types import SimpleNamespace

        from arbiter.audit.pnl_reconciler import PnLReconciler

        api = await _make_rate_limit_api()
        reconciler = PnLReconciler(log_to_disk=False)
        reconciler.set_starting_balance("kalshi", 100.0)
        reconciler.set_starting_balance("polymarket", 200.0)
        reconciler.record_deposit(
            "kalshi",
            50.0,
            balance_before=100.0,
            balance_after=150.0,
        )

        api.reconciler = reconciler
        api.monitor.current_balances = {
            "kalshi": SimpleNamespace(balance=160.0),
            "polymarket": SimpleNamespace(balance=200.0),
        }

        response = await api.handle_pnl_summary(None)
        payload = json.loads(response.text)

        assert payload["adjusted_starting_balances"]["kalshi"] == 150.0
        assert payload["original_starting_balances"]["kalshi"] == 100.0
        assert payload["total_deposits"]["kalshi"] == 50.0
        assert payload["balance_change_by_platform"]["kalshi"] == 10.0
        assert payload["net_cash_change"] == 10.0
        assert payload["net_balance_change"] == 10.0

    asyncio.run(_run())


def test_pnl_summary_keeps_disabled_platforms_in_all_platform_basis():
    async def _run():
        from types import SimpleNamespace

        from arbiter.audit.pnl_reconciler import PnLReconciler

        api = await _make_rate_limit_api()
        reconciler = PnLReconciler(log_to_disk=False)
        reconciler.set_starting_balance("kalshi", 100.0)
        reconciler.set_starting_balance("forecastex", 300.0)
        reconciler.record_deposit(
            "forecastex",
            300.0,
            balance_before=0.0,
            balance_after=300.0,
        )
        reconciler.record_execution_pnl("kalshi", 12.0)

        api.reconciler = reconciler
        api.monitor.current_balances = {
            "kalshi": SimpleNamespace(balance=112.0),
        }

        response = await api.handle_pnl_summary(None)
        payload = json.loads(response.text)

        assert payload["net_trading_pnl"] == 12.0
        assert payload["current_balances"]["forecastex"] == 600.0
        assert payload["estimated_balance_platforms"]["forecastex"] == 600.0
        assert payload["total_deposits_all_platforms"] == 300.0
        assert payload["capital_basis"] == 700.0
        assert "forecastex" in payload["all_pnl_platforms"]

    asyncio.run(_run())


def test_reconciliation_snapshot_reports_drift_when_latest_report_has_flags():
    """The reconciliation API summary must not say healthy when flags exist."""
    from types import SimpleNamespace

    async def _run():
        api = await _make_rate_limit_api()
        api.reconciler = SimpleNamespace(
            stats={
                "reconciliation_count": 3,
                "flag_count": 2,
                "starting_balances": {},
                "recorded_pnl": {},
                "latest_report": {
                    "entries": [],
                    "has_flags": True,
                    "total_discrepancy": -180.41,
                },
            }
        )

        payload = api._reconciliation_snapshot()

        assert payload["configured"] is True
        assert payload["summary"] == "PnL reconciliation drift is flagged"
        assert payload["latest_report"]["has_flags"] is True

    asyncio.run(_run())


def test_discovery_status_endpoint_defaults_to_idle():
    async def _run():
        api = await _make_rate_limit_api()
        response = await api.handle_discovery_status(None)
        payload = json.loads(response.text)

        assert payload["status"] == "idle"
        assert payload["phase"] == "idle"
        assert isinstance(payload["events"], list)
        assert isinstance(payload["counts"], dict)

    asyncio.run(_run())


def test_ops_refetch_modal_uses_live_discovery_api():
    html = open(os.path.join(os.getcwd(), "arbiter", "web", "ops.html"), encoding="utf-8").read()

    assert "/api/discovery/status" in html
    assert "/api/batch-discover" in html
    assert "function authHeaders" in html
    assert "window.__arbiterAuthMe" in html
    assert "sessionStorage.setItem(AUTH_TOKEN_KEY" in html
    assert "authFetch('/api/batch-discover'" in html
    assert "/api/mappings?status=confirmed&limit=5000" in html
    assert "/api/mappings?status=candidate&limit=5000" in html
    assert "The `phases` array below is a scripted simulation" not in html


def test_ops_mapping_validation_modal_uses_live_backend():
    html = open(os.path.join(os.getcwd(), "arbiter", "web", "ops.html"), encoding="utf-8").read()

    assert "/api/market-mappings/' + encodeURIComponent(id) + '/validation" in html
    assert "/api/market-mappings/' + encodeURIComponent(id) + '/validate" in html
    assert "window.__arbiterMappingValidationHistory" in html
    assert "window.__arbiterRunMappingValidation" in html
    assert "Claude Opus 4.7" in html
    assert "Run validation" in html
    assert "arbiter agents validate --model claude-opus-4-7" in html
    assert "Apply confirm" not in html
    assert "Apply reject" not in html
    assert "Mapping ${v.verdict" not in html
    assert "buildScript(candidate)" not in html
    assert "model=claude-sonnet-4" not in html
    assert "Will the GOP nominate Donald Trump" not in html


def test_mobile_mappings_render_api_field_names():
    html = open(os.path.join(os.getcwd(), "arbiter", "web", "ops.html"), encoding="utf-8").read()

    assert "function MobMappings()" in html
    assert "r.kalshi_market_id || r.kalshi_ticker || r.kalshi || r.ticker" in html
    assert "r.polymarket_slug || r.poly_slug || r.polymarket" in html
    assert "function MapLine" in html
    assert "setModal({ kind:'agentValidate', payload: c })" in html
    assert "Tap any mapping to inspect validation history" in html


def test_ops_execution_ledger_preserves_context_and_filters_realized_losses():
    html = open(os.path.join(os.getcwd(), "arbiter", "web", "ops.html"), encoding="utf-8").read()

    assert "const normalizedOpp = Object.assign({}, opp" in html
    assert "net_edge_cents: edgeCents" in html
    assert "persistence_scans: asNumber(opp.persistence_scans" in html
    assert "if (statusFilter === 'losses') return !isOpenTrade(e) && Number(e.realized_pnl || 0) < 0;" in html


def test_ops_charts_and_markets_use_live_data_sources():
    html = open(os.path.join(os.getcwd(), "arbiter", "web", "ops.html"), encoding="utf-8").read()

    assert "window.location.protocol === 'file:'" in html
    assert "window.__arbiterAPI = API" in html
    assert "const RANGE_SECONDS" in html
    assert "window.filterTimeSeries" in html
    assert "window.filterRowsByRange" in html
    assert "window.seriesDelta" in html
    assert "TimeRange value={range} onChange={setRange}" in html
    assert "marketRowsFromMappings(M, 'confirmed')" in html
    assert "rows={visibleRows}" in html
    assert "rows={M.opportunities}" not in html
    assert "The scanner is monitoring 312 mapped markets" not in html
    assert "Includes $100 deposit at 14:32" not in html
    assert "Scans (24h)\" value=\"18,420\"" not in html
    assert "Annualized" not in html
    assert "totalPnl/1000" not in html
    assert "const startingCapital = window.capitalBasis ? window.capitalBasis(M)" in html
    assert "Array.isArray(readiness.checks)" in html
    assert "c && c.status === 'pass'" in html
    assert "7/7 readiness gates" not in html
    assert "these 7 health checks" not in html


def test_ops_scanner_balance_and_trades_widgets_use_live_telemetry():
    html = open(os.path.join(os.getcwd(), "arbiter", "web", "ops.html"), encoding="utf-8").read()

    assert "M.health.scanner.active_opportunities" in html
    assert "M.health.scanner.tradable_opportunities" in html
    assert "M.health.audit.recent_results" in html
    assert "function edgeTelemetrySamples" in html
    assert "function bestTelemetryEdge" in html
    assert "M.edgeBucketSampleCount" in html
    assert "Audited edge samples" in html
    assert "Telemetry edge samples" in html
    assert "function BalanceLegendItem" in html
    assert "Rows show live balance details, source share, and deposit-neutral trading P&L" in html
    assert "Tap a row" not in html
    assert "Edge samples" in html
    assert "Scanner & edge quality" in html
    assert 'className="scanner-edge-layout"' in html
    assert "function ScannerMetric" in html
    assert "Scanner telemetry" in html
    assert "Expected profit" in html
    assert "expected_vs_realized" in html
    assert "status_group" in html
    assert "M.executions.slice(0, 8)" not in html
    assert 'Avg edge captured" value="2.4' not in html


def test_signed_auth_token_survives_worker_restart_state():
    """A valid signed UI token should not 401 after in-memory sessions reset."""
    from types import SimpleNamespace

    import arbiter.api as api

    token = api._generate_token("sparx.sandeep@gmail.com")
    api._ACTIVE_SESSIONS.clear()
    api._REVOKED_SESSIONS.clear()

    request = SimpleNamespace(headers={"Authorization": f"Bearer {token}"}, cookies={})
    user = asyncio.run(api.get_current_user(request))

    assert user == "sparx.sandeep@gmail.com"
    assert api._ACTIVE_SESSIONS[token] == "sparx.sandeep@gmail.com"


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


def test_system_endpoint_includes_mappings_summary():
    """/api/system surfaces a `mappings` block so operators can see
    status counts + discovery state without a second round-trip to
    /api/mappings/metrics + /api/discovery/status.
    """
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    async def _run():
        api = await _make_rate_limit_api()

        # Wire a fake mapping_store with a count_by_status() method.
        class _FakeStore:
            async def count_by_status(self):
                return {
                    "confirmed": 72, "candidate": 113, "review": 76,
                    "expired": 36977, "rejected": 111,
                }

        api.mapping_store = _FakeStore()
        api._pm_us_metrics = {
            "auto_discovery_candidates_pending": 189,
            "auto_discovery_last_written": 7,
        }

        app = web.Application()
        app.router.add_get("/api/system", api.handle_system)
        async with TestClient(TestServer(app)) as client:
            response = await client.get("/api/system")
            assert response.status == 200
            body = await response.json()
            assert "mappings" in body, (
                f"/api/system missing 'mappings'; keys={list(body)}"
            )
            mappings = body["mappings"]
            assert mappings["status_counts"]["confirmed"] == 72
            assert mappings["status_counts"]["candidate"] == 113
            assert mappings["status_counts"]["review"] == 76
            assert mappings["status_counts"]["expired"] == 36977
            assert mappings["active_total"] == 72 + 113 + 76
            assert mappings["expired_total"] == 36977
            assert mappings["rejected_total"] == 111
            disc = mappings["discovery"]
            assert "phase" in disc and "status" in disc
            assert disc["candidates_pending"] == 189
            assert disc["last_written"] == 7

    free_port()
    asyncio.run(_run())


def test_system_endpoint_mapping_summary_falls_back_to_runtime_cache():
    """When mapping_store is None (or count_by_status missing), the snapshot
    falls back to counting MARKET_MAP entries — never blank."""
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer
    from arbiter.mapping.market_map import MARKET_MAP

    async def _run():
        api = await _make_rate_limit_api()
        api.mapping_store = None
        # Seed two confirmed + one candidate in runtime cache (snapshot copy).
        original = dict(MARKET_MAP)
        MARKET_MAP.clear()
        MARKET_MAP.update({
            "X-1": {"status": "confirmed"},
            "X-2": {"status": "confirmed"},
            "X-3": {"status": "candidate"},
        })
        try:
            app = web.Application()
            app.router.add_get("/api/system", api.handle_system)
            async with TestClient(TestServer(app)) as client:
                response = await client.get("/api/system")
                assert response.status == 200
                body = await response.json()
                m = body["mappings"]
                assert m["status_counts"] == {"confirmed": 2, "candidate": 1}
                assert m["active_total"] == 3
                assert m["expired_total"] == 0
        finally:
            MARKET_MAP.clear()
            MARKET_MAP.update(original)

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


def test_market_mappings_metrics_uses_store_when_available():
    """GET /api/mappings/metrics returns the live aggregate payload from the
    durable store when one is wired in. The dashboard reads these fields
    directly, so the contract here must match what PageMappings expects."""
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    free_port()

    expected_payload = {
        "total": 1024,
        "active_total": 256,
        "status_counts": {
            "confirmed": 200,
            "candidate": 40,
            "review": 16,
            "rejected": 8,
            "expired": 760,
        },
        "review_backlog": 56,
        "auto_trade": {"enabled": 180, "eligible": 200, "rate": 0.9},
        "confirmation_rate": 0.806,
        "quality": {
            "active_total": 256,
            "avg_score": 0.81,
            "avg_confidence": 0.79,
            "buckets": [
                {"label": "Excellent", "range": "0.90 – 1.00", "count": 100},
                {"label": "Good",      "range": "0.70 – 0.90", "count": 96},
                {"label": "Moderate",  "range": "0.50 – 0.70", "count": 50},
                {"label": "Low",       "range": "< 0.50",      "count": 10},
            ],
        },
        "criteria_match": {"identical": 180, "similar": 20, "pending_operator_review": 56},
        "categories": [
            {"name": "sports", "count": 150, "confirmed": 120, "pending": 30},
        ],
        "platform_coverage": {"kalshi_markets": 220, "polymarket_markets": 198},
        "trade_accuracy": {
            "mappings_traded": 25,
            "total_arbs": 80,
            "live_arbs": 12,
            "profitable": 60,
            "losing": 12,
            "decided_arbs": 72,
            "profitable_rate": 0.8333333333333334,
            "realized_pnl": 142.5,
        },
    }

    async def _run():
        api = await _make_mapping_api()

        class StubStore:
            def __init__(self):
                self.calls = 0

            async def compute_metrics(self):
                self.calls += 1
                return expected_payload

        store = StubStore()
        api.mapping_store = store

        app = web.Application()
        app.router.add_get("/api/mappings/metrics", api.handle_market_mappings_metrics)
        async with TestClient(TestServer(app)) as client:
            response = await client.get("/api/mappings/metrics")
            assert response.status == 200
            payload = await response.json()
            assert store.calls == 1
            for key, value in expected_payload.items():
                assert payload[key] == value, f"mismatch on {key}: {payload[key]} != {value}"
            assert "generated_at" in payload

    asyncio.run(_run())


def test_market_mappings_metrics_falls_back_when_store_fails():
    """When the durable store is unreachable, the metrics endpoint still
    returns a non-empty payload computed from the in-process MARKET_MAP so
    the dashboard never renders blank cards."""
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    free_port()

    async def _run():
        api = await _make_mapping_api()

        class BrokenStore:
            async def compute_metrics(self):
                raise RuntimeError("simulated DB outage")

        api.mapping_store = BrokenStore()

        app = web.Application()
        app.router.add_get("/api/mappings/metrics", api.handle_market_mappings_metrics)
        async with TestClient(TestServer(app)) as client:
            response = await client.get("/api/mappings/metrics")
            assert response.status == 200
            payload = await response.json()
            # Shape must match what the frontend expects, even on fallback.
            for key in (
                "status_counts", "active_total", "review_backlog", "auto_trade",
                "confirmation_rate", "quality", "criteria_match", "categories",
                "platform_coverage", "trade_accuracy", "generated_at",
            ):
                assert key in payload, f"fallback payload missing {key}"
            assert isinstance(payload["status_counts"], dict)
            assert isinstance(payload["quality"]["buckets"], list)
            assert len(payload["quality"]["buckets"]) == 4
            assert isinstance(payload["categories"], list)
            # trade_accuracy is intentionally None on the runtime fallback —
            # the in-process MARKET_MAP has no DB join available.
            assert payload["trade_accuracy"] is None

    asyncio.run(_run())


def test_half_recorded_recovery_endpoint_groups_manual_blockers():
    """Half-recorded filled legs must be visible as exact manual blockers.

    The endpoint is intentionally read-only: it may identify the exposure, but
    it must not imply auto-trade can resume or that recovery closed the rows.
    """
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    free_port()

    async def _run():
        api = await _make_mapping_api()

        class StubExecutionStore:
            async def list_half_recorded_arbs(self):
                return [
                    {
                        "arb_id": "ARB-ONE",
                        "canonical_id": "CAN-EXPOSURE",
                        "status": "pending",
                        "created_at": datetime(2026, 5, 15, 12, 0, tzinfo=timezone.utc),
                        "leg_count": 1,
                        "filled_leg_count": 1,
                        "filled_notional": 8.75,
                        "leg_order_ids": ["K-ORDER-1"],
                    },
                    {
                        "arb_id": "ARB-TWO",
                        "canonical_id": "CAN-EXPOSURE",
                        "status": "recovering",
                        "created_at": datetime(2026, 5, 15, 12, 5, tzinfo=timezone.utc),
                        "leg_count": 1,
                        "filled_leg_count": 1,
                        "filled_notional": 1.25,
                        "leg_order_ids": ["K-ORDER-2"],
                    },
                ]

        api.execution_store = StubExecutionStore()

        app = web.Application()
        app.router.add_get("/api/recovery/half-recorded", api.handle_half_recorded_recovery)
        async with TestClient(TestServer(app)) as client:
            response = await client.get("/api/recovery/half-recorded")
            assert response.status == 200
            payload = await response.json()

        assert payload["available"] is True
        assert payload["status"] == "blocked"
        assert payload["allow_auto_trade"] is False
        assert payload["action_required"] == "manual_venue_reconciliation_required"
        assert payload["count"] == 2
        assert payload["filled_leg_count"] == 2
        assert payload["total_filled_notional"] == pytest.approx(10.0)
        assert payload["by_canonical"] == [
            {
                "canonical_id": "CAN-EXPOSURE",
                "count": 2,
                "filled_leg_count": 2,
                "filled_notional": 10.0,
                "statuses": {"pending": 1, "recovering": 1},
                "arb_ids": ["ARB-ONE", "ARB-TWO"],
            }
        ]
        assert payload["arbs"][0]["created_at"] == "2026-05-15T12:00:00+00:00"
        assert all(row["allow_auto_trade"] is False for row in payload["arbs"])
        assert all(
            row["action_required"] == "manual_venue_reconciliation_required"
            for row in payload["arbs"]
        )

    asyncio.run(_run())


def test_market_mapping_validation_history_and_run_are_read_only(monkeypatch):
    """Row-click validation must show real replayable events without changing status."""
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    from arbiter import api as api_mod
    from arbiter.config.settings import MARKET_MAP
    from arbiter.mapping import llm_verifier

    free_port()

    test_email = "test-op@arbiter.local"
    test_password_hash = api_mod._hash_password("letmein")
    monkeypatch.setattr(api_mod, "UI_ALLOWED_USERS", {test_email: test_password_hash})
    monkeypatch.setenv("LLM_VERIFIER_BACKEND", "cli")

    async def fake_verify(kalshi_question, poly_question):
        assert "House" in kalshi_question or "Democratic" in kalshi_question
        assert "House" in poly_question or "Democratic" in poly_question
        return "YES"

    monkeypatch.setattr(llm_verifier, "verify", fake_verify)
    monkeypatch.setattr(llm_verifier, "_BACKEND", "cli")

    original = dict(MARKET_MAP["DEM_HOUSE_2026"])

    async def _run():
        api = await _make_mapping_api()
        api._record_discovery_event({
            "phase": "llm_batch",
            "message": "Validated DEM_HOUSE_2026 in batch cache",
            "counts": {"verdicts": 1},
        })
        app = web.Application()
        app.router.add_get(
            "/api/market-mappings/{canonical_id}/validation",
            api.handle_market_mapping_validation,
        )
        app.router.add_post(
            "/api/market-mappings/{canonical_id}/validate",
            api.handle_market_mapping_validate,
        )
        app.router.add_post("/api/auth/login", api.handle_login)

        async with TestClient(TestServer(app)) as client:
            unauth_history = await client.get(
                "/api/market-mappings/DEM_HOUSE_2026/validation"
            )
            assert unauth_history.status == 401
            unauth_run = await client.post(
                "/api/market-mappings/DEM_HOUSE_2026/validate"
            )
            assert unauth_run.status == 401

            login_resp = await client.post(
                "/api/auth/login",
                json={"email": test_email, "password": "letmein"},
            )
            assert login_resp.status == 200

            history_resp = await client.get(
                "/api/market-mappings/DEM_HOUSE_2026/validation"
            )
            assert history_resp.status == 200, (await history_resp.text())
            history = await history_resp.json()
            assert history["mapping"]["canonical_id"] == "DEM_HOUSE_2026"
            assert history["verifier"]["model"] == "claude-opus-4-7"
            assert any(e["phase"] == "llm_batch" for e in history["discovery_events"])

            run_resp = await client.post(
                "/api/market-mappings/DEM_HOUSE_2026/validate"
            )
            assert run_resp.status == 200, (await run_resp.text())
            run = await run_resp.json()
            assert run["read_only"] is True
            assert run["verifier"]["backend"] == "cli"
            assert any(e["kind"] == "tool_call" and e["tool"] == "llm_verifier.verify" for e in run["events"])
            assert any(e["kind"] == "verdict" for e in run["events"])
            assert MARKET_MAP["DEM_HOUSE_2026"]["status"] == original["status"]

            updated_history = await (
                await client.get("/api/market-mappings/DEM_HOUSE_2026/validation")
            ).json()
            assert len(updated_history["validation_runs"]) == 1
            assert updated_history["validation_runs"][0]["events"][-1]["kind"] == "verdict"

    try:
        asyncio.run(_run())
    finally:
        MARKET_MAP["DEM_HOUSE_2026"] = original


def test_mapping_validation_checks_polymarket_us_client():
    """Polymarket US validation should use the current client, not legacy Gamma only."""
    from types import SimpleNamespace

    async def _run():
        api = await _make_mapping_api()

        class FakePolyClient:
            async def get_market_by_slug(self, slug):
                if slug == "pm-live-slug":
                    return {
                        "market": {
                            "slug": slug,
                            "question": "Will the live event happen?",
                            "state": "open",
                            "active": True,
                            "closed": False,
                            "endDate": "2026-06-01T00:00:00Z",
                        }
                    }
                if slug == "pm-closed-slug":
                    return {
                        "market": {
                            "slug": slug,
                            "question": "Did the closed event happen?",
                            "state": "closed",
                            "active": False,
                            "closed": True,
                            "endDate": "2026-01-01T00:00:00Z",
                        }
                    }
                raise AssertionError(slug)

        api.collectors = {
            "polymarket": SimpleNamespace(client=FakePolyClient()),
        }
        result = await api._validate_live_market_presence({
            "polymarket": "pm-live-slug",
        })
        assert result["polymarket"]["checked"] is True
        assert result["polymarket"]["exists"] is True
        assert result["polymarket"]["active"] is True
        assert result["polymarket"]["question"] == "Will the live event happen?"

        closed = await api._validate_live_market_presence({
            "polymarket": "pm-closed-slug",
        })
        assert closed["polymarket"]["checked"] is True
        assert closed["polymarket"]["exists"] is True
        assert closed["polymarket"]["active"] is False
        assert closed["polymarket"]["closed"] is True

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


def test_market_mapping_reject_action_is_supported(monkeypatch):
    """The operator UI's Reject button must map to a real API action."""
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
                json={"action": "reject", "note": "not equivalent"},
            )
            assert resp.status == 200, (await resp.text())
            body = await resp.json()
            assert body["status"] == "rejected"
            assert body["allow_auto_trade"] is False

    try:
        asyncio.run(_run())
    finally:
        MARKET_MAP["DEM_HOUSE_2026"] = original


def test_market_mapping_expire_action_is_supported(monkeypatch):
    """Stale or resolved mappings must be expirable through the live API path."""
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
                json={"action": "expire", "note": "settlement date is in the past"},
            )
            assert resp.status == 200, (await resp.text())
            body = await resp.json()
            assert body["status"] == "expired"
            assert body["allow_auto_trade"] is False

    try:
        asyncio.run(_run())
    finally:
        MARKET_MAP["DEM_HOUSE_2026"] = original


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


def _mint_session_token():
    """Mint a fresh operator-session token for require_auth-protected tests."""
    import arbiter.api as api_mod

    token = api_mod._generate_token("sparx.sandeep@gmail.com")
    api_mod._ACTIVE_SESSIONS[token] = "sparx.sandeep@gmail.com"
    return token


def test_set_forecastex_conid_writes_through_to_mapping_store_and_reactivates_collector():
    """POST /api/market-mappings/{cid}/forecastex_conid attaches the
    operator-supplied tradeable conid, calls mapping_store.upsert, and
    pokes the FX collector to drop any in-memory inactive state on the
    old AND new conids so the next poll cycle re-probes cleanly.
    """
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, MagicMock
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    free_port()

    async def _run():
        api = await _make_mapping_api()

        stored = SimpleNamespace(
            canonical_id="DEM_HOUSE_2026",
            forecastex_contract_id="733131966",  # parent
        )
        store = MagicMock()
        store.get = AsyncMock(return_value=stored)
        store.upsert = AsyncMock()
        api.mapping_store = store

        fx = MagicMock()
        fx.reactivate_conid = MagicMock()
        api.collectors = {"forecastex": fx}

        app = web.Application()
        app.router.add_post(
            "/api/market-mappings/{canonical_id}/forecastex_conid",
            api.handle_set_forecastex_conid,
        )
        async with TestClient(TestServer(app)) as client:
            token = _mint_session_token()
            response = await client.post(
                "/api/market-mappings/DEM_HOUSE_2026/forecastex_conid",
                json={"conid": "888888"},
                headers={"Authorization": f"Bearer {token}"},
            )
            assert response.status == 200, await response.text()
            payload = await response.json()
            assert payload["prior_conid"] == "733131966"
            assert payload["new_conid"] == "888888"
            assert store.upsert.await_count == 1
            assert stored.forecastex_contract_id == "888888"
            args = {c.args[0] for c in fx.reactivate_conid.call_args_list}
            assert "733131966" in args
            assert "888888" in args

    asyncio.run(_run())


def test_set_forecastex_conid_returns_404_for_unknown_mapping():
    from unittest.mock import AsyncMock, MagicMock
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    free_port()

    async def _run():
        api = await _make_mapping_api()
        store = MagicMock()
        store.get = AsyncMock(return_value=None)
        store.upsert = AsyncMock()
        api.mapping_store = store
        app = web.Application()
        app.router.add_post(
            "/api/market-mappings/{canonical_id}/forecastex_conid",
            api.handle_set_forecastex_conid,
        )
        async with TestClient(TestServer(app)) as client:
            token = _mint_session_token()
            response = await client.post(
                "/api/market-mappings/NONEXISTENT/forecastex_conid",
                json={"conid": "888"},
                headers={"Authorization": f"Bearer {token}"},
            )
            assert response.status == 404
            assert store.upsert.await_count == 0

    asyncio.run(_run())
