"""
ARBITER — FastAPI server for the dashboard.
Provides REST + WebSocket endpoints for the ARBITER dashboard to consume.
"""
import asyncio
import json
import logging
import time
from typing import Optional

from .config.settings import ArbiterConfig, MARKET_MAP
from .utils.price_store import PriceStore
from .scanner.arbitrage import ArbitrageScanner
from .execution.engine import ExecutionEngine
from .monitor.balance import BalanceMonitor

logger = logging.getLogger("arbiter.api")


class ArbiterAPI:
    """
    Lightweight async HTTP server for the ARBITER dashboard.
    Uses aiohttp to avoid heavy framework dependencies.
    """

    def __init__(self, price_store: PriceStore, scanner: ArbitrageScanner,
                 engine: ExecutionEngine, monitor: BalanceMonitor,
                 config: ArbiterConfig, host: str = "0.0.0.0", port: int = 8080):
        self.store = price_store
        self.scanner = scanner
        self.engine = engine
        self.monitor = monitor
        self.config = config
        self.host = host
        self.port = port
        self._ws_clients = []

    async def serve(self):
        """Start the API server."""
        import aiohttp.web as web

        app = web.Application()
        app.router.add_get("/api/health", self.handle_health)
        app.router.add_get("/api/prices", self.handle_prices)
        app.router.add_get("/api/opportunities", self.handle_opportunities)
        app.router.add_get("/api/balances", self.handle_balances)
        app.router.add_get("/api/executions", self.handle_executions)
        app.router.add_get("/api/stats", self.handle_stats)
        app.router.add_get("/api/markets", self.handle_markets)
        app.router.add_get("/ws", self.handle_websocket)

        # CORS middleware
        @web.middleware
        async def cors_middleware(request, handler):
            response = await handler(request)
            response.headers["Access-Control-Allow-Origin"] = "*"
            response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
            response.headers["Access-Control-Allow-Headers"] = "Content-Type"
            return response

        app.middlewares.append(cors_middleware)

        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, self.host, self.port)
        await site.start()

        logger.info(f"API server running at http://{self.host}:{self.port}")
        logger.info(f"Dashboard WebSocket at ws://{self.host}:{self.port}/ws")

        # Start WebSocket broadcast loop
        asyncio.create_task(self._broadcast_loop())

        # Keep alive
        while True:
            await asyncio.sleep(3600)

    async def handle_health(self, request):
        """Health check endpoint."""
        import aiohttp.web as web
        return web.json_response({
            "status": "ok",
            "uptime": time.time(),
            "scanner": self.scanner.stats,
            "execution": self.engine.stats,
        })

    async def handle_prices(self, request):
        """Get all current prices across platforms."""
        import aiohttp.web as web
        all_prices = await self.store.get_all_prices()
        result = {}
        for key, price in all_prices.items():
            result[key] = price.to_dict()
        return web.json_response(result)

    async def handle_opportunities(self, request):
        """Get current arbitrage opportunities."""
        import aiohttp.web as web
        opps = self.scanner.current_opportunities
        return web.json_response([o.to_dict() for o in opps])

    async def handle_balances(self, request):
        """Get current balances."""
        import aiohttp.web as web
        balances = self.monitor.current_balances
        result = {}
        for platform, snap in balances.items():
            result[platform] = {
                "balance": snap.balance,
                "is_low": snap.is_low,
                "timestamp": snap.timestamp,
            }
        return web.json_response(result)

    async def handle_executions(self, request):
        """Get execution history."""
        import aiohttp.web as web
        execs = self.engine.execution_history[-50:]  # last 50
        result = []
        for ex in execs:
            result.append({
                "arb_id": ex.arb_id,
                "canonical_id": ex.opportunity.canonical_id,
                "status": ex.status,
                "pnl": ex.realized_pnl,
                "timestamp": ex.timestamp,
                "yes_platform": ex.opportunity.yes_platform,
                "no_platform": ex.opportunity.no_platform,
            })
        return web.json_response(result)

    async def handle_stats(self, request):
        """Get system statistics."""
        import aiohttp.web as web
        return web.json_response({
            "scanner": self.scanner.stats,
            "execution": self.engine.stats,
            "balances": {
                "total": self.monitor.total_balance,
                "platforms": {
                    p: {"balance": s.balance, "is_low": s.is_low}
                    for p, s in self.monitor.current_balances.items()
                },
            },
            "config": {
                "dry_run": self.config.scanner.dry_run,
                "min_edge_cents": self.config.scanner.min_edge_cents,
                "max_position": self.config.scanner.max_position_usd,
                "scan_interval": self.config.scanner.scan_interval,
            },
        })

    async def handle_markets(self, request):
        """Get tracked market definitions."""
        import aiohttp.web as web
        return web.json_response(MARKET_MAP)

    async def handle_websocket(self, request):
        """WebSocket endpoint for real-time updates to dashboard."""
        import aiohttp.web as web
        ws = web.WebSocketResponse()
        await ws.prepare(request)

        self._ws_clients.append(ws)
        logger.info(f"Dashboard WebSocket connected ({len(self._ws_clients)} clients)")

        try:
            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    # Handle commands from dashboard
                    try:
                        cmd = json.loads(msg.data)
                        await self._handle_ws_command(ws, cmd)
                    except json.JSONDecodeError:
                        pass
                elif msg.type == aiohttp.WSMsgType.ERROR:
                    logger.error(f"WebSocket error: {ws.exception()}")
        finally:
            self._ws_clients.remove(ws)
            logger.info(f"Dashboard disconnected ({len(self._ws_clients)} clients)")

        return ws

    async def _handle_ws_command(self, ws, cmd: dict):
        """Handle commands from the dashboard WebSocket."""
        import aiohttp.web as web
        action = cmd.get("action")

        if action == "get_prices":
            all_prices = await self.store.get_all_prices()
            await ws.send_json({
                "type": "prices",
                "data": {k: v.to_dict() for k, v in all_prices.items()},
            })
        elif action == "get_opportunities":
            opps = self.scanner.current_opportunities
            await ws.send_json({
                "type": "opportunities",
                "data": [o.to_dict() for o in opps],
            })

    async def _broadcast_loop(self):
        """Broadcast updates to all connected WebSocket clients."""
        price_queue = self.store.subscribe()

        while True:
            try:
                price = await asyncio.wait_for(price_queue.get(), timeout=2.0)
                msg = json.dumps({
                    "type": "price_update",
                    "data": price.to_dict(),
                })
                for ws in list(self._ws_clients):
                    try:
                        await ws.send_str(msg)
                    except Exception:
                        pass

            except asyncio.TimeoutError:
                # Send periodic stats even when no price updates
                if self._ws_clients:
                    stats_msg = json.dumps({
                        "type": "stats",
                        "data": {
                            "scanner": self.scanner.stats,
                            "execution": self.engine.stats,
                        },
                    })
                    for ws in list(self._ws_clients):
                        try:
                            await ws.send_str(stats_msg)
                        except Exception:
                            pass
            except Exception as e:
                logger.error(f"Broadcast error: {e}")
                await asyncio.sleep(1)


def create_api_server(price_store, scanner, engine, monitor, config,
                       host="0.0.0.0", port=8080) -> ArbiterAPI:
    return ArbiterAPI(price_store, scanner, engine, monitor, config, host, port)
