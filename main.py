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
import signal
import sys

from .config import ArbiterConfig, load_config
from .utils.logger import setup_logging, TradeLogger
from .utils.price_store import PriceStore
from .collectors.kalshi import KalshiCollector
from .collectors.polymarket import PolymarketCollector
from .collectors.predictit import PredictItCollector
from .scanner.arbitrage import ArbitrageScanner
from .monitor.balance import BalanceMonitor
from .execution.engine import ExecutionEngine


async def run_system(config: ArbiterConfig, api_only: bool = False):
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
    engine = ExecutionEngine(config, monitor)

    # ── API Server (for dashboard) ─────────────────────────────
    from .api import create_api_server
    api = create_api_server(price_store, scanner, engine, monitor, config)

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
        ])

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
    await kalshi.stop()
    await polymarket.stop()
    await predictit.stop()
    await scanner.stop()
    await monitor.stop()
    await engine.stop()

    # Final stats
    logger.info("─" * 40)
    logger.info(f"Scanner stats: {scanner.stats}")
    logger.info(f"Execution stats: {engine.stats}")
    logger.info("ARBITER shutdown complete")


def main():
    parser = argparse.ArgumentParser(description="ARBITER — Prediction Market Arbitrage")
    parser.add_argument("--live", action="store_true", help="Enable live trading (default: dry run)")
    parser.add_argument("--api-only", action="store_true", help="Run API server only")
    parser.add_argument("--port", type=int, default=8080, help="API server port")
    parser.add_argument("--log-level", default="INFO", help="Log level")
    parser.add_argument("--log-file", default=None, help="Log file path")
    args = parser.parse_args()

    setup_logging(args.log_level, args.log_file)

    config = load_config()
    if args.live:
        config.scanner.dry_run = False

    asyncio.run(run_system(config, api_only=args.api_only))


if __name__ == "__main__":
    main()
