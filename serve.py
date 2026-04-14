"""
ARBITER — Standalone Live Server
Serves both the API and the dashboard from a single process.
Designed to run behind a tunnel (ngrok/localtunnel) for remote access.

Usage: python -m arbiter.serve
"""
import asyncio
import json
import logging
import os
import sys
import time
import psutil
from collections import deque
from datetime import datetime

# Add parent to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import aiohttp
from aiohttp import web

from arbiter.config import ArbiterConfig, load_config, MARKET_MAP
from arbiter.config.settings import kalshi_fee, polymarket_fee, predictit_fee
from arbiter.utils.logger import setup_logging
from arbiter.utils.price_store import PriceStore, PricePoint
from arbiter.collectors.kalshi import KalshiCollector
from arbiter.collectors.polymarket import PolymarketCollector
from arbiter.collectors.predictit import PredictItCollector
from arbiter.scanner.arbitrage import ArbitrageScanner
from arbiter.monitor.balance import BalanceMonitor
from arbiter.execution.engine import ExecutionEngine

logger = logging.getLogger("arbiter.serve")

# ── Globals ────────────────────────────────────────
config: ArbiterConfig = None
price_store: PriceStore = None
scanner: ArbitrageScanner = None
engine: ExecutionEngine = None
monitor: BalanceMonitor = None
kalshi_col: KalshiCollector = None
poly_col: PolymarketCollector = None
pi_col: PredictItCollector = None
ws_clients = []
start_time = time.time()
dashboard_html = ""

# ── Error Tracking ─────────────────────────────────
error_log = deque(maxlen=200)  # Keep last 200 errors
error_log_lock = asyncio.Lock()


def log_error(source: str, message: str, level: str = "error"):
    """Log an error to the global error tracking system."""
    error_entry = {
        "timestamp": datetime.utcnow().isoformat(),
        "source": source,
        "message": message,
        "level": level,
    }
    error_log.append(error_entry)
    logger.log(
        getattr(logging, level.upper(), logging.ERROR),
        f"[{source}] {message}"
    )


async def start_background_tasks(app):
    """Start all collector and scanner tasks."""
    global config, price_store, scanner, engine, monitor
    global kalshi_col, poly_col, pi_col, dashboard_html

    config = load_config()
    price_store = PriceStore(ttl=60)

    # Collectors
    kalshi_col = KalshiCollector(config.kalshi, price_store)
    poly_col = PolymarketCollector(config.polymarket, price_store)
    pi_col = PredictItCollector(config.predictit, price_store)

    # Scanner
    scanner = ArbitrageScanner(config.scanner, price_store)

    # Monitor & Execution
    collectors_dict = {"kalshi": kalshi_col, "polymarket": poly_col, "predictit": pi_col}
    monitor = BalanceMonitor(config.alerts, collectors_dict)
    engine = ExecutionEngine(config, monitor, collectors=collectors_dict)

    # Load dashboard HTML
    dash_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                              "arbitrage_dashboard.html")
    if os.path.exists(dash_path):
        with open(dash_path, "r") as f:
            dashboard_html = f.read()
        logger.info(f"Dashboard loaded from {dash_path}")
    else:
        logger.warning(f"Dashboard not found at {dash_path}")

    # Subscribe execution engine to arb queue
    arb_queue = scanner.subscribe()

    # Launch background tasks
    app['bg_tasks'] = []
    for name, coro in [
        ("kalshi", kalshi_col.run()),
        ("polymarket", poly_col.run()),
        ("predictit", pi_col.run()),
        ("scanner", scanner.run()),
        ("execution", engine.run(arb_queue)),
    ]:
        task = asyncio.create_task(coro)
        app['bg_tasks'].append(task)
        logger.info(f"Started {name} background task")

    # Start WebSocket broadcast
    app['bg_tasks'].append(asyncio.create_task(broadcast_loop()))

    logger.info("=" * 55)
    logger.info("  ARBITER LIVE — all systems running")
    logger.info(f"  Markets: {len(MARKET_MAP)}")
    logger.info(f"  Mode: {'DRY RUN' if config.scanner.dry_run else 'LIVE'}")
    logger.info(f"  Kalshi auth: {'yes' if kalshi_col.auth.is_authenticated else 'no'}")
    logger.info("=" * 55)


async def cleanup_background_tasks(app):
    """Cancel background tasks on shutdown."""
    for task in app.get('bg_tasks', []):
        task.cancel()
    await asyncio.gather(*app.get('bg_tasks', []), return_exceptions=True)
    logger.info("All background tasks stopped")


# ── API Handlers ───────────────────────────────────

async def handle_dashboard(request):
    """Serve the dashboard HTML with API URL injected."""
    if not dashboard_html:
        return web.Response(text="Dashboard not found", status=404)

    # Detect the public URL from request headers (tunnel sets X-Forwarded-Host or Host)
    host = request.headers.get('X-Forwarded-Host') or request.headers.get('Host') or 'localhost:8080'
    proto = request.headers.get('X-Forwarded-Proto') or 'http'
    base_url = f"{proto}://{host}"
    ws_proto = 'wss' if proto == 'https' else 'ws'
    ws_url = f"{ws_proto}://{host}/ws"

    # Inject the correct URLs into the dashboard
    # Handle both old-style and new configurable backend format
    patched = dashboard_html.replace(
        "let API = paramBackend || savedBackend || 'http://localhost:8080';",
        f"let API = '{base_url}';"
    ).replace(
        "let WS_URL = API.replace('http', 'ws') + '/ws';",
        f"let WS_URL = '{ws_url}';"
    )
    # Fallback for old format
    patched = patched.replace(
        "const API = 'http://localhost:8080'",
        f"const API = '{base_url}'"
    ).replace(
        "const WS_URL = 'ws://localhost:8080/ws'",
        f"const WS_URL = '{ws_url}'"
    )
    return web.Response(text=patched, content_type='text/html')


async def handle_health(request):
    return web.json_response({
        "status": "ok",
        "uptime_s": round(time.time() - start_time, 1),
        "scanner": scanner.stats if scanner else {},
        "execution": engine.stats if engine else {},
    })


async def handle_prices(request):
    all_p = await price_store.get_all_prices()
    return web.json_response({k: v.to_dict() for k, v in all_p.items()})


async def handle_opportunities(request):
    opps = scanner.current_opportunities if scanner else []
    return web.json_response([o.to_dict() for o in opps])


async def handle_balances(request):
    if not monitor:
        return web.json_response({})
    bals = monitor.current_balances
    return web.json_response({
        p: {"balance": s.balance, "is_low": s.is_low, "timestamp": s.timestamp}
        for p, s in bals.items()
    })


async def handle_executions(request):
    if not engine:
        return web.json_response([])
    execs = engine.execution_history[-50:]
    return web.json_response([{
        "arb_id": e.arb_id,
        "canonical_id": e.opportunity.canonical_id,
        "status": e.status,
        "pnl": e.realized_pnl,
        "timestamp": e.timestamp,
    } for e in execs])


async def handle_stats(request):
    """Enhanced stats endpoint with granular collector information."""
    uptime = time.time() - start_time

    def _safe_state(cb):
        s = getattr(cb, 'state', 'unknown')
        return s.value if hasattr(s, 'value') else str(s)

    def _safe_stats(cb):
        try:
            raw = getattr(cb, 'stats', {})
            return {k: (v.value if hasattr(v, 'value') else v) for k, v in raw.items()}
        except Exception:
            return {}

    # Collector health snapshots
    collectors_detail = {}
    if kalshi_col:
        collectors_detail["kalshi"] = {
            "circuit_state": _safe_state(kalshi_col.circuit),
            "circuit_stats": _safe_stats(kalshi_col.circuit),
            "error_count": getattr(kalshi_col, 'total_errors', 0),
            "fetch_count": getattr(kalshi_col, 'total_fetches', 0),
            "authenticated": kalshi_col.auth.is_authenticated if hasattr(kalshi_col, 'auth') else False,
        }
    if poly_col:
        collectors_detail["polymarket"] = {
            "gamma_circuit_state": _safe_state(poly_col.circuit_gamma),
            "clob_circuit_state": _safe_state(poly_col.circuit_clob),
            "gamma_stats": _safe_stats(poly_col.circuit_gamma),
            "clob_stats": _safe_stats(poly_col.circuit_clob),
            "error_count": getattr(poly_col, 'total_errors', 0),
            "fetch_count": getattr(poly_col, 'total_fetches', 0),
        }
    if pi_col:
        collectors_detail["predictit"] = {
            "circuit_state": _safe_state(pi_col.circuit),
            "circuit_stats": _safe_stats(pi_col.circuit),
            "error_count": getattr(pi_col, 'total_errors', 0),
            "fetch_count": getattr(pi_col, 'total_fetches', 0),
        }

    return web.json_response({
        "uptime_s": round(uptime, 1),
        "scanner": scanner.stats if scanner else {},
        "scanner_detail": {
            "total_scans": getattr(scanner, 'scan_count', 0) if scanner else 0,
            "avg_scan_time": getattr(scanner, 'avg_scan_time', 0) if scanner else 0,
            "opportunities_found": len(scanner.current_opportunities) if scanner else 0,
        } if scanner else {},
        "execution": engine.stats if engine else {},
        "execution_detail": {
            "dry_run_mode": config.scanner.dry_run if config else True,
            "total_trades_attempted": len(engine.execution_history) if engine else 0,
            "total_trades_executed": sum(1 for e in engine.execution_history if e.status == "executed") if engine else 0,
        } if engine else {},
        "collectors": collectors_detail,
        "config": {
            "dry_run": config.scanner.dry_run if config else True,
            "min_edge_cents": config.scanner.min_edge_cents if config else 2.0,
            "scan_interval": config.scanner.scan_interval if config else 1.0,
        },
        "error_log_size": len(error_log),
    })


async def handle_markets(request):
    return web.json_response(MARKET_MAP)


async def handle_system(request):
    """Comprehensive system state endpoint."""
    uptime = time.time() - start_time
    try:
        process = psutil.Process()
        memory_info = process.memory_info()
        mem_rss = round(memory_info.rss / 1024 / 1024, 2)
        mem_vms = round(memory_info.vms / 1024 / 1024, 2)
        cpu_pct = process.cpu_percent(interval=0.1)
    except Exception:
        mem_rss = mem_vms = cpu_pct = 0

    def _cb_state(cb):
        """Safely extract circuit breaker state as string."""
        try:
            s = getattr(cb, 'state', None)
            if s is None:
                return 'unknown'
            return s.value if hasattr(s, 'value') else str(s)
        except Exception:
            return 'unknown'

    def _cb_stats(cb):
        """Safely extract circuit breaker stats as dict."""
        try:
            raw = getattr(cb, 'stats', {})
            # Convert any enum values to strings
            return {k: (v.value if hasattr(v, 'value') else v) for k, v in raw.items()}
        except Exception:
            return {}

    # Collector health for each platform
    collectors_health = {}
    if kalshi_col:
        collectors_health["kalshi"] = {
            "circuit_breaker_state": _cb_state(kalshi_col.circuit),
            "circuit_stats": _cb_stats(kalshi_col.circuit),
            "error_count": getattr(kalshi_col, 'total_errors', 0),
            "fetch_count": getattr(kalshi_col, 'total_fetches', 0),
            "consecutive_errors": getattr(kalshi_col, 'consecutive_errors', 0),
            "authenticated": kalshi_col.auth.is_authenticated if hasattr(kalshi_col, 'auth') else False,
        }
    if poly_col:
        collectors_health["polymarket"] = {
            "gamma_circuit_state": _cb_state(poly_col.circuit_gamma),
            "clob_circuit_state": _cb_state(poly_col.circuit_clob),
            "gamma_stats": _cb_stats(poly_col.circuit_gamma),
            "clob_stats": _cb_stats(poly_col.circuit_clob),
            "error_count": getattr(poly_col, 'total_errors', 0),
            "fetch_count": getattr(poly_col, 'total_fetches', 0),
        }
    if pi_col:
        collectors_health["predictit"] = {
            "circuit_breaker_state": _cb_state(pi_col.circuit),
            "circuit_stats": _cb_stats(pi_col.circuit),
            "error_count": getattr(pi_col, 'total_errors', 0),
            "fetch_count": getattr(pi_col, 'total_fetches', 0),
        }

    # Scanner state
    scanner_state = {
        "total_scans": getattr(scanner, 'scan_count', 0),
        "avg_scan_time_ms": round(getattr(scanner, 'avg_scan_time', 0) * 1000, 2),
        "opportunities_found": len(scanner.current_opportunities) if scanner else 0,
    } if scanner else {}

    # Execution engine state
    execution_state = {
        "dry_run_mode": config.scanner.dry_run if config else True,
        "total_trades_attempted": len(engine.execution_history) if engine else 0,
        "total_trades_executed": sum(1 for e in engine.execution_history if e.status == "executed") if engine else 0,
        "trade_history_size": len(engine.execution_history) if engine else 0,
    } if engine else {}

    return web.json_response({
        "timestamp": datetime.utcnow().isoformat(),
        "uptime_seconds": round(uptime, 1),
        "collectors_health": collectors_health,
        "scanner_state": scanner_state,
        "execution_engine_state": execution_state,
        "error_log": {
            "count": len(error_log),
            "max_size": error_log.maxlen,
            "recent_errors": list(error_log)[-10:] if error_log else [],
        },
        "system_resources": {
            "memory_rss_mb": mem_rss,
            "memory_vms_mb": mem_vms,
            "cpu_percent": cpu_pct,
        },
        "websocket_clients": len(ws_clients),
    })


async def handle_errors(request):
    """Recent errors from all collectors."""
    return web.json_response({
        "timestamp": datetime.utcnow().isoformat(),
        "error_count": len(error_log),
        "errors": list(error_log),
    })


async def handle_trades(request):
    """Trade execution history with full details."""
    if not engine:
        return web.json_response({"trades": []})

    trades = []
    for exec_record in engine.execution_history:
        trade_dict = {
            "arb_id": exec_record.arb_id,
            "canonical_id": exec_record.opportunity.canonical_id,
            "status": exec_record.status,
            "realized_pnl": exec_record.realized_pnl,
            "timestamp": exec_record.timestamp,
            "platform_1": getattr(exec_record.opportunity, 'platform_1', None),
            "platform_2": getattr(exec_record.opportunity, 'platform_2', None),
            "market_id": getattr(exec_record.opportunity, 'market_id', None),
            "edge_cents": getattr(exec_record.opportunity, 'edge_cents', None),
        }
        trades.append(trade_dict)

    return web.json_response({
        "timestamp": datetime.utcnow().isoformat(),
        "total_trades": len(trades),
        "trades": trades[-100:],  # Return last 100
    })


async def handle_logs(request):
    """Stream the last 100 lines from the server log file."""
    # Get the log file path from the logging configuration
    log_file = None
    for handler in logger.handlers:
        if hasattr(handler, 'baseFilename'):
            log_file = handler.baseFilename
            break

    # Also check parent loggers
    if not log_file:
        parent_logger = logging.getLogger("arbiter")
        for handler in parent_logger.handlers:
            if hasattr(handler, 'baseFilename'):
                log_file = handler.baseFilename
                break

    if not log_file or not os.path.exists(log_file):
        return web.json_response({
            "timestamp": datetime.utcnow().isoformat(),
            "log_file": log_file,
            "lines": [],
            "error": "Log file not found or not configured",
        })

    try:
        with open(log_file, "r") as f:
            all_lines = f.readlines()
        # Get last 100 lines
        last_lines = all_lines[-100:] if len(all_lines) > 100 else all_lines
        return web.json_response({
            "timestamp": datetime.utcnow().isoformat(),
            "log_file": log_file,
            "total_lines": len(all_lines),
            "returned_lines": len(last_lines),
            "lines": [line.rstrip() for line in last_lines],
        })
    except Exception as e:
        log_error("logs_endpoint", f"Failed to read log file: {e}", "warning")
        return web.json_response({
            "timestamp": datetime.utcnow().isoformat(),
            "log_file": log_file,
            "lines": [],
            "error": str(e),
        }, status=500)


# ── WebSocket ──────────────────────────────────────

async def handle_websocket(request):
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    ws_clients.append(ws)
    logger.info(f"WebSocket client connected ({len(ws_clients)} total)")

    try:
        async for msg in ws:
            if msg.type == aiohttp.WSMsgType.TEXT:
                try:
                    cmd = json.loads(msg.data)
                    if cmd.get("action") == "get_prices":
                        all_p = await price_store.get_all_prices()
                        await ws.send_json({"type": "prices", "data": {k: v.to_dict() for k, v in all_p.items()}})
                except Exception:
                    pass
            elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                break
    finally:
        ws_clients.remove(ws)
        logger.info(f"WebSocket client disconnected ({len(ws_clients)} remaining)")
    return ws


async def broadcast_loop():
    """Broadcast price updates, stats, health, errors, and trade events to WebSocket clients."""
    q = price_store.subscribe()
    tick = 0
    last_error_count = len(error_log)

    while True:
        try:
            # Price update
            try:
                price = await asyncio.wait_for(q.get(), timeout=2.0)
                msg = json.dumps({"type": "price_update", "data": price.to_dict()})
                for ws in list(ws_clients):
                    try:
                        await ws.send_str(msg)
                    except Exception:
                        pass
            except asyncio.TimeoutError:
                pass

            tick += 1

            # Collector health status broadcast every 10 ticks
            if tick % 10 == 0 and ws_clients:
                def _ws_cb_state(cb):
                    s = getattr(cb, 'state', 'unknown')
                    return s.value if hasattr(s, 'value') else str(s)

                collectors_health = {}
                if kalshi_col:
                    collectors_health["kalshi"] = {
                        "state": _ws_cb_state(kalshi_col.circuit),
                        "error_count": getattr(kalshi_col, 'total_errors', 0),
                    }
                if poly_col:
                    collectors_health["polymarket"] = {
                        "gamma_state": _ws_cb_state(poly_col.circuit_gamma),
                        "clob_state": _ws_cb_state(poly_col.circuit_clob),
                        "error_count": getattr(poly_col, 'total_errors', 0),
                    }
                if pi_col:
                    collectors_health["predictit"] = {
                        "state": _ws_cb_state(pi_col.circuit),
                        "error_count": getattr(pi_col, 'total_errors', 0),
                    }

                health_msg = json.dumps({
                    "type": "collector_health",
                    "timestamp": datetime.utcnow().isoformat(),
                    "data": collectors_health,
                })
                for ws in list(ws_clients):
                    try:
                        await ws.send_str(health_msg)
                    except Exception:
                        pass

            # Error event broadcast when new errors occur
            current_error_count = len(error_log)
            if current_error_count > last_error_count and ws_clients:
                # Broadcast the new error(s)
                new_errors = list(error_log)[-( current_error_count - last_error_count):]
                for error in new_errors:
                    error_msg = json.dumps({
                        "type": "error_event",
                        "data": error,
                    })
                    for ws in list(ws_clients):
                        try:
                            await ws.send_str(error_msg)
                        except Exception:
                            pass
                last_error_count = current_error_count

            # Trade execution event broadcast (if engine is running)
            if engine and ws_clients and hasattr(engine, 'last_broadcast_trade_idx'):
                last_idx = getattr(engine, 'last_broadcast_trade_idx', 0)
                current_idx = len(engine.execution_history)
                if current_idx > last_idx:
                    new_trades = engine.execution_history[last_idx:current_idx]
                    for trade in new_trades:
                        trade_msg = json.dumps({
                            "type": "trade_event",
                            "timestamp": trade.timestamp,
                            "data": {
                                "arb_id": trade.arb_id,
                                "status": trade.status,
                                "realized_pnl": trade.realized_pnl,
                            },
                        })
                        for ws in list(ws_clients):
                            try:
                                await ws.send_str(trade_msg)
                            except Exception:
                                pass
                    engine.last_broadcast_trade_idx = current_idx

            # Full system status broadcast every 15 ticks
            if tick % 15 == 0 and ws_clients:
                uptime = time.time() - start_time
                system_status = {
                    "uptime_s": round(uptime, 1),
                    "scanner_opps": len(scanner.current_opportunities) if scanner else 0,
                    "execution_trades": len(engine.execution_history) if engine else 0,
                    "error_log_size": len(error_log),
                    "ws_clients": len(ws_clients),
                }
                status_msg = json.dumps({
                    "type": "system_status",
                    "timestamp": datetime.utcnow().isoformat(),
                    "data": system_status,
                })
                for ws in list(ws_clients):
                    try:
                        await ws.send_str(status_msg)
                    except Exception:
                        pass

            # Stats broadcast every 5 ticks (original behavior)
            if tick % 5 == 0 and ws_clients:
                stats_msg = json.dumps({
                    "type": "stats",
                    "data": {
                        "scanner": scanner.stats if scanner else {},
                        "execution": engine.stats if engine else {},
                    }
                })
                for ws in list(ws_clients):
                    try:
                        await ws.send_str(stats_msg)
                    except Exception:
                        pass

        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Broadcast error: {e}")
            await asyncio.sleep(1)


# ── CORS Middleware ─────────────────────────────────

@web.middleware
async def cors_middleware(request, handler):
    if request.method == "OPTIONS":
        return web.Response(headers={
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type",
        })
    response = await handler(request)
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return response


# ── App Factory ────────────────────────────────────

def create_app():
    app = web.Application(middlewares=[cors_middleware])

    # Dashboard (root)
    app.router.add_get("/", handle_dashboard)

    # API routes
    app.router.add_get("/api/health", handle_health)
    app.router.add_get("/api/prices", handle_prices)
    app.router.add_get("/api/opportunities", handle_opportunities)
    app.router.add_get("/api/balances", handle_balances)
    app.router.add_get("/api/executions", handle_executions)
    app.router.add_get("/api/stats", handle_stats)
    app.router.add_get("/api/markets", handle_markets)

    # New system visibility endpoints
    app.router.add_get("/api/system", handle_system)
    app.router.add_get("/api/errors", handle_errors)
    app.router.add_get("/api/trades", handle_trades)
    app.router.add_get("/api/logs", handle_logs)

    # WebSocket
    app.router.add_get("/ws", handle_websocket)

    # Lifecycle
    app.on_startup.append(start_background_tasks)
    app.on_cleanup.append(cleanup_background_tasks)

    return app


if __name__ == "__main__":
    setup_logging("INFO")
    app = create_app()
    web.run_app(app, host="0.0.0.0", port=8080, print=lambda *a: logger.info(a[0] if a else ""))
