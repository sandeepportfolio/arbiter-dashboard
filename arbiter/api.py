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
import socket
import time
from pathlib import Path
from typing import Dict, List, Optional

from aiohttp import WSMsgType, web
from aiohttp.web_exceptions import HTTPUnauthorized

from .config.settings import MARKET_MAP, ArbiterConfig, update_market_mapping
from .execution.engine import ArbExecution, ExecutionEngine, ExecutionIncident
from .monitor.balance import BalanceMonitor
from .operator_settings import OperatorSettingsStore, load_market_discovery_settings
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


def _build_allowed_users() -> Dict[str, str]:
    """Load operator credentials, preferring production OPS_* names."""
    users: Dict[str, str] = {}
    for email_key, password_key in (
        ("OPS_EMAIL", "OPS_PASSWORD"),
        ("UI_USER_EMAIL", "UI_USER_PASSWORD"),
    ):
        email = os.getenv(email_key, "").strip().lower()
        password = os.getenv(password_key, "")
        if email and password:
            users[email] = _hash_password(password)

    if users:
        return users

    return {
        "sparx.sandeep@gmail.com": _hash_password("saibaba"),
    }


UI_ALLOWED_USERS = _build_allowed_users()


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
        mapping_store=None,
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
        self.mapping_store = mapping_store
        # SafetyEventStore exposes list_events() for GET /api/safety/events;
        # supervisor holds the reference on ``_safety_store`` (may be None in
        # dev mode without Postgres).
        self.safety_store = getattr(safety, "_safety_store", None)
        self.started_at = time.time()
        self._ws_clients: list[web.WebSocketResponse] = []
        self._site_index = Path(__file__).resolve().parent.parent / "index.html"
        self._dashboard_dir = Path(__file__).resolve().parent / "web"
        self.auto_executor = None
        self._broadcast_task: Optional[asyncio.Task] = None
        self._operator_settings_store = OperatorSettingsStore()
        self._market_discovery_settings = load_market_discovery_settings(self._operator_settings_store)
        self._operator_settings_meta = {
            "persisted": False,
            "updated_at": None,
            "updated_by": None,
        }
        # SAFE-04: periodic broadcaster task for rate_limit_state events.
        # Started in serve(); cancelled on shutdown.
        self._rate_limit_task: Optional[asyncio.Task] = None
        self._startup_event = asyncio.Event()
        self._startup_error: Optional[BaseException] = None
        self._restore_operator_settings()

    def attach_auto_executor(self, auto_executor) -> None:
        self.auto_executor = auto_executor
        self._restore_operator_settings()

    async def serve(self):
        app = web.Application(middlewares=[self._cors_middleware])
        app.router.add_get("/", self.handle_site_index)
        app.router.add_get("/health", self.handle_liveness)
        app.router.add_get("/ready", self.handle_service_ready)
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
        app.router.add_get("/api/market-mappings/{canonical_id}/audit", self.handle_market_mapping_audit)
        app.router.add_get("/api/settings", self.handle_settings)
        app.router.add_post("/api/settings", self.handle_settings_update)
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
        app.router.add_get("/api/metrics", self.handle_metrics)
        app.router.add_get("/ws", self.handle_websocket)

        runner = web.AppRunner(app)
        try:
            await runner.setup()
            await self._start_site(runner)
            logger.info("ARBITER API listening at http://%s:%s", self.host, self.port)
            self._startup_error = None
            self._startup_event.set()
            self._broadcast_task = asyncio.create_task(self._broadcast_loop(), name="api-broadcast")
            # SAFE-04: launch the periodic rate_limit_state broadcaster.
            self._rate_limit_task = asyncio.create_task(
                self._rate_limit_broadcast_loop(),
                name="api-rate-limit-broadcast",
            )
            while True:
                await asyncio.sleep(3600)
        except Exception as exc:
            self._startup_error = exc
            self._startup_event.set()
            raise
        finally:
            await self._cancel_background_task(self._broadcast_task)
            self._broadcast_task = None
            # Cancel the rate-limit broadcaster on shutdown so the asyncio loop
            # doesn't report a pending task on exit.
            await self._cancel_background_task(self._rate_limit_task)
            self._rate_limit_task = None
            await runner.cleanup()

    async def wait_until_started(self, timeout: float = 10.0) -> None:
        await asyncio.wait_for(self._startup_event.wait(), timeout=timeout)
        if self._startup_error is not None:
            raise RuntimeError(
                f"ARBITER API failed to start on {self.host}:{self.port}: {self._startup_error}"
            ) from self._startup_error

    async def _start_site(self, runner: web.AppRunner) -> None:
        site = web.TCPSite(runner, self.host, self.port)
        try:
            await site.start()
        except OSError as exc:
            if not self._should_retry_with_socksite(exc):
                raise

            logger.warning(
                "aiohttp TCPSite start failed on %s:%s (%s); retrying with SockSite fallback",
                self.host,
                self.port,
                exc,
            )
            listen_socket = self._create_listen_socket()
            fallback_site = web.SockSite(runner, listen_socket)
            try:
                await fallback_site.start()
            except Exception:
                listen_socket.close()
                raise

    def _create_listen_socket(self) -> socket.socket:
        sock = socket.create_server((self.host, self.port), reuse_port=False, backlog=128)
        sock.setblocking(False)
        return sock

    @staticmethod
    def _should_retry_with_socksite(exc: OSError) -> bool:
        text = str(exc).lower()
        return exc.errno == 22 and ("keepalive" in text or "invalid argument" in text)

    @staticmethod
    async def _cancel_background_task(task: Optional[asyncio.Task]) -> None:
        if task is None or task.done():
            return
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, BaseException):
            pass

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

    async def handle_liveness(self, request):
        return web.json_response(self._service_health_snapshot())

    async def handle_service_ready(self, request):
        payload = self._service_ready_snapshot()
        return web.json_response(payload, status=200 if payload["ready"] else 503)

    async def handle_health(self, request):
        readiness = self._readiness_snapshot()
        return web.json_response(
            {
                "status": "ok",
                "probe": "liveness",
                "service_ready": True,
                "live_trading_ready": readiness.get("ready_for_live_trading", False),
                "uptime_seconds": round(time.time() - self.started_at, 1),
                "scanner": self.scanner.stats,
                "execution": self.engine.stats,
                "audit": self.engine.stats.get("audit", {}),
                "profitability": self._profitability_snapshot(),
                "readiness": readiness,
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
        if self.mapping_store is not None:
            try:
                status = str(request.query.get("status", "") or "").strip() or None
                raw_limit = request.query.get("limit", "500")
                limit = min(max(int(raw_limit), 1), 5000)
                payload = [
                    mapping.to_dict()
                    for mapping in await self.mapping_store.all(status=status, limit=limit)
                ]
            except Exception as exc:
                logger.warning(
                    "Failed to load market mappings from store, falling back to runtime cache: %s",
                    exc,
                )
                payload = []

        if not payload:
            for canonical_id, mapping in MARKET_MAP.items():
                row = {"canonical_id": canonical_id}
                row.update(mapping)
                payload.append(row)

        for row in payload:
            # SAFE-06 (plan 03-06): every mapping row exposes resolution_criteria
            # and resolution_match_status so the dashboard can render the
            # side-by-side comparison (plan 03-07). Entries without the new
            # fields fall back to sane defaults — never raise KeyError.
            row.setdefault("resolution_criteria", None)
            row.setdefault("resolution_match_status", "pending_operator_review")

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

    async def handle_market_mapping_audit(self, request):
        """Return the audit log for a single mapping (Phase 6 Plan 06-05).

        Operator-only (require_auth). Response shape:
            { "canonical_id": "...", "audit_log": [ {ts, actor, field, old, new, note}, ... ] }
        """
        await require_auth(request)
        canonical_id = request.match_info["canonical_id"]
        if canonical_id not in MARKET_MAP:
            return web.json_response({"error": f"Unknown mapping: {canonical_id}"}, status=404)
        mapping = MARKET_MAP[canonical_id]
        return web.json_response({
            "canonical_id": canonical_id,
            "audit_log": list(mapping.get("audit_log") or []),
        })

    async def handle_market_mapping_action(self, request):
        actor = await require_auth(request)
        canonical_id = request.match_info["canonical_id"]
        if canonical_id not in MARKET_MAP:
            return web.json_response({"error": f"Unknown mapping: {canonical_id}"}, status=404)

        payload = await self._read_json_body(request)
        action = str(payload.get("action", "")).strip().lower()
        note = str(payload.get("note", "")).strip()

        # SAFE-06 (plan 03-06, threat T-3-06-B): resolution_criteria rides any
        # action (confirm / review / enable_auto_trade / disable_auto_trade).
        # Validate criteria_match enum before accepting the payload so the
        # UI can't smuggle arbitrary strings into downstream renderers.
        resolution_criteria = payload.get("resolution_criteria")
        resolution_match_status = payload.get("resolution_match_status")
        _ALLOWED_CRITERIA_MATCH = {
            None, "identical", "similar", "divergent", "pending_operator_review",
        }
        if resolution_criteria is not None:
            if not isinstance(resolution_criteria, dict):
                return web.json_response(
                    {"error": "resolution_criteria must be an object"}, status=400,
                )
            criteria_match_value = resolution_criteria.get("criteria_match")
            if criteria_match_value not in _ALLOWED_CRITERIA_MATCH:
                return web.json_response(
                    {
                        "error": (
                            "Invalid criteria_match; expected one of "
                            "identical, similar, divergent, pending_operator_review"
                        )
                    },
                    status=400,
                )
        if resolution_match_status is not None and resolution_match_status not in (
            "identical", "similar", "divergent", "pending_operator_review",
        ):
            return web.json_response(
                {"error": "Invalid resolution_match_status"}, status=400,
            )

        update_kwargs: dict = {}
        if resolution_criteria is not None:
            update_kwargs["resolution_criteria"] = resolution_criteria
        if resolution_match_status is not None:
            update_kwargs["resolution_match_status"] = resolution_match_status

        current_mapping = MARKET_MAP[canonical_id]
        effective_match_status = (
            resolution_match_status
            or (
                resolution_criteria.get("criteria_match")
                if isinstance(resolution_criteria, dict)
                else None
            )
            or current_mapping.get("resolution_match_status", "pending_operator_review")
        )

        if action == "confirm":
            mapping = update_market_mapping(
                canonical_id,
                status="confirmed",
                note=note or "Confirmed from the operator desk.",
                actor=actor,
                **update_kwargs,
            )
        elif action == "review":
            mapping = update_market_mapping(
                canonical_id,
                status="review",
                allow_auto_trade=False,
                note=note or "Returned to review from the operator desk.",
                actor=actor,
                **update_kwargs,
            )
        elif action == "enable_auto_trade":
            if str(current_mapping.get("status", "candidate")).lower() != "confirmed":
                return web.json_response(
                    {"error": "enable_auto_trade requires an already confirmed mapping"},
                    status=400,
                )
            if str(effective_match_status or "").lower() != "identical":
                return web.json_response(
                    {"error": "enable_auto_trade requires resolution_match_status=identical"},
                    status=400,
                )
            mapping = update_market_mapping(
                canonical_id,
                allow_auto_trade=True,
                note=note or "Auto-trade enabled from the operator desk.",
                actor=actor,
                **update_kwargs,
            )
        elif action == "disable_auto_trade":
            mapping = update_market_mapping(
                canonical_id,
                allow_auto_trade=False,
                note=note or "Auto-trade held from the operator desk.",
                actor=actor,
                **update_kwargs,
            )
        else:
            return web.json_response({"error": f"Unsupported mapping action: {action or 'unknown'}"}, status=400)

        if mapping is not None and self.mapping_store is not None:
            from .mapping.market_map import MarketMapping

            await self.mapping_store.upsert(
                MarketMapping.from_dict(canonical_id, mapping)
            )

        # SAFE-06: emit a `mapping_state` WS event whenever the criteria or
        # match status changed so dashboards can refresh without polling.
        if mapping is not None and (
            resolution_criteria is not None or resolution_match_status is not None
        ):
            await self._broadcast_json(
                {
                    "type": "mapping_state",
                    "payload": {
                        "canonical_id": canonical_id,
                        "resolution_criteria": mapping.get("resolution_criteria"),
                        "resolution_match_status": mapping.get(
                            "resolution_match_status",
                            "pending_operator_review",
                        ),
                        "status": mapping.get("status"),
                        "updated_at": mapping.get("updated_at"),
                    },
                }
            )

        return web.json_response(mapping)

    async def handle_settings(self, request):
        return web.json_response(self._settings_snapshot())

    async def handle_settings_update(self, request):
        actor = await require_auth(request)
        payload = await self._read_json_body(request)
        try:
            snapshot = self._update_operator_settings(payload, actor=actor)
        except ValueError as exc:
            return web.json_response({"error": str(exc)}, status=400)
        await self._broadcast_json({"type": "settings", "payload": snapshot})
        return web.json_response(snapshot)

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

    async def handle_metrics(self, request):
        """Prometheus text-exposition metrics.

        Phase 6 Plan 06-04 deliverable. Content-Type: text/plain; version=0.0.4.
        Scrape from Prometheus with:

            - job_name: arbiter
              static_configs:
                - targets: ['arbiter-api-prod:8080']
              metrics_path: /api/metrics
              scheme: http
        """
        lines: list[str] = []

        scanner_stats = getattr(self.scanner, "stats", {}) or {}
        engine_stats = getattr(self.engine, "stats", {}) or {}
        safety_armed = 1 if (self.safety and self.safety.is_armed) else 0

        lines.append("# HELP arbiter_build_info Arbiter build metadata")
        lines.append("# TYPE arbiter_build_info gauge")
        lines.append(
            f'arbiter_build_info{{release="{os.getenv("ARBITER_RELEASE", "dev")}",env="{os.getenv("ARBITER_ENV", "dev")}"}} 1'
        )

        lines.append("# HELP arbiter_scanner_scans_total Total scanner iterations")
        lines.append("# TYPE arbiter_scanner_scans_total counter")
        lines.append(f"arbiter_scanner_scans_total {int(scanner_stats.get('scan_count', 0))}")

        lines.append("# HELP arbiter_scanner_active_opportunities Current opportunities in flight")
        lines.append("# TYPE arbiter_scanner_active_opportunities gauge")
        lines.append(
            f"arbiter_scanner_active_opportunities {int(scanner_stats.get('active_opportunities', 0))}"
        )

        lines.append("# HELP arbiter_scanner_best_edge_cents Best currently-tradable edge (cents)")
        lines.append("# TYPE arbiter_scanner_best_edge_cents gauge")
        lines.append(f"arbiter_scanner_best_edge_cents {float(scanner_stats.get('best_edge_cents', 0))}")

        lines.append("# HELP arbiter_scanner_last_scan_ms Latest scan duration (ms)")
        lines.append("# TYPE arbiter_scanner_last_scan_ms gauge")
        lines.append(f"arbiter_scanner_last_scan_ms {float(scanner_stats.get('last_scan_ms', 0))}")

        lines.append("# HELP arbiter_executions_total Total trade executions, by status")
        lines.append("# TYPE arbiter_executions_total counter")
        lines.append(f'arbiter_executions_total{{status="live"}} {int(engine_stats.get("live", 0))}')
        lines.append(f'arbiter_executions_total{{status="simulated"}} {int(engine_stats.get("simulated", 0))}')
        lines.append(f'arbiter_executions_total{{status="manual"}} {int(engine_stats.get("manual", 0))}')

        lines.append("# HELP arbiter_incidents_total Total recorded incidents")
        lines.append("# TYPE arbiter_incidents_total counter")
        lines.append(f"arbiter_incidents_total {int(engine_stats.get('incidents', 0))}")

        lines.append("# HELP arbiter_recoveries_total One-leg recovery completions")
        lines.append("# TYPE arbiter_recoveries_total counter")
        lines.append(f"arbiter_recoveries_total {int(engine_stats.get('recoveries', 0))}")

        lines.append("# HELP arbiter_aborts_total Trades aborted (reconcile breach / auto_abort)")
        lines.append("# TYPE arbiter_aborts_total counter")
        lines.append(f"arbiter_aborts_total {int(engine_stats.get('aborted', 0))}")

        lines.append("# HELP arbiter_pnl_total Cumulative realized PnL (USD)")
        lines.append("# TYPE arbiter_pnl_total gauge")
        lines.append(f"arbiter_pnl_total {float(engine_stats.get('total_pnl', 0))}")

        lines.append("# HELP arbiter_kill_switch_armed Kill-switch state (1=armed, 0=disarmed)")
        lines.append("# TYPE arbiter_kill_switch_armed gauge")
        lines.append(f"arbiter_kill_switch_armed {safety_armed}")

        # Per-collector circuit state + rate-limiter tokens
        circuit_map = {"closed": 0, "half_open": 1, "open": 2}
        for platform, collector in (getattr(self, "collectors", {}) or {}).items():
            circuit = getattr(collector, "circuit", None)
            if circuit is not None:
                state_name = str(getattr(circuit, "state", "closed")).lower()
                circuit_val = circuit_map.get(state_name, 0)
                lines.append("# HELP arbiter_circuit_state Circuit state (0=closed,1=half_open,2=open)")
                lines.append("# TYPE arbiter_circuit_state gauge")
                lines.append(f'arbiter_circuit_state{{platform="{platform}"}} {circuit_val}')
            limiter = getattr(collector, "rate_limiter", None)
            if limiter is not None:
                available = int(getattr(limiter, "available_tokens", 0) or 0)
                penalty = float(getattr(limiter, "remaining_penalty_seconds", 0) or 0.0)
                lines.append("# HELP arbiter_rate_limiter_tokens Available rate-limit tokens")
                lines.append("# TYPE arbiter_rate_limiter_tokens gauge")
                lines.append(f'arbiter_rate_limiter_tokens{{platform="{platform}"}} {available}')
                lines.append("# HELP arbiter_rate_limiter_penalty_seconds Cooldown remaining")
                lines.append("# TYPE arbiter_rate_limiter_penalty_seconds gauge")
                lines.append(
                    f'arbiter_rate_limiter_penalty_seconds{{platform="{platform}"}} {penalty}'
                )

        # AutoExecutor stats (Plan 06-01) — attached to arbiter.main, may not be exposed yet
        ae = getattr(self, "auto_executor", None)
        if ae is not None and getattr(ae, "stats", None) is not None:
            s = ae.stats
            lines.append("# HELP arbiter_auto_executor_considered Opportunities seen by auto-executor")
            lines.append("# TYPE arbiter_auto_executor_considered counter")
            lines.append(f"arbiter_auto_executor_considered {s.considered}")
            lines.append("# HELP arbiter_auto_executor_executed Opportunities auto-executed")
            lines.append("# TYPE arbiter_auto_executor_executed counter")
            lines.append(f"arbiter_auto_executor_executed {s.executed}")
            lines.append("# HELP arbiter_auto_executor_skipped Opportunities skipped by policy gate")
            lines.append("# TYPE arbiter_auto_executor_skipped counter")
            for reason, count in (
                ("disabled", s.skipped_disabled),
                ("armed", s.skipped_armed),
                ("requires_manual", s.skipped_requires_manual),
                ("not_allowed", s.skipped_not_allowed),
                ("duplicate", s.skipped_duplicate),
                ("over_cap", s.skipped_over_cap),
                ("bootstrap_full", s.skipped_bootstrap_full),
            ):
                lines.append(
                    f'arbiter_auto_executor_skipped{{reason="{reason}"}} {count}'
                )

        # ── Task 18: 9 new Polymarket US / ops metrics ─────────────────────────
        # These are registered here (even at zero) so Prometheus scrape configs
        # can discover them immediately on startup without waiting for an event.

        lines.append("# HELP polymarket_us_rest_latency_p99_ms P99 REST latency to Polymarket US API (ms)")
        lines.append("# TYPE polymarket_us_rest_latency_p99_ms gauge")
        pm_us = getattr(self, "_pm_us_metrics", {})
        lines.append(
            f"polymarket_us_rest_latency_p99_ms {float(pm_us.get('rest_latency_p99_ms', 0.0))}"
        )

        lines.append("# HELP polymarket_us_ws_reconnects_total Total WebSocket reconnects to Polymarket US")
        lines.append("# TYPE polymarket_us_ws_reconnects_total counter")
        lines.append(
            f"polymarket_us_ws_reconnects_total {int(pm_us.get('ws_reconnects_total', 0))}"
        )

        lines.append("# HELP matched_pair_stream_events_total Total stream events processed by the pair matcher")
        lines.append("# TYPE matched_pair_stream_events_total counter")
        lines.append(
            f"matched_pair_stream_events_total {int(pm_us.get('matched_pair_stream_events_total', 0))}"
        )

        lines.append("# HELP matcher_backpressure_drops_total Events dropped due to queue backpressure")
        lines.append("# TYPE matcher_backpressure_drops_total counter")
        lines.append(
            f"matcher_backpressure_drops_total {int(pm_us.get('matcher_backpressure_drops_total', 0))}"
        )

        lines.append("# HELP matched_pair_latency_seconds End-to-end latency for a matched pair decision")
        lines.append("# TYPE matched_pair_latency_seconds histogram")
        for bucket_le, count in pm_us.get("matched_pair_latency_buckets", {
            "0.005": 0, "0.01": 0, "0.025": 0, "0.05": 0, "0.1": 0, "+Inf": 0,
        }).items():
            lines.append(
                f'matched_pair_latency_seconds_bucket{{le="{bucket_le}"}} {int(count)}'
            )
        lines.append(
            f'matched_pair_latency_seconds_count {int(pm_us.get("matched_pair_latency_count", 0))}'
        )
        lines.append(
            f'matched_pair_latency_seconds_sum {float(pm_us.get("matched_pair_latency_sum", 0.0))}'
        )

        lines.append("# HELP auto_discovery_candidates_pending Candidates awaiting auto-discovery review")
        lines.append("# TYPE auto_discovery_candidates_pending gauge")
        lines.append(
            f"auto_discovery_candidates_pending {int(pm_us.get('auto_discovery_candidates_pending', 0))}"
        )

        lines.append("# HELP auto_promote_rejections_total Auto-promote gate rejections by reason")
        lines.append("# TYPE auto_promote_rejections_total counter")
        for reason, count in pm_us.get("auto_promote_rejections", {
            "llm_disagree": 0, "low_score": 0, "missing_fields": 0,
        }).items():
            lines.append(
                f'auto_promote_rejections_total{{reason="{reason}"}} {int(count)}'
            )

        lines.append("# HELP ed25519_sign_failures_total Ed25519 signing failures")
        lines.append("# TYPE ed25519_sign_failures_total counter")
        lines.append(
            f"ed25519_sign_failures_total {int(pm_us.get('ed25519_sign_failures_total', 0))}"
        )

        lines.append("# HELP ws_subscription_count Active WebSocket subscriptions by platform")
        lines.append("# TYPE ws_subscription_count gauge")
        for platform, count in pm_us.get("ws_subscription_count", {
            "polymarket_us": 0, "kalshi": 0,
        }).items():
            lines.append(
                f'ws_subscription_count{{platform="{platform}"}} {int(count)}'
            )

        body = "\n".join(lines) + "\n"
        return web.Response(
            text=body,
            content_type="text/plain",
            charset="utf-8",
            headers={"Cache-Control": "no-store"},
        )

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
                        # Generic incident broadcast (all severities).
                        await self._broadcast_json({"type": "incident", "payload": result.to_dict()})
                        # Plan 03-03 (SAFE-03): when the incident carries a
                        # one_leg_exposure event_type, re-emit as a dedicated
                        # WebSocket event so the dashboard can render a
                        # hero-level banner without scanning incident metadata.
                        if (
                            isinstance(result.metadata, dict)
                            and result.metadata.get("event_type") == "one_leg_exposure"
                        ):
                            await self._broadcast_json(
                                {"type": "one_leg_exposure", "payload": result.to_dict()}
                            )
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

    async def _rate_limit_broadcast_loop(self):
        """SAFE-04: Emit ``rate_limit_state`` WS events every 2 seconds.

        Payload shape::

            {"type": "rate_limit_state",
             "payload": {platform_name: RateLimiter.stats dict}}

        Only platforms whose adapter carries a ``rate_limiter`` attribute are
        included. Cancelled cleanly on shutdown.
        """
        while True:
            try:
                await asyncio.sleep(2.0)
                if not self._ws_clients:
                    continue
                adapters = getattr(self.engine, "adapters", {}) or {}
                snapshot: dict = {}
                for platform, adapter in adapters.items():
                    rl = getattr(adapter, "rate_limiter", None)
                    if rl is None:
                        continue
                    try:
                        snapshot[platform] = rl.stats
                    except Exception as stats_exc:
                        logger.debug(
                            "rate_limit_broadcast stats failure for %s: %s",
                            platform, stats_exc,
                        )
                if snapshot:
                    await self._broadcast_json(
                        {"type": "rate_limit_state", "payload": snapshot}
                    )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error("rate_limit_broadcast error: %s", exc)
                await asyncio.sleep(1.0)

    async def _broadcast_json(self, payload: dict):
        for client in list(self._ws_clients):
            try:
                await client.send_json(payload)
            except Exception:
                if client in self._ws_clients:
                    self._ws_clients.remove(client)

    def _restore_operator_settings(self) -> None:
        payload = self._operator_settings_store.load()
        settings = payload.get("settings") if isinstance(payload, dict) else None
        if not isinstance(settings, dict) or not settings:
            return
        try:
            patch = self._normalize_settings_patch(settings)
            self._apply_operator_settings_patch(patch)
        except ValueError as exc:
            logger.warning("Ignoring invalid persisted operator settings: %s", exc)
            return
        self._operator_settings_meta = {
            "persisted": True,
            "updated_at": payload.get("updated_at"),
            "updated_by": payload.get("updated_by") or "system",
        }

    def _editable_settings_payload(self) -> dict:
        auto_executor = getattr(self, "auto_executor", None)
        return {
            "scanner": {
                "min_edge_cents": float(self.config.scanner.min_edge_cents),
                "confidence_threshold": float(self.config.scanner.confidence_threshold),
                "max_position_usd": float(self.config.scanner.max_position_usd),
                "scan_interval": float(self.config.scanner.scan_interval),
                "max_quote_age_seconds": float(self.config.scanner.max_quote_age_seconds),
                "min_liquidity": float(self.config.scanner.min_liquidity),
                "slippage_tolerance": float(self.config.scanner.slippage_tolerance),
                "persistence_scans": int(self.config.scanner.persistence_scans),
            },
            "alerts": {
                "kalshi_low": float(self.config.alerts.kalshi_low),
                "polymarket_low": float(self.config.alerts.polymarket_low),
                "cooldown": float(self.config.alerts.cooldown),
            },
            "auto_executor": {
                "enabled": bool(getattr(getattr(auto_executor, "_config", None), "enabled", False)),
                "max_position_usd": float(
                    getattr(getattr(auto_executor, "_config", None), "max_position_usd", self.config.scanner.max_position_usd)
                ),
                "bootstrap_trades": getattr(getattr(auto_executor, "_config", None), "bootstrap_trades", None),
            },
            "mapping": {
                "auto_discovery_enabled": bool(self._market_discovery_settings.get("auto_discovery_enabled", True)),
                "auto_discovery_interval_seconds": float(self._market_discovery_settings.get("auto_discovery_interval_seconds", 300.0)),
                "auto_discovery_budget_rps": float(self._market_discovery_settings.get("auto_discovery_budget_rps", 2.0)),
                "auto_discovery_min_score": float(self._market_discovery_settings.get("auto_discovery_min_score", 0.25)),
                "auto_discovery_max_candidates": int(self._market_discovery_settings.get("auto_discovery_max_candidates", 500)),
                "auto_promote_enabled": bool(self._market_discovery_settings.get("auto_promote_enabled", False)),
                "auto_promote_min_score": float(self._market_discovery_settings.get("auto_promote_min_score", 0.78)),
                "auto_promote_daily_cap": int(self._market_discovery_settings.get("auto_promote_daily_cap", 250)),
                "auto_promote_advisory_scans": int(self._market_discovery_settings.get("auto_promote_advisory_scans", 0)),
                "auto_promote_max_days": int(self._market_discovery_settings.get("auto_promote_max_days", 400)),
            },
        }

    def _settings_snapshot(self) -> dict:
        editable = self._editable_settings_payload()
        auto_executor = editable["auto_executor"]
        return {
            "mode": {
                "dry_run": bool(self.config.scanner.dry_run),
                "label": "Dry run" if self.config.scanner.dry_run else "Live trading",
                "live_switch_editable": False,
                "live_switch_note": "Trading mode still flips via CLI plus preflight, not the dashboard.",
            },
            "scanner": editable["scanner"],
            "alerts": editable["alerts"],
            "auto_executor": auto_executor,
            "mapping": editable["mapping"],
            "meta": {
                "persisted": bool(self._operator_settings_meta.get("persisted")),
                "updated_at": self._operator_settings_meta.get("updated_at"),
                "updated_by": self._operator_settings_meta.get("updated_by"),
                "storage_label": "operator runtime store",
            },
        }

    @staticmethod
    def _coerce_bool(value, *, label: str) -> bool:
        if isinstance(value, bool):
            return value
        raise ValueError(f"{label} must be true or false")

    @staticmethod
    def _coerce_float(value, *, label: str, minimum: float | None = None, maximum: float | None = None) -> float:
        try:
            number = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{label} must be a number") from exc
        if minimum is not None and number < minimum:
            raise ValueError(f"{label} must be >= {minimum}")
        if maximum is not None and number > maximum:
            raise ValueError(f"{label} must be <= {maximum}")
        return number

    @staticmethod
    def _coerce_int(value, *, label: str, minimum: int | None = None, maximum: int | None = None) -> int:
        try:
            number = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{label} must be an integer") from exc
        if minimum is not None and number < minimum:
            raise ValueError(f"{label} must be >= {minimum}")
        if maximum is not None and number > maximum:
            raise ValueError(f"{label} must be <= {maximum}")
        return number

    def _normalize_settings_patch(self, payload: dict) -> dict:
        if not isinstance(payload, dict):
            raise ValueError("settings payload must be an object")

        patch: dict = {}

        scanner = payload.get("scanner")
        if scanner is not None:
            if not isinstance(scanner, dict):
                raise ValueError("scanner settings must be an object")
            scanner_patch = {}
            if "min_edge_cents" in scanner:
                scanner_patch["min_edge_cents"] = self._coerce_float(scanner["min_edge_cents"], label="min_edge_cents", minimum=0.1, maximum=100.0)
            if "confidence_threshold" in scanner:
                scanner_patch["confidence_threshold"] = self._coerce_float(scanner["confidence_threshold"], label="confidence_threshold", minimum=0.0, maximum=1.0)
            if "max_position_usd" in scanner:
                scanner_patch["max_position_usd"] = self._coerce_float(scanner["max_position_usd"], label="max_position_usd", minimum=1.0, maximum=100000.0)
            if "scan_interval" in scanner:
                scanner_patch["scan_interval"] = self._coerce_float(scanner["scan_interval"], label="scan_interval", minimum=0.1, maximum=60.0)
            if "max_quote_age_seconds" in scanner:
                scanner_patch["max_quote_age_seconds"] = self._coerce_float(scanner["max_quote_age_seconds"], label="max_quote_age_seconds", minimum=1.0, maximum=300.0)
            if "min_liquidity" in scanner:
                scanner_patch["min_liquidity"] = self._coerce_float(scanner["min_liquidity"], label="min_liquidity", minimum=0.0, maximum=1000000.0)
            if "slippage_tolerance" in scanner:
                scanner_patch["slippage_tolerance"] = self._coerce_float(scanner["slippage_tolerance"], label="slippage_tolerance", minimum=0.0, maximum=1.0)
            if "persistence_scans" in scanner:
                scanner_patch["persistence_scans"] = self._coerce_int(scanner["persistence_scans"], label="persistence_scans", minimum=1, maximum=20)
            if scanner_patch:
                patch["scanner"] = scanner_patch

        alerts = payload.get("alerts")
        if alerts is not None:
            if not isinstance(alerts, dict):
                raise ValueError("alerts settings must be an object")
            alerts_patch = {}
            if "kalshi_low" in alerts:
                alerts_patch["kalshi_low"] = self._coerce_float(alerts["kalshi_low"], label="kalshi_low", minimum=0.0, maximum=1000000.0)
            if "polymarket_low" in alerts:
                alerts_patch["polymarket_low"] = self._coerce_float(alerts["polymarket_low"], label="polymarket_low", minimum=0.0, maximum=1000000.0)
            if "cooldown" in alerts:
                alerts_patch["cooldown"] = self._coerce_float(alerts["cooldown"], label="cooldown", minimum=0.0, maximum=86400.0)
            if alerts_patch:
                patch["alerts"] = alerts_patch

        auto_executor = payload.get("auto_executor")
        if auto_executor is not None:
            if not isinstance(auto_executor, dict):
                raise ValueError("auto_executor settings must be an object")
            auto_patch = {}
            if "enabled" in auto_executor:
                auto_patch["enabled"] = self._coerce_bool(auto_executor["enabled"], label="auto_executor.enabled")
            if "max_position_usd" in auto_executor:
                auto_patch["max_position_usd"] = self._coerce_float(auto_executor["max_position_usd"], label="auto_executor.max_position_usd", minimum=1.0, maximum=100000.0)
            if auto_patch:
                patch["auto_executor"] = auto_patch

        mapping = payload.get("mapping")
        if mapping is not None:
            if not isinstance(mapping, dict):
                raise ValueError("mapping settings must be an object")
            mapping_patch = {}
            if "auto_discovery_enabled" in mapping:
                mapping_patch["auto_discovery_enabled"] = self._coerce_bool(mapping["auto_discovery_enabled"], label="mapping.auto_discovery_enabled")
            if "auto_discovery_interval_seconds" in mapping:
                mapping_patch["auto_discovery_interval_seconds"] = self._coerce_float(mapping["auto_discovery_interval_seconds"], label="mapping.auto_discovery_interval_seconds", minimum=15.0, maximum=3600.0)
            if "auto_discovery_budget_rps" in mapping:
                mapping_patch["auto_discovery_budget_rps"] = self._coerce_float(mapping["auto_discovery_budget_rps"], label="mapping.auto_discovery_budget_rps", minimum=0.1, maximum=20.0)
            if "auto_discovery_min_score" in mapping:
                mapping_patch["auto_discovery_min_score"] = self._coerce_float(mapping["auto_discovery_min_score"], label="mapping.auto_discovery_min_score", minimum=0.0, maximum=1.0)
            if "auto_discovery_max_candidates" in mapping:
                mapping_patch["auto_discovery_max_candidates"] = self._coerce_int(mapping["auto_discovery_max_candidates"], label="mapping.auto_discovery_max_candidates", minimum=1, maximum=5000)
            if "auto_promote_enabled" in mapping:
                mapping_patch["auto_promote_enabled"] = self._coerce_bool(mapping["auto_promote_enabled"], label="mapping.auto_promote_enabled")
            if "auto_promote_min_score" in mapping:
                mapping_patch["auto_promote_min_score"] = self._coerce_float(mapping["auto_promote_min_score"], label="mapping.auto_promote_min_score", minimum=0.0, maximum=1.0)
            if "auto_promote_daily_cap" in mapping:
                mapping_patch["auto_promote_daily_cap"] = self._coerce_int(mapping["auto_promote_daily_cap"], label="mapping.auto_promote_daily_cap", minimum=1, maximum=5000)
            if "auto_promote_advisory_scans" in mapping:
                mapping_patch["auto_promote_advisory_scans"] = self._coerce_int(mapping["auto_promote_advisory_scans"], label="mapping.auto_promote_advisory_scans", minimum=0, maximum=5000)
            if "auto_promote_max_days" in mapping:
                mapping_patch["auto_promote_max_days"] = self._coerce_int(mapping["auto_promote_max_days"], label="mapping.auto_promote_max_days", minimum=1, maximum=2000)
            if mapping_patch:
                patch["mapping"] = mapping_patch

        return patch

    def _apply_operator_settings_patch(self, patch: dict) -> None:
        scanner_patch = patch.get("scanner") or {}
        for key, value in scanner_patch.items():
            setattr(self.config.scanner, key, value)

        alerts_patch = patch.get("alerts") or {}
        for key, value in alerts_patch.items():
            setattr(self.config.alerts, key, value)
        if alerts_patch:
            self.monitor._thresholds["kalshi"] = float(self.config.alerts.kalshi_low)
            self.monitor._thresholds["polymarket"] = float(self.config.alerts.polymarket_low)

        auto_patch = patch.get("auto_executor") or {}
        if auto_patch and getattr(self, "auto_executor", None) is not None:
            for key, value in auto_patch.items():
                setattr(self.auto_executor._config, key, value)

        mapping_patch = patch.get("mapping") or {}
        if mapping_patch:
            self._market_discovery_settings = {
                **self._market_discovery_settings,
                **mapping_patch,
            }

    def _update_operator_settings(self, payload: dict, *, actor: str) -> dict:
        patch = self._normalize_settings_patch(payload)
        if not patch:
            raise ValueError("No editable settings were provided")
        self._apply_operator_settings_patch(patch)
        persisted_payload = self._operator_settings_store.save(self._editable_settings_payload(), updated_by=actor)
        self._operator_settings_meta = {
            "persisted": True,
            "updated_at": persisted_payload.get("updated_at"),
            "updated_by": persisted_payload.get("updated_by") or actor,
        }
        return self._settings_snapshot()

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
            "settings": self._settings_snapshot(),
            "collectors": self._collector_snapshot(),
            "balances": balances,
            "safety": (
                self.safety._state.to_dict()
                if self.safety is not None
                else {"armed": False, "available": False}
            ),
            # SAFE-04: per-adapter RateLimiter.stats so GET /api/system and the
            # WebSocket bootstrap carry the same rate-limit payload as the
            # periodic rate_limit_state events.
            "rate_limits": {
                platform: adapter.rate_limiter.stats
                for platform, adapter in (
                    getattr(self.engine, "adapters", {}) or {}
                ).items()
                if getattr(adapter, "rate_limiter", None) is not None
            },
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

    def _service_health_snapshot(self) -> dict:
        return {
            "status": "ok",
            "probe": "liveness",
            "mode": "dry-run" if self.config.scanner.dry_run else "live",
            "uptime_seconds": round(time.time() - self.started_at, 1),
        }

    def _service_ready_snapshot(self) -> dict:
        readiness = self._readiness_snapshot()
        return {
            "status": "ready",
            "probe": "service_readiness",
            "ready": True,
            "mode": "dry-run" if self.config.scanner.dry_run else "live",
            "uptime_seconds": round(time.time() - self.started_at, 1),
            "live_trading_ready": readiness.get("ready_for_live_trading", False),
            "live_trading_endpoint": "/api/readiness",
        }

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
    mapping_store=None,
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
        mapping_store=mapping_store,
    )
