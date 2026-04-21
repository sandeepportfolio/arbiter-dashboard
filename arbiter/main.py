"""
ARBITER — Main Orchestrator
Wires all components together and runs the system.

Usage:
    python -m arbiter.main              # dry-run mode (default)
    python -m arbiter.main --live       # live trading (requires API keys)
    python -m arbiter.main --api-only   # just the API server (for dashboard)
"""
import argparse
import asyncio
import logging
import os
import signal
import sys
import time
from pathlib import Path
from typing import Optional

import aiohttp

from .audit.pnl_reconciler import PnLReconciler
from .config import ArbiterConfig, load_config
from .utils.logger import setup_logging, TradeLogger
from .utils.price_store import PricePoint, PriceStore
from .collectors.kalshi import KalshiCollector
from .collectors.polymarket import PolymarketCollector
from .scanner.arbitrage import ArbitrageScanner
from .monitor.balance import BalanceMonitor, BalanceSnapshot
from .execution.engine import ExecutionEngine
from .execution.adapters import KalshiAdapter, PolymarketAdapter
from .execution.recovery import reconcile_non_terminal_orders
from .execution.store import ExecutionStore
from .portfolio import PortfolioConfig, PortfolioMonitor
from .profitability import ProfitabilityConfig, ProfitabilityValidator
from .readiness import OperationalReadiness
from .safety.persistence import SafetyEventStore
from .safety.supervisor import SafetySupervisor
from .utils.retry import CircuitBreaker, RateLimiter

import sentry_sdk
from sentry_sdk.integrations.aiohttp import AioHttpIntegration
from sentry_sdk.integrations.asyncio import AsyncioIntegration
from sentry_sdk.integrations.logging import LoggingIntegration


def _init_sentry() -> None:
    """Initialize sentry-sdk. No-op if SENTRY_DSN unset (sentry-sdk handles dsn=None)."""
    sentry_sdk.init(
        dsn=os.getenv("SENTRY_DSN") or None,
        environment=os.getenv("ARBITER_ENV", "development"),
        release=os.getenv("ARBITER_RELEASE", "unknown"),
        integrations=[
            AsyncioIntegration(),
            AioHttpIntegration(),
            LoggingIntegration(level=logging.INFO, event_level=logging.ERROR),
        ],
        traces_sample_rate=0.0,
        sample_rate=1.0,
        send_default_pii=False,
        attach_stacktrace=True,
    )


def sync_runtime_reconciliation(
    reconciler: PnLReconciler,
    monitor: BalanceMonitor,
    engine: ExecutionEngine,
):
    """Refresh the reconciler from the current balances and execution ledger."""
    current_balances = {
        platform: snapshot.balance
        for platform, snapshot in monitor.current_balances.items()
    }
    if not current_balances:
        return None

    known_balances = reconciler.stats.get("starting_balances", {})
    for platform, balance in current_balances.items():
        if platform not in known_balances:
            reconciler.set_starting_balance(platform, balance)

    reconciler.load_execution_history(engine.execution_history)
    return reconciler.reconcile(current_balances)


async def run_reconciliation_loop(
    reconciler: PnLReconciler,
    monitor: BalanceMonitor,
    engine: ExecutionEngine,
):
    """Continuously reconcile runtime balances against recorded execution P&L."""
    logger = logging.getLogger("arbiter.main")
    while True:
        try:
            sync_runtime_reconciliation(reconciler, monitor, engine)
            await asyncio.sleep(reconciler.check_interval)
        except asyncio.CancelledError:
            break
        except Exception as exc:
            logger.error("PnL reconciliation loop error: %s", exc)
            await asyncio.sleep(min(reconciler.check_interval, 10.0))


async def run_shutdown_sequence(
    safety: SafetySupervisor,
    tasks: list,
    *,
    timeout: float = 5.0,
) -> None:
    """Graceful-shutdown sequence — SAFE-05 fail-safe.

    Runs in the following order:
      1. ``safety.prepare_shutdown()`` — broadcasts ``shutdown_state`` then
         invokes ``trip_kill`` which fans out ``adapter.cancel_all()`` across
         every platform adapter in parallel (per-adapter 5s timeout inside
         the supervisor).
      2. ``task.cancel()`` — only AFTER the cancel fanout completes (or the
         ``timeout`` budget elapses).
      3. ``asyncio.gather(..., return_exceptions=True)`` — drain the tasks.

    The ``timeout`` argument is a hard upper bound on the ``prepare_shutdown``
    await; if it expires we log an error and fall through to ``task.cancel()``
    so the process can still exit. Second-signal escape hatch (forced
    immediate exit) lives in the signal handler in ``run_system``.
    """
    logger = logging.getLogger("arbiter.main")
    logger.info("Preparing safety-supervised shutdown...")
    try:
        await asyncio.wait_for(safety.prepare_shutdown(), timeout=timeout)
    except asyncio.TimeoutError:
        logger.error(
            "Kill-switch trip exceeded %.1fs — some orders may remain open",
            timeout,
        )
    except Exception as exc:
        logger.error(
            "safety.prepare_shutdown raised during shutdown sequence: %s", exc,
        )

    logger.info("Stopping all components...")
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


async def run_system(config: ArbiterConfig, api_only: bool = False, host: str = "0.0.0.0", port: int = 8080):
    """Start all ARBITER components."""
    logger = logging.getLogger("arbiter.main")
    trade_logger = TradeLogger()

    # ── Core infrastructure ────────────────────────────────────
    price_store = PriceStore(redis_client=None, ttl=30)

    # ── Collectors ─────────────────────────────────────────────
    kalshi = KalshiCollector(config.kalshi, price_store)
    polymarket = PolymarketCollector(config.polymarket, price_store)

    # ── Scanner ────────────────────────────────────────────────
    scanner = ArbitrageScanner(config.scanner, price_store)
    arb_queue = scanner.subscribe()  # execution engine subscribes
    alert_queue = scanner.subscribe()  # balance monitor subscribes

    # ── Monitor ────────────────────────────────────────────────
    collectors_dict = {
        "kalshi": kalshi,
        "polymarket": polymarket,
    }
    monitor = BalanceMonitor(config.alerts, collectors_dict)

    # ── Persistence (EXEC-02) ──────────────────────────────────
    database_url = os.getenv("DATABASE_URL")
    store: Optional[ExecutionStore] = None
    if database_url:
        store = ExecutionStore(database_url)
        await store.connect()
        await store.init_schema()
        logger.info("ExecutionStore connected, schema applied")
    else:
        logger.warning(
            "DATABASE_URL not set; execution persistence disabled (dev mode)"
        )

    execution_timeout_s = float(os.getenv("EXECUTION_TIMEOUT_S", "10.0"))

    # ── Execution ──────────────────────────────────────────────
    engine = ExecutionEngine(
        config,
        monitor,
        price_store=price_store,
        collectors=collectors_dict,
        store=store,
        execution_timeout_s=execution_timeout_s,
        # adapters attached right after — need engine reference for poly factory (D-13)
    )

    # ── Build adapters (uses engine for cached ClobClient via factory) ──
    # Shared aiohttp session for adapter HTTP calls. Engine keeps its own
    # internal session for legacy paths; Phase 3 can consolidate.
    shared_session = aiohttp.ClientSession()

    kalshi_circuit = CircuitBreaker(
        name="kalshi-exec", failure_threshold=5, recovery_timeout=30.0,
    )
    kalshi_rate_limiter = RateLimiter(
        name="kalshi-exec", max_requests=10, window_seconds=1.0,  # SAFE-04: 10 writes/sec
    )
    poly_circuit = CircuitBreaker(
        name="poly-exec", failure_threshold=5, recovery_timeout=30.0,
    )
    poly_rate_limiter = RateLimiter(
        name="poly-exec", max_requests=5, window_seconds=1.0,  # conservative starting point
    )

    kalshi_adapter = KalshiAdapter(
        config=config,
        session=shared_session,
        auth=kalshi.auth,  # KalshiAuth (not the collector)
        rate_limiter=kalshi_rate_limiter,
        circuit=kalshi_circuit,
    )
    poly_adapter = PolymarketAdapter(
        config=config,
        # D-13: share cached ClobClient with heartbeat task via factory closure
        clob_client_factory=lambda: engine._get_poly_clob_client(),
        rate_limiter=poly_rate_limiter,
        circuit=poly_circuit,
    )
    adapters = {"kalshi": kalshi_adapter, "polymarket": poly_adapter}
    engine.adapters = adapters  # late binding — engine constructed before adapters

    portfolio = PortfolioMonitor(
        PortfolioConfig(
            max_per_market_usd=config.scanner.max_position_usd,
            kalshi_min_balance=config.alerts.kalshi_low,
            polymarket_min_balance=config.alerts.polymarket_low,
        ),
        config.scanner,
        engine,
        monitor,
    )
    profitability = ProfitabilityValidator(ProfitabilityConfig(), scanner, engine)
    reconciler = PnLReconciler(log_to_disk=not api_only)
    readiness = OperationalReadiness(
        config,
        engine=engine,
        monitor=monitor,
        profitability=profitability,
        collectors=collectors_dict,
        reconciler=reconciler,
    )

    # ── Safety supervisor (SAFE-01, plan 03-01) ────────────────────
    safety_events_store = SafetyEventStore(
        pool=store._pool if store is not None else None
    )
    safety = SafetySupervisor(
        config=config.safety,
        engine=engine,
        adapters=adapters,
        notifier=monitor.notifier,  # reuse the single BalanceMonitor-owned Telegram client
        redis=None,                 # optional; wire a RedisStateShim in plan 03-05 if needed
        store=store,
        safety_store=safety_events_store,
    )
    engine._safety = safety  # late injection for plan 03-03 one-leg hook

    # Apply safety_events DDL when a Postgres pool is available. Additionally
    # re-run init.sql idempotently so schema migrations (SAFE-06 ALTER TABLE
    # market_mappings columns, etc.) land on restart. Every statement in
    # init.sql uses IF NOT EXISTS / IF NOT EXISTS forms so reruns are safe.
    if store is not None and getattr(store, "_pool", None) is not None:
        for sql_name in ("safety_events.sql", "init.sql"):
            try:
                sql_path = Path(__file__).parent / "sql" / sql_name
                ddl = sql_path.read_text()
                async with store._pool.acquire() as conn:
                    await conn.execute(ddl)
                logger.info("%s schema ensured", sql_name)
            except Exception as exc:
                logger.warning("%s migration skipped: %s", sql_name, exc)

    # Chain trade gate: readiness first, safety second. Denials from either
    # layer short-circuit and preserve the tuple shape returned by the denier.
    async def chained_gate(opp):
        readiness_res = readiness.allow_execution(opp)
        if asyncio.iscoroutine(readiness_res):
            readiness_res = await readiness_res
        if isinstance(readiness_res, tuple):
            if len(readiness_res) >= 1 and not readiness_res[0]:
                return readiness_res
        elif not readiness_res:
            return (False, "readiness denied", {})
        return await safety.allow_execution(opp)

    engine.set_trade_gate(chained_gate)

    # Wire ClobClient to collector for dynamic fee rate lookup (D-09)
    # This happens lazily -- the collector will use fallback rates until ClobClient is ready
    poly_clob = engine._get_poly_clob_client()
    if poly_clob is not None:
        polymarket.set_clob_client(poly_clob)

    if api_only and os.getenv("ARBITER_UI_SMOKE_SEED") == "1":
        await seed_dashboard_fixture(price_store, scanner, engine, monitor)
        sync_runtime_reconciliation(reconciler, monitor, engine)
        profitability.refresh()
        readiness.refresh()

    # ── API Server (for dashboard) ─────────────────────────────
    from .api import create_api_server
    api = create_api_server(
        price_store,
        scanner,
        engine,
        monitor,
        config,
        collectors=collectors_dict,
        portfolio=portfolio,
        workflow_manager=None,
        profitability=profitability,
        readiness=readiness,
        reconciler=reconciler,
        host=host,
        port=port,
        safety=safety,
    )

    logger.info("=" * 60)
    logger.info("  ARBITER — Prediction Market Arbitrage System")
    logger.info("=" * 60)
    logger.info(f"  Mode: {'DRY RUN (simulation)' if config.scanner.dry_run else '🔴 LIVE TRADING'}")
    logger.info(f"  Min edge: {config.scanner.min_edge_cents}¢")
    logger.info(f"  Max position: ${config.scanner.max_position_usd}")
    logger.info(f"  Kalshi auth: {'✓' if kalshi.auth.is_authenticated else '✗ (public data only)'}")
    logger.info(f"  Polymarket wallet: {'✓' if config.polymarket.private_key else '✗'}")
    logger.info(f"  Telegram alerts: {'✓' if config.alerts.telegram_bot_token else '✗'}")
    logger.info("=" * 60)

    # ── Restart reconciliation (Pitfall 5 / D-17) ──────────────
    if store is not None:
        orphaned = await reconcile_non_terminal_orders(store, adapters)
        for o in orphaned:
            try:
                parts = o.order_id.split("-")
                arb_id_resolved = (
                    "-".join(parts[0:2]) if len(parts) >= 2 else o.order_id
                )
                await engine.record_incident(
                    arb_id=arb_id_resolved,
                    canonical_id=o.canonical_id,
                    severity="warning",
                    message=f"Orphaned order on restart: {o.order_id}",
                    metadata={"platform": o.platform, "error": o.error},
                )
            except Exception as exc:
                logger.warning("Failed to emit orphaned-order incident: %s", exc)

    # ── Launch all tasks ───────────────────────────────────────
    tasks = []

    if not api_only:
        tasks.extend([
            asyncio.create_task(kalshi.run(), name="kalshi-collector"),
            asyncio.create_task(polymarket.run(), name="poly-collector"),
            asyncio.create_task(scanner.run(), name="arb-scanner"),
            asyncio.create_task(monitor.run(alert_queue), name="balance-monitor"),
            asyncio.create_task(engine.run(arb_queue), name="execution-engine"),
            asyncio.create_task(portfolio.run(), name="portfolio-monitor"),
            asyncio.create_task(engine.polymarket_heartbeat_loop(), name="poly-heartbeat"),
        ])

    tasks.append(asyncio.create_task(profitability.run(), name="profitability-validator"))
    tasks.append(asyncio.create_task(run_reconciliation_loop(reconciler, monitor, engine), name="pnl-reconciler"))

    # API server always runs
    tasks.append(asyncio.create_task(api.serve(), name="api-server"))

    # ── Graceful shutdown ──────────────────────────────────────
    # SAFE-05: cancel orders BEFORE cancelling tasks. A second SIGINT/SIGTERM
    # triggers an immediate forced exit so operators always have a hard exit
    # hatch if a hung adapter or deadlock ever blocks the 5s trip_kill window.
    shutdown_event = asyncio.Event()
    shutdown_state = {"in_progress": False}

    def handle_shutdown(sig):
        if shutdown_state["in_progress"]:
            logger.warning(
                "Received %s again, forcing immediate exit", sig.name,
            )
            os._exit(1)
        shutdown_state["in_progress"] = True
        logger.info("Received %s, shutting down...", sig.name)
        shutdown_event.set()

    # NOTE: Windows asyncio loops do not support add_signal_handler. Wrap in
    # a try/except so `python -m arbiter.main` still runs on Win32; SIGINT
    # there falls through to KeyboardInterrupt handling in asyncio.run.
    try:
        for sig in (signal.SIGINT, signal.SIGTERM):
            asyncio.get_event_loop().add_signal_handler(sig, handle_shutdown, sig)
    except NotImplementedError:
        logger.info(
            "signal.add_signal_handler unavailable on this platform; "
            "installing SIGBREAK via signal.signal() for CTRL_BREAK_EVENT "
            "compatibility (SAFE-05 graceful-shutdown subprocess test relies "
            "on this for Windows CI)",
        )
        # Windows fallback: synchronous signal.signal() handler that schedules
        # the shutdown on the running loop. SIGBREAK is raised by CTRL_BREAK_EVENT
        # on processes created with CREATE_NEW_PROCESS_GROUP — which is how
        # Scenario 9 sends the shutdown signal. Without this, CTRL_BREAK falls
        # through Python's default handler and terminates the process with
        # STATUS_CONTROL_C_EXIT (0xC000013A) before any shutdown sequence runs.
        loop = asyncio.get_event_loop()
        def _win_signal_handler(sig_num, _frame):
            try:
                sig_enum = signal.Signals(sig_num)
            except (ValueError, AttributeError):
                sig_enum = sig_num
            loop.call_soon_threadsafe(handle_shutdown, sig_enum)
        for sig in (getattr(signal, "SIGBREAK", None), signal.SIGINT, signal.SIGTERM):
            if sig is not None:
                try:
                    signal.signal(sig, _win_signal_handler)
                except (ValueError, OSError):
                    pass

    # Wait for shutdown signal.
    await shutdown_event.wait()

    # Cancel orders BEFORE tasks (SAFE-05 fail-safe).
    await run_shutdown_sequence(safety, tasks, timeout=5.0)

    # Cleanup
    engine.stop_heartbeat()
    await kalshi.stop()
    await polymarket.stop()
    await scanner.stop()
    await monitor.stop()
    await engine.stop()
    portfolio.stop()
    profitability.stop()
    if store is not None:
        await store.disconnect()
    if not shared_session.closed:
        await shared_session.close()

    # Final stats
    logger.info("─" * 40)
    logger.info(f"Scanner stats: {scanner.stats}")
    logger.info(f"Execution stats: {engine.stats}")
    logger.info(f"Profitability: {profitability.get_snapshot().to_dict()}")
    logger.info("ARBITER shutdown complete")


async def seed_dashboard_fixture(
    price_store: PriceStore,
    scanner: ArbitrageScanner,
    engine: ExecutionEngine,
    monitor: BalanceMonitor,
):
    """Populate deterministic state for dashboard smoke tests."""
    now = time.time()

    monitor._balances = {
        "kalshi": BalanceSnapshot(platform="kalshi", balance=148.22, timestamp=now, is_low=False),
        "polymarket": BalanceSnapshot(platform="polymarket", balance=79.54, timestamp=now, is_low=False),
    }

    seed_prices = [
        PricePoint(
            platform="kalshi",
            canonical_id="DEM_HOUSE_2026",
            yes_price=0.41,
            no_price=0.59,
            yes_volume=140,
            no_volume=140,
            timestamp=now,
            raw_market_id="KXPRESPARTY-2028",
            yes_market_id="KXPRESPARTY-2028",
            no_market_id="KXPRESPARTY-2028",
            fee_rate=0.07,
            mapping_status="candidate",
            mapping_score=0.42,
        ),
        PricePoint(
            platform="polymarket",
            canonical_id="DEM_HOUSE_2026",
            yes_price=0.49,
            no_price=0.43,
            yes_volume=160,
            no_volume=160,
            timestamp=now,
            raw_market_id="PM-HOUSE-2026",
            yes_market_id="PM-HOUSE-2026-YES",
            no_market_id="PM-HOUSE-2026-NO",
            fee_rate=0.01,
            mapping_status="candidate",
            mapping_score=0.42,
        ),
        PricePoint(
            platform="kalshi",
            canonical_id="DEM_SENATE_2026",
            yes_price=0.32,
            no_price=0.68,
            yes_volume=180,
            no_volume=180,
            timestamp=now,
            raw_market_id="K-SEN-2026-DEM",
            yes_market_id="K-SEN-2026-DEM",
            no_market_id="K-SEN-2026-DEM-NO",
            fee_rate=0.07,
            mapping_status="confirmed",
            mapping_score=0.91,
        ),
        PricePoint(
            platform="polymarket",
            canonical_id="DEM_SENATE_2026",
            yes_price=0.58,
            no_price=0.44,
            yes_volume=220,
            no_volume=220,
            timestamp=now,
            raw_market_id="PM-SEN-2026-DEM",
            yes_market_id="PM-SEN-2026-DEM-YES",
            no_market_id="PM-SEN-2026-DEM-NO",
            fee_rate=0.01,
            mapping_status="confirmed",
            mapping_score=0.91,
        ),
        PricePoint(
            platform="kalshi",
            canonical_id="GOP_SENATE_2026",
            yes_price=0.29,
            no_price=0.71,
            yes_volume=165,
            no_volume=165,
            timestamp=now,
            raw_market_id="K-SEN-2026-GOP",
            yes_market_id="K-SEN-2026-GOP",
            no_market_id="K-SEN-2026-GOP-NO",
            fee_rate=0.07,
            mapping_status="confirmed",
            mapping_score=0.89,
        ),
        PricePoint(
            platform="polymarket",
            canonical_id="GOP_SENATE_2026",
            yes_price=0.55,
            no_price=0.46,
            yes_volume=210,
            no_volume=210,
            timestamp=now,
            raw_market_id="PM-SEN-2026-GOP",
            yes_market_id="PM-SEN-2026-GOP-YES",
            no_market_id="PM-SEN-2026-GOP-NO",
            fee_rate=0.01,
            mapping_status="confirmed",
            mapping_score=0.89,
        ),
    ]
    for price in seed_prices:
        await price_store.put(price)

    for _ in range(scanner.config.persistence_scans):
        await scanner.scan_once()

    for canonical_id in ("DEM_SENATE_2026", "GOP_SENATE_2026"):
        manual_opportunity = next(
            (
                opportunity
                for opportunity in scanner.current_opportunities
                if opportunity.canonical_id == canonical_id and opportunity.status == "manual"
            ),
            None,
        )
        if manual_opportunity is not None:
            await engine.execute_opportunity(manual_opportunity)

    await engine.record_incident(
        arb_id="ARB-SEED-RECOVERY",
        canonical_id="DEM_HOUSE_2026",
        severity="warning",
        message="Seeded recovery check awaiting operator acknowledgement",
        metadata={"route": "Kalshi vs Polymarket", "reason": "dashboard smoke fixture"},
    )


def main():
    parser = argparse.ArgumentParser(description="ARBITER — Prediction Market Arbitrage")
    parser.add_argument("--live", action="store_true", help="Enable live trading (default: dry run)")
    parser.add_argument("--api-only", action="store_true", help="Run API server only")
    parser.add_argument("--host", default=os.getenv("ARBITER_HOST", "0.0.0.0"), help="API server host/interface")
    parser.add_argument("--port", type=int, default=8080, help="API server port")
    parser.add_argument("--log-level", default="INFO", help="Log level")
    parser.add_argument("--log-file", default=None, help="Log file path")
    args = parser.parse_args()

    _init_sentry()              # must be before setup_logging so LoggingIntegration sees the JSON formatter
    setup_logging(args.log_level, args.log_file)

    config = load_config()
    if args.live:
        config.scanner.dry_run = False
        readiness = OperationalReadiness(config)
        failures = readiness.startup_failures()
        if failures:
            for failure in failures:
                logging.getLogger("arbiter.main").critical("Live startup blocked: %s", failure)
            sys.exit(2)

    asyncio.run(run_system(config, api_only=args.api_only, host=args.host, port=args.port))


if __name__ == "__main__":
    main()
