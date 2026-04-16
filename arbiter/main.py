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

from .audit.pnl_reconciler import PnLReconciler
from .config import ArbiterConfig, load_config
from .utils.logger import setup_logging, TradeLogger
from .utils.price_store import PricePoint, PriceStore
from .collectors.kalshi import KalshiCollector
from .collectors.polymarket import PolymarketCollector
from .collectors.predictit import PredictItCollector
from .scanner.arbitrage import ArbitrageScanner
from .monitor.balance import BalanceMonitor, BalanceSnapshot
from .execution.engine import ExecutionEngine
from .portfolio import PortfolioConfig, PortfolioMonitor
from .profitability import ProfitabilityConfig, ProfitabilityValidator
from .readiness import OperationalReadiness


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


async def run_system(config: ArbiterConfig, api_only: bool = False, host: str = "0.0.0.0", port: int = 8080):
    """Start all ARBITER components."""
    logger = logging.getLogger("arbiter.main")
    trade_logger = TradeLogger()

    # ── Core infrastructure ────────────────────────────────────
    price_store = PriceStore(redis_client=None, ttl=30)

    # ── Collectors ─────────────────────────────────────────────
    kalshi = KalshiCollector(config.kalshi, price_store)
    polymarket = PolymarketCollector(config.polymarket, price_store)
    predictit = PredictItCollector(config.predictit, price_store)

    # ── Scanner ────────────────────────────────────────────────
    scanner = ArbitrageScanner(config.scanner, price_store)
    arb_queue = scanner.subscribe()  # execution engine subscribes
    alert_queue = scanner.subscribe()  # balance monitor subscribes

    # ── Monitor ────────────────────────────────────────────────
    collectors_dict = {
        "kalshi": kalshi,
        "polymarket": polymarket,
        "predictit": predictit,
    }
    monitor = BalanceMonitor(config.alerts, collectors_dict)

    # ── Execution ──────────────────────────────────────────────
    engine = ExecutionEngine(config, monitor, price_store=price_store, collectors=collectors_dict)
    portfolio = PortfolioMonitor(
        PortfolioConfig(
            max_per_market_usd=config.scanner.max_position_usd,
            kalshi_min_balance=config.alerts.kalshi_low,
            polymarket_min_balance=config.alerts.polymarket_low,
            predictit_min_balance=config.alerts.predictit_low,
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
    engine.set_trade_gate(readiness.allow_execution)

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

    # ── Launch all tasks ───────────────────────────────────────
    tasks = []

    if not api_only:
        tasks.extend([
            asyncio.create_task(kalshi.run(), name="kalshi-collector"),
            asyncio.create_task(polymarket.run(), name="poly-collector"),
            asyncio.create_task(predictit.run(), name="pi-collector"),
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
    shutdown_event = asyncio.Event()

    def handle_shutdown(sig):
        logger.info(f"Received {sig.name}, shutting down...")
        shutdown_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        asyncio.get_event_loop().add_signal_handler(sig, handle_shutdown, sig)

    # Wait for shutdown signal
    await shutdown_event.wait()

    # Cancel all tasks
    logger.info("Stopping all components...")
    for task in tasks:
        task.cancel()

    await asyncio.gather(*tasks, return_exceptions=True)

    # Cleanup
    engine.stop_heartbeat()
    await kalshi.stop()
    await polymarket.stop()
    await predictit.stop()
    await scanner.stop()
    await monitor.stop()
    await engine.stop()
    portfolio.stop()
    profitability.stop()

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
        "predictit": BalanceSnapshot(platform="predictit", balance=62.10, timestamp=now, is_low=True),
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
            platform="predictit",
            canonical_id="DEM_SENATE_2026",
            yes_price=0.32,
            no_price=0.68,
            yes_volume=180,
            no_volume=180,
            timestamp=now,
            raw_market_id="PI-8155-DEM",
            yes_market_id="PI-8155-DEM",
            no_market_id="PI-8155-DEM-NO",
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
            platform="predictit",
            canonical_id="GOP_SENATE_2026",
            yes_price=0.29,
            no_price=0.71,
            yes_volume=165,
            no_volume=165,
            timestamp=now,
            raw_market_id="PI-8155-GOP",
            yes_market_id="PI-8155-GOP",
            no_market_id="PI-8155-GOP-NO",
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
