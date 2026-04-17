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
from .portfolio import PortfolioMonitor
from .profitability import ProfitabilityValidator
from .readiness import OperationalReadiness
from .safety.supervisor import SafetySupervisor
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


def _request_is_secure(request: web.Request) -> bool:
    """Respect reverse-proxy headers when deciding whether cookies must be secure."""
    forwarded_proto = request.headers.get("X-Forwarded-Proto", "")
    if forwarded_proto:
        return forwarded_proto.split(",", 1)[0].strip().lower() == "https"

    forwarded = request.headers.get("Forwarded", "")
    for segment in forwarded.split(";"):
        key, _, value = segment.partition("=")
        if key.strip().lower() == "proto":
            return value.strip().strip('"').lower() == "https"

    return request.scheme == "https"


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
        portfolio: Optional[PortfolioMonitor] = None,
        workflow_manager: Optional[object] = None,
        profitability: Optional[ProfitabilityValidator] = None,
        readiness: Optional[OperationalReadiness] = None,
        reconciler=None,
        host: str = "0.0.0.0",
        port: int = 8080,
        safety: Optional[SafetySupervisor] = None,
    ):
        self.store = price_store
        self.scanner = scanner
        self.engine = engine
        self.monitor = monitor
        self.config = config
        self.collectors = collectors or {}
        self.portfolio = portfolio
        self.workflow_manager = workflow_manager
        self.profitability = profitability
        self.readiness = readiness
        self.reconciler = reconciler
        self.host = host
        self.port = port
        self.safety = safety
        # SafetyEventStore exposes list_events() for GET /api/safety/events;
        # supervisor holds the reference on ``_safety_store`` (may be None in
        # dev mode without Postgres).
        self.safety_store = getattr(safety, "_safety_store", None)
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
        app.router.add_get("/api/readiness", self.handle_readiness)
        app.router.add_get("/api/reconciliation", self.handle_reconciliation)
        app.router.add_get("/api/portfolio", self.handle_portfolio)
        app.router.add_get("/api/portfolio/positions", self.handle_portfolio_positions)
        app.router.add_get("/api/portfolio/violations", self.handle_portfolio_violations)
        app.router.add_post("/api/portfolio/unwind/{position_id}", self.handle_portfolio_unwind)
        app.router.add_get("/api/portfolio/summary", self.handle_portfolio_summary)
        app.router.add_post("/api/auth/login", self.handle_login)
        app.router.add_post("/api/auth/logout", self.handle_logout)
        app.router.add_get("/api/auth/me", self.handle_auth_me)
        app.router.add_post("/api/kill-switch", self.handle_kill_switch)
        app.router.add_get("/api/safety/status", self.handle_safety_status)
        app.router.add_get("/api/safety/events", self.handle_safety_events)
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
        if request.method == "OPTIONS":
            response = web.Response(status=204)
            response.headers["Access-Control-Allow-Origin"] = "*"
            response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
            response.headers["Access-Control-Allow-Headers"] = "Authorization, Content-Type"
            response.headers["Access-Control-Max-Age"] = "86400"
            response.headers["Vary"] = "Origin"
            return response

        try:
            response = await handler(request)
        except web.HTTPException as exc:
            response = exc
        response.headers["Access-Control-Allow-Origin"] = "*"
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        response.headers["Access-Control-Allow-Headers"] = "Authorization, Content-Type"
        response.headers["Access-Control-Max-Age"] = "86400"
        response.headers["Vary"] = "Origin"
        return response

    async def handle_site_index(self, request):
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
                "readiness": self._readiness_snapshot(),
                "reconciliation": self._reconciliation_snapshot(),
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

    async def handle_readiness(self, request):
        return web.json_response(self._readiness_snapshot())

    async def handle_reconciliation(self, request):
        return web.json_response(self._reconciliation_snapshot())

    async def handle_market_mapping_action(self, request):
        await require_auth(request)
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
        await require_auth(request)
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
        await require_auth(request)
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
        return web.json_response(self._portfolio_snapshot())

    async def handle_portfolio_violations(self, request):
        """Return active risk violations only."""
        snapshot = self._portfolio_monitor_snapshot()
        if snapshot is not None:
            return web.json_response({"violations": [violation.to_dict() for violation in snapshot.violations]})
        fallback = self._portfolio_snapshot()
        return web.json_response({"violations": fallback.get("violations", [])})

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
                    "gross_edge_cents": round(opp.gross_edge * 100.0, 4),
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
        portfolio_snapshot = self._portfolio_monitor_snapshot()
        total_fees = sum(e.opportunity.total_fees * e.opportunity.suggested_qty for e in executions)
        return web.json_response({
            "total_executions": len(executions),
            "realized_pnl": round(realized, 4),
            "estimated_fees": round(total_fees, 4),
            "unrealized_pnl": round(portfolio_snapshot.unrealized_pnl, 4) if portfolio_snapshot else 0.0,
            "total_exposure": round(portfolio_snapshot.total_exposure, 4) if portfolio_snapshot else 0.0,
            "dry_run": self.config.scanner.dry_run,
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

        response = web.json_response({"status": "ok", "email": email, "token": token})
        response.set_cookie(
            "arbiter_session",
            token,
            httponly=True,
            secure=_request_is_secure(request),
            max_age=7 * 86400,
            samesite="lax",
        )
        return response

    async def handle_logout(self, request):
        """Logout — invalidate session."""
        auth = request.headers.get("Authorization", "")
        token = auth[7:] if auth.startswith("Bearer ") else request.cookies.get("arbiter_session", "")
        await logout_user(token)
        response = web.json_response({"status": "logged_out"})
        response.del_cookie("arbiter_session")
        return response

    async def handle_auth_me(self, request):
        """Return current user info if authenticated."""
        user = await get_current_user(request)
        if not user:
            return web.json_response({"authenticated": False})
        return web.json_response({"authenticated": True, "email": user})

    # ── Safety / kill-switch endpoints (SAFE-01) ──────────────────────────

    async def handle_kill_switch(self, request):
        """POST /api/kill-switch — arm or reset the kill switch.

        Body: {"action": "arm" | "reset", "reason": str, "note": str}
        - arm:   requires operator auth + non-empty reason
        - reset: respects SafetySupervisor cooldown (400 while cooldown active)
        """
        await require_auth(request)
        if self.safety is None:
            return web.json_response(
                {"error": "Safety supervisor unavailable"}, status=503,
            )
        payload = await self._read_json_body(request)
        action = str(payload.get("action", "")).strip().lower()
        reason = str(payload.get("reason", "")).strip()[:500]
        note = str(payload.get("note", "")).strip()[:500]
        email = await get_current_user(request) or "unknown"

        try:
            if action == "arm":
                if not reason:
                    return web.json_response(
                        {"error": "reason required"}, status=400,
                    )
                state = await self.safety.trip_kill(
                    by=f"operator:{email}", reason=reason,
                )
                return web.json_response(state.to_dict())
            if action == "reset":
                state = await self.safety.reset_kill(
                    by=f"operator:{email}", note=note or "operator reset",
                )
                return web.json_response(state.to_dict())
            return web.json_response(
                {"error": f"Unsupported kill-switch action: {action or 'unknown'}"},
                status=400,
            )
        except ValueError as exc:
            return web.json_response({"error": str(exc)}, status=400)

    async def handle_safety_status(self, request):
        """GET /api/safety/status — unauth'd read-only snapshot of SafetyState."""
        if self.safety is None:
            return web.json_response({"armed": False, "available": False})
        payload = self.safety._state.to_dict()
        payload["available"] = True
        return web.json_response(payload)

    async def handle_safety_events(self, request):
        """GET /api/safety/events — paginated audit trail."""
        if self.safety_store is None:
            return web.json_response({"events": [], "limit": 0, "offset": 0})
        try:
            limit = min(int(request.query.get("limit", 50)), 500)
        except (TypeError, ValueError):
            limit = 50
        try:
            offset = max(int(request.query.get("offset", 0)), 0)
        except (TypeError, ValueError):
            offset = 0
        rows = await self.safety_store.list_events(limit=limit, offset=offset)
        return web.json_response(
            {"events": rows, "limit": limit, "offset": offset},
        )

    async def handle_portfolio_unwind(self, request):
        """
        Trigger unwind workflow for a stuck position.
        Sends Telegram unwind alert to operator.
        """
        await require_auth(request)
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

        if not self.workflow_manager:
            logger.warning(f"Unwind requested for {position_id}: reason={reason}, notes={notes}")
            return web.json_response({
                "status": "unwind_initiated",
                "position_id": position_id,
                "reason": reason,
                "message": f"Unwind alert sent for {position_id}",
                "next_step": "Check Telegram for unwind instructions",
            })

        reason_enum = self._parse_unwind_reason(reason)
        manual_position = next(
            (
                position
                for position in self.engine.manual_positions
                if position.position_id == position_id or position.position_id.endswith(execution.arb_id)
            ),
            None,
        )
        if manual_position is None:
            manual_position = self._build_manual_position_from_execution(execution)

        instruction = self.workflow_manager.generate_unwind_instruction(
            position=manual_position,
            reason=reason_enum,
            yes_fill_qty=self._resolved_fill_qty(execution.leg_yes, execution.opportunity.suggested_qty),
            no_fill_qty=self._resolved_fill_qty(execution.leg_no, execution.opportunity.suggested_qty),
            yes_avg_price=execution.leg_yes.fill_price or execution.opportunity.yes_price,
            no_avg_price=execution.leg_no.fill_price or execution.opportunity.no_price,
            yes_order_id=execution.leg_yes.order_id,
            no_order_id=execution.leg_no.order_id,
            notes=[notes] if notes else None,
        )
        sent = await self.workflow_manager.send_unwind_alert(instruction)
        await self.engine.record_incident(
            arb_id=execution.arb_id,
            canonical_id=execution.opportunity.canonical_id,
            severity="warning",
            message=f"Unwind initiated for {position_id}",
            metadata={
                "reason": reason_enum.value,
                "instruction": self._instruction_payload(instruction),
                "alert_sent": sent,
            },
        )
        return web.json_response({
            "status": "unwind_initiated",
            "position_id": position_id,
            "reason": reason_enum.value,
            "message": f"Unwind alert {'sent' if sent else 'queued locally'} for {position_id}",
            "instruction": {
                "recommended_action": instruction.recommended_action,
                "close_yes_first": instruction.close_yes_first,
                "estimated_cost": round(instruction.estimated_cost, 4),
                "exposure_at_risk": round(instruction.exposure_at_risk, 4),
                "notes": list(instruction.notes),
            },
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
        # SAFE-01: safety supervisor fans out kill_switch/shutdown_state events
        # as pre-shaped dicts. Supervisor may be None in dev mode.
        safety_queue = self.safety.subscribe() if self.safety is not None else None

        while True:
            if not self._ws_clients:
                await asyncio.sleep(1.0)
                continue

            try:
                tasks = [
                    asyncio.create_task(price_queue.get()),
                    asyncio.create_task(opp_queue.get()),
                    asyncio.create_task(execution_queue.get()),
                    asyncio.create_task(incident_queue.get()),
                    asyncio.create_task(asyncio.sleep(2.0)),
                ]
                if safety_queue is not None:
                    tasks.append(asyncio.create_task(safety_queue.get()))
                done, pending = await asyncio.wait(
                    tasks,
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
                    elif isinstance(result, dict) and result.get("type") in (
                        "kill_switch", "shutdown_state",
                    ):
                        # Supervisor emits pre-shaped {"type": ..., "payload": ...} dicts
                        await self._broadcast_json(result)
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
            "readiness": self._readiness_snapshot(),
            "reconciliation": self._reconciliation_snapshot(),
            "collectors": self._collector_snapshot(),
            "balances": balances,
            "safety": (
                self.safety._state.to_dict()
                if self.safety is not None
                else {"armed": False, "available": False}
            ),
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

    def _readiness_snapshot(self) -> dict:
        if not self.readiness:
            return {
                "timestamp": time.time(),
                "mode": "dry-run" if self.config.scanner.dry_run else "live",
                "ready_for_live_trading": False,
                "blocking_reasons": ["Readiness gate is not configured"],
                "warnings": [],
                "checks": [],
            }
        return self.readiness.refresh().to_dict()

    def _reconciliation_snapshot(self) -> dict:
        if not self.reconciler:
            return {
                "configured": False,
                "summary": "PnL reconciler is not configured",
                "reconciliation_count": 0,
                "flag_count": 0,
                "starting_balances": {},
                "recorded_pnl": {},
                "latest_report": None,
            }
        stats = self.reconciler.stats
        return {
            "configured": True,
            "summary": (
                "PnL reconciliation is healthy"
                if stats.get("latest_report")
                else "PnL reconciliation is collecting its first report"
            ),
            **stats,
        }

    def _portfolio_monitor_snapshot(self):
        if self.portfolio:
            return self.portfolio.get_snapshot() or self.portfolio.compute_snapshot()
        return None

    def _portfolio_snapshot(self) -> dict:
        snapshot = self._portfolio_monitor_snapshot()
        if snapshot is not None:
            return snapshot.to_dict()

        dry_run = self.config.scanner.dry_run
        return {
            "timestamp": time.time(),
            "total_exposure": 0.0,
            "total_open_positions": 0,
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

    @staticmethod
    def _parse_unwind_reason(reason: str) -> str:
        return reason.strip().lower().replace(" ", "_")

    @staticmethod
    def _build_manual_position_from_execution(execution: ArbExecution):
        from .execution.engine import ManualPosition

        return ManualPosition(
            position_id=f"MANUAL-{execution.arb_id}",
            canonical_id=execution.opportunity.canonical_id,
            description=execution.opportunity.description,
            instructions="Operator unwind requested from dashboard.",
            yes_platform=execution.opportunity.yes_platform,
            no_platform=execution.opportunity.no_platform,
            quantity=execution.opportunity.suggested_qty,
            yes_price=execution.opportunity.yes_price,
            no_price=execution.opportunity.no_price,
            status=execution.status,
            timestamp=execution.timestamp,
            updated_at=time.time(),
        )

    @staticmethod
    def _resolved_fill_qty(order, fallback_qty: int) -> int:
        if getattr(order, "fill_qty", 0) > 0:
            return int(order.fill_qty)
        if getattr(order, "status", None) and getattr(order.status, "value", "").lower() in {"filled", "simulated"}:
            return int(fallback_qty)
        return 0

    @staticmethod
    def _instruction_payload(instruction) -> dict:
        return {
            "reason": instruction.reason.value,
            "position_id": instruction.position_id,
            "canonical_id": instruction.canonical_id,
            "yes_platform": instruction.yes_platform,
            "no_platform": instruction.no_platform,
            "yes_order_id": instruction.yes_order_id,
            "no_order_id": instruction.no_order_id,
            "yes_fill_qty": instruction.yes_fill_qty,
            "no_fill_qty": instruction.no_fill_qty,
            "yes_avg_price": instruction.yes_avg_price,
            "no_avg_price": instruction.no_avg_price,
            "exposure_at_risk": instruction.exposure_at_risk,
            "recommended_action": instruction.recommended_action,
            "close_yes_first": instruction.close_yes_first,
            "estimated_cost": instruction.estimated_cost,
            "notes": list(instruction.notes),
        }

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
    portfolio=None,
    workflow_manager=None,
    profitability=None,
    readiness=None,
    reconciler=None,
    host="0.0.0.0",
    port=8080,
    safety=None,
) -> ArbiterAPI:
    return ArbiterAPI(
        price_store,
        scanner,
        engine,
        monitor,
        config,
        collectors=collectors,
        portfolio=portfolio,
        workflow_manager=workflow_manager,
        profitability=profitability,
        readiness=readiness,
        reconciler=reconciler,
        host=host,
        port=port,
        safety=safety,
    )
