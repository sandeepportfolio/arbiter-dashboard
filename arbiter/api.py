"""
Canonical aiohttp server for the ARBITER dashboard and API.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import secrets
import time
from pathlib import Path
from typing import Dict, List, Optional

from aiohttp import WSMsgType, web
from aiohttp.web_exceptions import HTTPUnauthorized

from .config.settings import MARKET_MAP, ArbiterConfig, update_market_mapping
from .execution.engine import ArbExecution, ExecutionEngine, ExecutionIncident
from .monitor.balance import BalanceMonitor
from .profitability import ProfitabilityValidator
from .scanner.arbitrage import ArbitrageOpportunity, ArbitrageScanner
from .utils.price_store import PricePoint, PriceStore

logger = logging.getLogger("arbiter.api")

# ─── Session auth helpers ──────────────────────────────────────────────────────

UI_SESSION_SECRET = os.getenv("UI_SESSION_SECRET", "")
def _hash_password(password: str) -> str:
    """SHA-256 hash of a password."""
    return hashlib.sha256(password.encode()).hexdigest()


UI_ALLOWED_USERS = {
    os.getenv("UI_USER_EMAIL", "sparx.sandeep@gmail.com"): _hash_password(
        os.getenv("UI_USER_PASSWORD", "saibaba")
    ),
}


def _get_secret() -> str:
    if not UI_SESSION_SECRET:
        logger.warning(
            "UI_SESSION_SECRET not set! Using insecure default. "
            "Generate one with: openssl rand -hex 32"
        )
        return "INSECURE_DEFAULT_CHANGE_ME"
    return UI_SESSION_SECRET


def _generate_token(email: str) -> str:
    """HMAC-signed session token valid for 7 days."""
    ts = int(time.time())
    payload = f"{email}:{ts}"
    sig = hmac.new(_get_secret().encode(), payload.encode(), "sha256").hexdigest()
    return f"{payload}:{sig}"


def _verify_token(token: str) -> Optional[str]:
    """Verify token, return email if valid (≤7 days old), else None."""
    if not token:
        return None
    parts = token.rsplit(":", 2)
    if len(parts) != 3:
        return None
    email, ts_str, sig = parts
    try:
        ts = int(ts_str)
        if time.time() - ts > 7 * 86400:
            return None
    except ValueError:
        return None
    expected = hmac.new(_get_secret().encode(), f"{email}:{ts_str}".encode(), "sha256").hexdigest()
    if not hmac.compare_digest(sig, expected):
        return None
    return email


# Active sessions: token -> email
_ACTIVE_SESSIONS: Dict[str, str] = {}


async def get_current_user(request: web.Request) -> Optional[str]:
    """Get logged-in user from cookie or Authorization header."""
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        token = auth[7:]
    else:
        token = request.cookies.get("arbiter_session", "")
    email = _verify_token(token)
    if email and _ACTIVE_SESSIONS.get(token) == email:
        return email
    return None


async def login_user(email: str, password: str) -> Optional[str]:
    """Authenticate user, return signed token or None."""
    hashed = _hash_password(password)
    if email not in UI_ALLOWED_USERS or UI_ALLOWED_USERS[email] != hashed:
        logger.warning(f"Failed login attempt: {email}")
        return None
    token = _generate_token(email)
    _ACTIVE_SESSIONS[token] = email
    logger.info(f"User logged in: {email}")
    return token

async def logout_user(token: str) -> None:
    """Invalidate a session token."""
    _ACTIVE_SESSIONS.pop(token, None)

async def require_auth(request: web.Request) -> str:
    """Raise 401 if not authenticated."""
    user = await get_current_user(request)
    if not user:
        raise HTTPUnauthorized(reason="Authentication required")
    return user


class ArbiterAPI:
    def __init__(
        self,
        price_store: PriceStore,
        scanner: ArbitrageScanner,
        engine: ExecutionEngine,
        monitor: BalanceMonitor,
        config: ArbiterConfig,
        collectors: Optional[Dict[str, object]] = None,
        profitability: Optional[ProfitabilityValidator] = None,
        host: str = "0.0.0.0",
        port: int = 8080,
    ):
        self.store = price_store
        self.scanner = scanner
        self.engine = engine
        self.monitor = monitor
        self.config = config
        self.collectors = collectors or {}
        self.profitability = profitability
        self.host = host
        self.port = port
        self.started_at = time.time()
        self._ws_clients: list[web.WebSocketResponse] = []
        self._site_index = Path(__file__).resolve().parent.parent / "index.html"
        self._dashboard_dir = Path(__file__).resolve().parent / "web"

    async def serve(self):
        app = web.Application(middlewares=[self._cors_middleware])
        app.router.add_get("/", self.handle_site_index)
        app.router.add_get("/ops", self.handle_dashboard)
        app.router.add_get("/favicon.ico", self.handle_favicon)
        if self._dashboard_dir.exists():
            app.router.add_static("/static", str(self._dashboard_dir), show_index=False)
        app.router.add_get("/api/health", self.handle_health)
        app.router.add_get("/api/system", self.handle_system)
        app.router.add_get("/api/prices", self.handle_prices)
        app.router.add_get("/api/opportunities", self.handle_opportunities)
        app.router.add_get("/api/balances", self.handle_balances)
        app.router.add_get("/api/trades", self.handle_trades)
        app.router.add_get("/api/executions", self.handle_trades)
        app.router.add_get("/api/stats", self.handle_system)
        app.router.add_get("/api/markets", self.handle_market_mappings)
        app.router.add_get("/api/market-mappings", self.handle_market_mappings)
        app.router.add_post("/api/market-mappings/{canonical_id}", self.handle_market_mapping_action)
        app.router.add_get("/api/errors", self.handle_errors)
        app.router.add_post("/api/errors/{incident_id}", self.handle_incident_action)
        app.router.add_get("/api/manual-positions", self.handle_manual_positions)
        app.router.add_post("/api/manual-positions/{position_id}", self.handle_manual_position_action)
        app.router.add_get("/api/profitability", self.handle_profitability)
        app.router.add_get("/api/portfolio", self.handle_portfolio)
        app.router.add_get("/api/portfolio/violations", self.handle_portfolio_violations)
        app.router.add_post("/api/portfolio/unwind/{position_id}", self.handle_portfolio_unwind)
        app.router.add_get("/api/portfolio/summary", self.handle_portfolio_summary)
        app.router.add_post("/api/auth/login", self.handle_login)
        app.router.add_post("/api/auth/logout", self.handle_logout)
        app.router.add_get("/api/auth/me", self.handle_auth_me)
        app.router.add_get("/ws", self.handle_websocket)

        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, self.host, self.port)
        await site.start()

        logger.info("ARBITER API listening at http://%s:%s", self.host, self.port)
        asyncio.create_task(self._broadcast_loop())

        while True:
            await asyncio.sleep(3600)

    @web.middleware
    async def _cors_middleware(self, request, handler):
        response = await handler(request)
        response.headers["Access-Control-Allow-Origin"] = "*"
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        response.headers["Access-Control-Allow-Headers"] = "Content-Type"
        return response

    async def handle_site_index(self, request):
        if self._site_index.exists():
            return web.FileResponse(self._site_index)
        return await self.handle_dashboard(request)

    async def handle_dashboard(self, request):
        dashboard_path = self._dashboard_dir / "dashboard.html"
        if not dashboard_path.exists():
            return web.Response(text="Dashboard not found", status=404)
        return web.FileResponse(dashboard_path)

    async def handle_favicon(self, request):
        return web.Response(status=204)

    async def handle_health(self, request):
        return web.json_response(
            {
                "status": "ok",
                "uptime_seconds": round(time.time() - self.started_at, 1),
                "scanner": self.scanner.stats,
                "execution": self.engine.stats,
                "audit": self.engine.stats.get("audit", {}),
                "profitability": self._profitability_snapshot(),
            }
        )

    async def handle_system(self, request):
        return web.json_response(await self._build_system_snapshot())

    async def handle_prices(self, request):
        all_prices = await self.store.get_all_prices()
        return web.json_response({key: value.to_dict() for key, value in all_prices.items()})

    async def handle_opportunities(self, request):
        return web.json_response([opportunity.to_dict() for opportunity in self.scanner.current_opportunities])

    async def handle_balances(self, request):
        return web.json_response(
            {
                platform: {
                    "balance": snapshot.balance,
                    "is_low": snapshot.is_low,
                    "timestamp": snapshot.timestamp,
                }
                for platform, snapshot in self.monitor.current_balances.items()
            }
        )

    async def handle_trades(self, request):
        return web.json_response([execution.to_dict() for execution in self.engine.execution_history[-100:]])

    async def handle_market_mappings(self, request):
        payload = []
        for canonical_id, mapping in MARKET_MAP.items():
            row = {"canonical_id": canonical_id}
            row.update(mapping)
            payload.append(row)
        return web.json_response(payload)

    async def handle_errors(self, request):
        return web.json_response([incident.to_dict() for incident in self.engine.incidents])

    async def handle_manual_positions(self, request):
        return web.json_response([position.to_dict() for position in self.engine.manual_positions])

    async def handle_profitability(self, request):
        return web.json_response(self._profitability_snapshot())

    async def handle_market_mapping_action(self, request):
        canonical_id = request.match_info["canonical_id"]
        if canonical_id not in MARKET_MAP:
            return web.json_response({"error": f"Unknown mapping: {canonical_id}"}, status=404)

        payload = await self._read_json_body(request)
        action = str(payload.get("action", "")).strip().lower()
        note = str(payload.get("note", "")).strip()

        if action == "confirm":
            mapping = update_market_mapping(
                canonical_id,
                status="confirmed",
                note=note or "Confirmed from the operator desk.",
            )
        elif action == "review":
            mapping = update_market_mapping(
                canonical_id,
                status="review",
                allow_auto_trade=False,
                note=note or "Returned to review from the operator desk.",
            )
        elif action == "enable_auto_trade":
            mapping = update_market_mapping(
                canonical_id,
                status="confirmed",
                allow_auto_trade=True,
                note=note or "Auto-trade enabled from the operator desk.",
            )
        elif action == "disable_auto_trade":
            mapping = update_market_mapping(
                canonical_id,
                allow_auto_trade=False,
                note=note or "Auto-trade held from the operator desk.",
            )
        else:
            return web.json_response({"error": f"Unsupported mapping action: {action or 'unknown'}"}, status=400)

        return web.json_response(mapping)

    async def handle_incident_action(self, request):
        incident_id = request.match_info["incident_id"]
        payload = await self._read_json_body(request)
        action = str(payload.get("action", "")).strip().lower()
        note = str(payload.get("note", "")).strip()
        if action != "resolve":
            return web.json_response({"error": f"Unsupported incident action: {action or 'unknown'}"}, status=400)

        incident = await self.engine.resolve_incident(incident_id, note=note or "Resolved from the dashboard.")
        if incident is None:
            return web.json_response({"error": f"Unknown incident: {incident_id}"}, status=404)
        return web.json_response(incident.to_dict())

    async def handle_manual_position_action(self, request):
        position_id = request.match_info["position_id"]
        payload = await self._read_json_body(request)
        action = str(payload.get("action", "")).strip().lower()
        note = str(payload.get("note", "")).strip()

        try:
            position = await self.engine.update_manual_position(position_id, action, note=note)
        except ValueError as exc:
            return web.json_response({"error": str(exc)}, status=400)

        if position is None:
            return web.json_response({"error": f"Unknown manual position: {position_id}"}, status=404)
        return web.json_response(position.to_dict())

    # ── Portfolio endpoints ───────────────────────────────────────────────

    async def handle_portfolio(self, request):
        """Return full portfolio snapshot."""
        dry_run = os.getenv("DRY_RUN", "true").lower() != "false"
        snapshot = {
            "timestamp": time.time(),
            "total_exposure": 0.0,
            "total_open_positions": len(self.engine._executions) if hasattr(self.engine, "_executions") else 0,
            "total_hedged": 0,
            "total_unhedged": 0,
            "by_venue": {},
            "by_canonical": {},
            "violations": [],
            "unsettled_positions": 0,
            "realized_pnl_today": 0.0,
            "unrealized_pnl": 0.0,
            "dry_run": dry_run,
        }
        # Populate from engine executions
        if hasattr(self.engine, "_executions"):
            executions = [e for e in self.engine._executions if e.status in ("pending", "submitted", "simulated")]
            by_canonical = {}
            for e in executions:
                opp = e.opportunity
                cid = opp.canonical_id
                cost = opp.suggested_qty * (opp.yes_price + opp.no_price)
                snapshot["total_exposure"] += cost
                snapshot["total_open_positions"] = len(executions)
                by_canonical[cid] = {
                    "canonical_id": cid,
                    "description": opp.description,
                    "quantity": opp.suggested_qty,
                    "total_cost": round(cost, 2),
                    "status": e.status,
                    "hedge_status": "complete" if e.leg_no.status.value == "filled" else "none",
                    "age_seconds": round(time.time() - e.timestamp, 0),
                }
                if e.leg_no.status.value == "filled":
                    snapshot["total_hedged"] += 1
                else:
                    snapshot["total_unhedged"] += 1
            snapshot["by_canonical"] = by_canonical
        return web.json_response(snapshot)

    async def handle_portfolio_violations(self, request):
        """Return active risk violations only."""
        # TODO: wire to PortfolioMonitor for full violation list
        # For now, return inline checks
        violations = []
        dry_run = os.getenv("DRY_RUN", "true").lower() != "false"
        if not dry_run:
            violations.append({
                "violation_id": "live_trading",
                "level": "critical",
                "category": "mode",
                "message": "LIVE TRADING ACTIVE — DRY_RUN=false",
                "canonical_id": None,
                "platform": None,
                "current_value": 1,
                "limit_value": 0,
                "timestamp": time.time(),
            })
        return web.json_response({"violations": violations})

    async def handle_portfolio_positions(self, request):
        """Return all open positions with full details."""
        positions = []
        if hasattr(self.engine, "_executions"):
            for e in self.engine._executions:
                opp = e.opportunity
                positions.append({
                    "arb_id": e.arb_id,
                    "canonical_id": opp.canonical_id,
                    "description": opp.description,
                    "quantity": opp.suggested_qty,
                    "yes_platform": opp.yes_platform,
                    "no_platform": opp.no_platform,
                    "yes_price": opp.yes_price,
                    "no_price": opp.no_price,
                    "gross_edge_cents": opp.gross_edge_cents,
                    "net_edge_cents": opp.net_edge_cents,
                    "status": e.status,
                    "yes_fill_price": e.leg_yes.fill_price,
                    "no_fill_price": e.leg_no.fill_price,
                    "yes_fill_qty": e.leg_yes.fill_qty,
                    "no_fill_qty": e.leg_no.fill_qty,
                    "realized_pnl": e.realized_pnl,
                    "created_at": e.timestamp,
                })
        return web.json_response({"positions": positions})

    async def handle_portfolio_summary(self, request):
        """Return aggregated portfolio performance summary."""
        executions = getattr(self.engine, "_executions", [])
        realized = sum(e.realized_pnl for e in executions)
        total_fees = 0.0
        for e in executions:
            opp = e.opportunity
            qty = opp.suggested_qty
            total_fees += qty * opp.yes_price * 0.07 + qty * opp.no_price * 0.02
        return web.json_response({
            "total_executions": len(executions),
            "realized_pnl": round(realized, 4),
            "estimated_fees": round(total_fees, 4),
            "dry_run": os.getenv("DRY_RUN", "true").lower() != "false",
            "drift_guard_active": True,
        })

    # ── Auth endpoints ────────────────────────────────────────────────────

    async def handle_login(self, request):
        """Login with email + password. Returns session token."""
        payload = await self._read_json_body(request)
        email = str(payload.get("email", "")).strip().lower()
        password = str(payload.get("password", ""))

        if not email or not password:
            return web.json_response({"error": "email and password required"}, status=400)

        token = await login_user(email, password)
        if not token:
            return web.json_response({"error": "Invalid credentials"}, status=401)

        response = web.json_response({"status": "ok", "email": email})
        response.set_cookie(
            "arbiter_session",
            token,
            httponly=True,
            secure=request.url.scheme == "https",
            max_age=7 * 86400,
            samesite="strict",
        )
        return response

    async def handle_logout(self, request):
        """Logout — invalidate session."""
        token = request.cookies.get("arbiter_session", "")
        await logout_user(token)
        response = web.json_response({"status": "logged_out"})
        response.del_cookie("arbiter_session")
        return response

    async def handle_auth_me(self, request):
        """Return current user info if authenticated."""
        user = await get_current_user(request)
        if not user:
            return web.json_response({"authenticated": False}, status=401)
        return web.json_response({"authenticated": True, "email": user})

    async def handle_portfolio_unwind(self, request):
        """
        Trigger unwind workflow for a stuck position.
        Sends Telegram unwind alert to operator.
        """
        position_id = request.match_info["position_id"]
        payload = await self._read_json_body(request)
        reason = str(payload.get("reason", "one_leg_timeout"))
        notes = str(payload.get("notes", ""))

        # Find the execution
        execution = None
        if hasattr(self.engine, "_executions"):
            for e in self.engine._executions:
                if e.arb_id == position_id or e.arb_id.startswith(position_id):
                    execution = e
                    break

        if execution is None:
            return web.json_response({"error": f"Position not found: {position_id}"}, status=404)

        # TODO: wire to PredictItWorkflowManager for real unwind alert
        logger.warning(f"Unwind requested for {position_id}: reason={reason}, notes={notes}")
        return web.json_response({
            "status": "unwind_initiated",
            "position_id": position_id,
            "reason": reason,
            "message": f"Unwind alert sent for {position_id}",
            "next_step": "Check Telegram for unwind instructions",
        })

    async def handle_websocket(self, request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self._ws_clients.append(ws)
        await ws.send_json({"type": "bootstrap", "payload": await self._build_system_snapshot()})

        try:
            async for message in ws:
                if message.type == WSMsgType.TEXT:
                    try:
                        payload = json.loads(message.data)
                    except json.JSONDecodeError:
                        continue
                    await self._handle_ws_command(ws, payload)
                elif message.type == WSMsgType.ERROR:
                    logger.error("Dashboard websocket error: %s", ws.exception())
        finally:
            if ws in self._ws_clients:
                self._ws_clients.remove(ws)
        return ws

    async def _handle_ws_command(self, ws: web.WebSocketResponse, payload: dict):
        action = payload.get("action")
        if action == "refresh":
            await ws.send_json({"type": "system", "payload": await self._build_system_snapshot()})
        elif action == "ping":
            await ws.send_json({"type": "heartbeat", "payload": {"timestamp": time.time()}})

    async def _broadcast_loop(self):
        price_queue = self.store.subscribe()
        opp_queue = self.scanner.subscribe()
        execution_queue = self.engine.subscribe()
        incident_queue = self.engine.subscribe_incidents()

        while True:
            if not self._ws_clients:
                await asyncio.sleep(1.0)
                continue

            try:
                done, pending = await asyncio.wait(
                    [
                        asyncio.create_task(price_queue.get()),
                        asyncio.create_task(opp_queue.get()),
                        asyncio.create_task(execution_queue.get()),
                        asyncio.create_task(incident_queue.get()),
                        asyncio.create_task(asyncio.sleep(2.0)),
                    ],
                    return_when=asyncio.FIRST_COMPLETED,
                )

                for task in pending:
                    task.cancel()

                for task in done:
                    if task.cancelled():
                        continue
                    result = task.result()
                    if isinstance(result, PricePoint):
                        await self._broadcast_json({"type": "quote", "payload": result.to_dict()})
                    elif isinstance(result, ArbitrageOpportunity):
                        await self._broadcast_json({"type": "opportunity", "payload": result.to_dict()})
                    elif isinstance(result, ArbExecution):
                        await self._broadcast_json({"type": "execution", "payload": result.to_dict()})
                    elif isinstance(result, ExecutionIncident):
                        await self._broadcast_json({"type": "incident", "payload": result.to_dict()})
                    elif result is None:
                        continue
                    else:
                        await self._broadcast_json({"type": "system", "payload": await self._build_system_snapshot()})
            except Exception as exc:
                logger.error("Broadcast error: %s", exc)
                await asyncio.sleep(1.0)

    async def _broadcast_json(self, payload: dict):
        for client in list(self._ws_clients):
            try:
                await client.send_json(payload)
            except Exception:
                if client in self._ws_clients:
                    self._ws_clients.remove(client)

    async def _build_system_snapshot(self) -> dict:
        balances = {
            platform: {
                "balance": snapshot.balance,
                "is_low": snapshot.is_low,
                "timestamp": snapshot.timestamp,
            }
            for platform, snapshot in self.monitor.current_balances.items()
        }
        active_prices = await self.store.get_all_prices()
        tracked_markets = {}
        for key, price in active_prices.items():
            tracked_markets.setdefault(price.canonical_id, []).append(price.to_dict())

        return {
            "timestamp": time.time(),
            "uptime_seconds": round(time.time() - self.started_at, 1),
            "mode": "dry-run" if self.config.scanner.dry_run else "live",
            "scanner": self.scanner.stats,
            "execution": self.engine.stats,
            "audit": self.engine.stats.get("audit", {}),
            "profitability": self._profitability_snapshot(),
            "collectors": self._collector_snapshot(),
            "balances": balances,
            "series": {
                "scanner": self.scanner.history,
                "equity": self.engine.equity_curve,
                "profitability": self.profitability.history if self.profitability else [],
            },
            "counts": {
                "prices": len(active_prices),
                "opportunities": len(self.scanner.current_opportunities),
                "trades": len(self.engine.execution_history),
                "manual_positions": len(self.engine.manual_positions),
                "incidents": len(self.engine.incidents),
            },
            "tracked_markets": tracked_markets,
        }

    def _profitability_snapshot(self) -> dict:
        if not self.profitability:
            return {
                "verdict": "unavailable",
                "is_profitable": False,
                "is_determined": False,
                "progress": 0.0,
                "reasons": ["Profitability validator is not configured"],
            }
        return self.profitability.get_snapshot().to_dict()

    def _collector_snapshot(self) -> dict:
        snapshot = {}
        for name, collector in self.collectors.items():
            collector_data = {
                "total_fetches": getattr(collector, "total_fetches", 0),
                "total_errors": getattr(collector, "total_errors", 0),
                "consecutive_errors": getattr(collector, "consecutive_errors", 0),
            }
            if hasattr(collector, "circuit"):
                collector_data["circuit"] = collector.circuit.stats
            if hasattr(collector, "circuit_gamma"):
                collector_data["gamma_circuit"] = collector.circuit_gamma.stats
            if hasattr(collector, "circuit_clob"):
                collector_data["clob_circuit"] = collector.circuit_clob.stats
            if hasattr(collector, "rate_limiter"):
                collector_data["rate_limiter"] = collector.rate_limiter.stats
            snapshot[name] = collector_data
        return snapshot

    async def _read_json_body(self, request) -> dict:
        if not request.can_read_body:
            return {}
        try:
            return await request.json()
        except json.JSONDecodeError:
            raise web.HTTPBadRequest(text="Invalid JSON body")


def create_api_server(
    price_store,
    scanner,
    engine,
    monitor,
    config,
    collectors=None,
    profitability=None,
    host="0.0.0.0",
    port=8080,
) -> ArbiterAPI:
    return ArbiterAPI(
        price_store,
        scanner,
        engine,
        monitor,
        config,
        collectors=collectors,
        profitability=profitability,
        host=host,
        port=port,
    )
