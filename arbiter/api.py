"""
Canonical aiohttp server for the ARBITER dashboard and API.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Dict, Optional

from aiohttp import WSMsgType, web

from .config.settings import MARKET_MAP, ArbiterConfig, update_market_mapping
from .execution.engine import ArbExecution, ExecutionEngine, ExecutionIncident
from .monitor.balance import BalanceMonitor
from .scanner.arbitrage import ArbitrageOpportunity, ArbitrageScanner
from .utils.price_store import PricePoint, PriceStore

logger = logging.getLogger("arbiter.api")


class ArbiterAPI:
    def __init__(
        self,
        price_store: PriceStore,
        scanner: ArbitrageScanner,
        engine: ExecutionEngine,
        monitor: BalanceMonitor,
        config: ArbiterConfig,
        collectors: Optional[Dict[str, object]] = None,
        host: str = "0.0.0.0",
        port: int = 8080,
    ):
        self.store = price_store
        self.scanner = scanner
        self.engine = engine
        self.monitor = monitor
        self.config = config
        self.collectors = collectors or {}
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
            "collectors": self._collector_snapshot(),
            "balances": balances,
            "series": {
                "scanner": self.scanner.history,
                "equity": self.engine.equity_curve,
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


def create_api_server(price_store, scanner, engine, monitor, config, collectors=None, host="0.0.0.0", port=8080) -> ArbiterAPI:
    return ArbiterAPI(price_store, scanner, engine, monitor, config, collectors=collectors, host=host, port=port)
