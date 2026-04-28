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
from contextlib import suppress
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
from .mapping.auto_discovery import discover as discover_market_mappings
from .mapping.market_map import MarketMappingStore

import sentry_sdk
from sentry_sdk.integrations.aiohttp import AioHttpIntegration
from sentry_sdk.integrations.asyncio import AsyncioIntegration
from sentry_sdk.integrations.logging import LoggingIntegration

from .config.settings import PolymarketConfig, PolymarketUSConfig
from .operator_settings import OperatorSettingsStore, load_market_discovery_settings
from .runtime_lock import RuntimeLockError, acquire_runtime_lock


def build_polymarket_component(config: ArbiterConfig):
    """Return the correct Polymarket adapter (or None) based on config.polymarket type.

    This is a minimal factory used for variant selection and rollback smoke tests.
    It does NOT wire the adapter into the engine — that happens in run_system().

    Returns
    -------
    PolymarketAdapter | PolymarketUSAdapter | None
        - None           when config.polymarket is None  (POLYMARKET_VARIANT=disabled)
        - PolymarketAdapter     when config.polymarket is PolymarketConfig (legacy)
        - PolymarketUSAdapter   when config.polymarket is PolymarketUSConfig (us)
    """
    if config.polymarket is None:
        return None

    if isinstance(config.polymarket, PolymarketUSConfig):
        from .collectors.polymarket_us import PolymarketUSClient
        from .auth.ed25519_signer import Ed25519Signer
        from .execution.adapters.polymarket_us import PolymarketUSAdapter

        cfg = config.polymarket
        # Only build the signer if credentials are present; otherwise use a stub
        if cfg.api_key_id and cfg.api_secret:
            signer = Ed25519Signer(key_id=cfg.api_key_id, secret_b64=cfg.api_secret)
        else:
            # Stub signer for test/dry-run contexts where no real credentials exist
            signer = None  # type: ignore[assignment]

        client = PolymarketUSClient(
            base_url=cfg.api_url,
            public_base_url=cfg.gateway_url,
            signer=signer,
        )
        return PolymarketUSAdapter(client=client)

    if isinstance(config.polymarket, PolymarketConfig):
        # Legacy CLOB adapter — defer heavy imports to avoid side effects in tests
        from .execution.adapters.polymarket import PolymarketAdapter as _PolymarketAdapter

        # Build a minimal adapter without a real ClobClient (dry-run / rollback context).
        # The full wire-up with a live ClobClient lives in run_system().
        return _PolymarketAdapter(
            config=config,
            clob_client_factory=lambda: None,
            rate_limiter=None,  # type: ignore[arg-type]
            circuit=None,       # type: ignore[arg-type]
        )

    return None


def _float_env(name: str) -> Optional[float]:
    raw = os.getenv(name, "").strip()
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return 0.0


def build_polymarket_collector(config: ArbiterConfig, price_store: PriceStore):
    """Return the correct Polymarket collector for the current runtime variant."""
    if config.polymarket is None:
        return None

    if isinstance(config.polymarket, PolymarketUSConfig):
        from .auth.ed25519_signer import Ed25519Signer
        from .collectors.polymarket_us import PolymarketUSClient, PolymarketUSCollector

        cfg = config.polymarket
        signer = None
        if cfg.api_key_id and cfg.api_secret:
            signer = Ed25519Signer(key_id=cfg.api_key_id, secret_b64=cfg.api_secret)
        client = PolymarketUSClient(
            base_url=cfg.api_url,
            public_base_url=cfg.gateway_url,
            signer=signer,
        )
        return PolymarketUSCollector(config=cfg, store=price_store, client=client)

    if isinstance(config.polymarket, PolymarketConfig):
        return PolymarketCollector(config.polymarket, price_store)

    return None


def build_polymarket_adapter(
    config: ArbiterConfig,
    *,
    engine: Optional[ExecutionEngine] = None,
    collector=None,
    rate_limiter=None,
    circuit=None,
):
    """Return the correct Polymarket adapter for the current runtime variant."""
    if config.polymarket is None:
        return None

    if isinstance(config.polymarket, PolymarketUSConfig):
        from .auth.ed25519_signer import Ed25519Signer
        from .collectors.polymarket_us import PolymarketUSClient
        from .execution.adapters.polymarket_us import PolymarketUSAdapter

        cfg = config.polymarket
        client = getattr(collector, "client", None)
        if client is None:
            signer = None
            if cfg.api_key_id and cfg.api_secret:
                signer = Ed25519Signer(key_id=cfg.api_key_id, secret_b64=cfg.api_secret)
            client = PolymarketUSClient(
                base_url=cfg.api_url,
                public_base_url=cfg.gateway_url,
                signer=signer,
            )
        return PolymarketUSAdapter(
            client=client,
            phase4_max_usd=_float_env("PHASE4_MAX_ORDER_USD"),
            phase5_max_usd=_float_env("PHASE5_MAX_ORDER_USD"),
        )

    if isinstance(config.polymarket, PolymarketConfig):
        clob_client_factory = (
            (lambda: engine._get_poly_clob_client()) if engine is not None else (lambda: None)
        )
        return PolymarketAdapter(
            config=config,
            clob_client_factory=clob_client_factory,
            rate_limiter=rate_limiter,
            circuit=circuit,
        )

    return None


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
    has_executions = bool(engine.execution_history)

    for platform, balance in current_balances.items():
        if platform not in known_balances:
            # First time seeing this platform — set starting balance.
            # If we restored from Postgres, this branch won't fire for
            # already-persisted platforms.
            reconciler.set_starting_balance(platform, balance)
        elif not has_executions and not reconciler._deposit_events and not reconciler._restored_from_db:
            # No trades executed yet, no deposit history, AND no state
            # restored from Postgres — re-baseline to current balance so
            # that external balance changes don't trigger a false-positive.
            # When _restored_from_db is True, starting balances are
            # authoritative from the database and must NOT be overwritten.
            if abs(known_balances[platform] - balance) > 0.01:
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
    # Wait up to 30s for the balance monitor to fetch initial balances
    # before starting the main reconciliation loop.
    for _ in range(15):
        if monitor.current_balances:
            break
        await asyncio.sleep(2.0)

    while True:
        try:
            sync_runtime_reconciliation(reconciler, monitor, engine)
            # Use a shorter interval if starting balances haven't been set yet
            # (e.g. first startup before any balances fetched).
            has_starting = bool(reconciler.stats.get("starting_balances"))
            interval = reconciler.check_interval if has_starting else 5.0
            await asyncio.sleep(interval)
        except asyncio.CancelledError:
            break
        except Exception as exc:
            logger.error("PnL reconciliation loop error: %s", exc)
            await asyncio.sleep(min(reconciler.check_interval, 10.0))


async def run_incident_auto_resolve_loop(engine: ExecutionEngine, interval: float = 120.0, max_age: float = 120.0):
    """Periodically auto-resolve stale critical incidents so one audit flag
    doesn't permanently block the trade gate."""
    _logger = logging.getLogger("arbiter.main.incident_cleanup")
    while True:
        try:
            await asyncio.sleep(interval)
            now = time.time()
            for inc in list(getattr(engine, "_incidents", [])):
                if getattr(inc, "status", "open") == "resolved":
                    continue
                if str(getattr(inc, "severity", "")).lower() != "critical":
                    continue
                inc_ts = getattr(inc, "timestamp", now)
                if now - inc_ts > max_age:
                    await engine.resolve_incident(
                        inc.incident_id,
                        note=f"Auto-resolved: stale critical incident (age={int(now - inc_ts)}s > {int(max_age)}s)",
                    )
                    _logger.info("Auto-resolved stale incident %s (age=%ds)", inc.incident_id, int(now - inc_ts))
        except asyncio.CancelledError:
            break
        except Exception as exc:
            _logger.error("Incident auto-resolve loop error: %s", exc)
            await asyncio.sleep(30.0)


async def cleanup_runtime(
    *,
    logger: logging.Logger,
    engine: ExecutionEngine,
    auto_executor,
    kalshi: KalshiCollector,
    polymarket,
    scanner: ArbitrageScanner,
    monitor: BalanceMonitor,
    portfolio: PortfolioMonitor,
    profitability: ProfitabilityValidator,
    store: Optional[ExecutionStore],
    mapping_store: Optional[MarketMappingStore],
    shared_session: aiohttp.ClientSession,
    retry_scheduler=None,
) -> None:
    """Best-effort teardown for shutdown and failed startup paths."""

    async def _await_cleanup(label: str, awaitable) -> None:
        try:
            await awaitable
        except Exception as exc:
            logger.warning("Cleanup step %s failed: %s", label, exc)

    engine.stop_heartbeat()
    await _await_cleanup("auto_executor.stop", auto_executor.stop())
    if retry_scheduler is not None:
        await _await_cleanup("retry_scheduler.stop", retry_scheduler.stop())
    await _await_cleanup("kalshi.stop", kalshi.stop())
    if polymarket is not None:
        await _await_cleanup("polymarket.stop", polymarket.stop())
    await _await_cleanup("scanner.stop", scanner.stop())
    await _await_cleanup("monitor.stop", monitor.stop())
    await _await_cleanup("engine.stop", engine.stop())
    with suppress(Exception):
        portfolio.stop()
    with suppress(Exception):
        profitability.stop()
    if store is not None:
        await _await_cleanup("store.disconnect", store.disconnect())
    if mapping_store is not None:
        await _await_cleanup("mapping_store.disconnect", mapping_store.disconnect())
    if not shared_session.closed:
        await _await_cleanup("shared_session.close", shared_session.close())


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


def arm_critical_task_watch(
    task: asyncio.Task,
    *,
    shutdown_event: asyncio.Event,
    shutdown_state: dict,
    logger: logging.Logger,
    fatal_error_holder: Optional[dict] = None,
) -> None:
    """Turn unexpected task exit into a full-process shutdown.

    Without this guard, a failed ``api-server`` task can leave the live engine,
    collectors, and auto-executor running headless. That is exactly the class of
    failure that can produce a duplicate live engine after a restart attempt.
    """

    def _on_done(done_task: asyncio.Task) -> None:
        if shutdown_state.get("in_progress"):
            return
        if done_task.cancelled():
            return

        task_name = done_task.get_name() or "unnamed-task"
        try:
            exc = done_task.exception()
        except asyncio.CancelledError:
            return

        if exc is None:
            logger.error(
                "Critical task %s exited unexpectedly, initiating shutdown",
                task_name,
            )
        else:
            logger.error(
                "Critical task %s crashed, initiating shutdown: %s",
                task_name,
                exc,
                exc_info=(type(exc), exc, exc.__traceback__),
            )
            if fatal_error_holder is not None and fatal_error_holder.get("exc") is None:
                fatal_error_holder["exc"] = exc

        shutdown_state["in_progress"] = True
        shutdown_event.set()

    task.add_done_callback(_on_done)


async def run_market_discovery_loop(
    kalshi: KalshiCollector,
    polymarket,
    mapping_store: MarketMappingStore,
    *,
    metrics: Optional[dict] = None,
) -> None:
    """Continuously refresh candidate mappings through the canonical store."""
    logger = logging.getLogger("arbiter.main.discovery")
    settings_store = OperatorSettingsStore()
    poly_client = getattr(polymarket, "client", polymarket)

    while True:
        runtime = load_market_discovery_settings(settings_store)
        interval_seconds = float(runtime["auto_discovery_interval_seconds"])

        try:
            if not runtime["auto_discovery_enabled"]:
                logger.info("Market discovery paused by operator runtime settings")
                if metrics is not None:
                    metrics["auto_discovery_last_written"] = 0
                await asyncio.sleep(interval_seconds)
                continue

            written = await discover_market_mappings(
                kalshi,
                poly_client,
                mapping_store,
                budget_rps=float(runtime["auto_discovery_budget_rps"]),
                min_score=float(runtime["auto_discovery_min_score"]),
                max_candidates=int(runtime["auto_discovery_max_candidates"]),
                promotion_settings=runtime,
            )
            pending = await mapping_store.count_candidates()
            if metrics is not None:
                metrics["auto_discovery_candidates_pending"] = pending
                metrics["auto_discovery_last_written"] = written
            logger.info(
                "Market discovery pass complete: wrote=%s pending_candidates=%s interval=%.1fs budget_rps=%.2f min_score=%.2f max_candidates=%s",
                written,
                pending,
                interval_seconds,
                float(runtime["auto_discovery_budget_rps"]),
                float(runtime["auto_discovery_min_score"]),
                int(runtime["auto_discovery_max_candidates"]),
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("Market discovery pass failed: %s", exc)

        await asyncio.sleep(interval_seconds)


async def run_system(config: ArbiterConfig, api_only: bool = False, host: str = "0.0.0.0", port: int = 8080):
    """Start all ARBITER components."""
    logger = logging.getLogger("arbiter.main")
    trade_logger = TradeLogger()

    # ── Core infrastructure ────────────────────────────────────
    price_store = PriceStore(redis_client=None, ttl=120)

    # Shared aiohttp session for adapter HTTP calls. Engine keeps its own
    # internal session for legacy paths; Phase 3 can consolidate.
    shared_session = aiohttp.ClientSession()

    kalshi_circuit = CircuitBreaker(
        name="kalshi-exec", failure_threshold=5, recovery_timeout=30.0,
    )
    kalshi_rate_limiter = RateLimiter(
        name="kalshi-exec", max_requests=10, window_seconds=1.0,
    )
    poly_circuit = CircuitBreaker(
        name="poly-exec", failure_threshold=5, recovery_timeout=30.0,
    )
    poly_rate_limiter = RateLimiter(
        name="poly-exec", max_requests=5, window_seconds=1.0,
    )

    # ── Collectors ─────────────────────────────────────────────
    kalshi = KalshiCollector(config.kalshi, price_store)
    polymarket = build_polymarket_collector(config, price_store)

    # ── Monitor ────────────────────────────────────────────────
    collectors_dict = {
        "kalshi": kalshi,
    }
    if polymarket is not None:
        collectors_dict["polymarket"] = polymarket
    monitor = BalanceMonitor(config.alerts, collectors_dict)

    # ── Scanner (with balance-proportioned sizing) ────────────
    def _balance_provider():
        """Return current platform balances for position sizing."""
        return {
            platform: snapshot.balance
            for platform, snapshot in monitor.current_balances.items()
        }

    scanner = ArbitrageScanner(config.scanner, price_store, balance_provider=_balance_provider)
    arb_queue = scanner.subscribe()  # execution engine subscribes
    alert_queue = scanner.subscribe()  # balance monitor subscribes

    # ── Persistence (EXEC-02) ──────────────────────────────────
    database_url = os.getenv("DATABASE_URL")
    store: Optional[ExecutionStore] = None
    mapping_store: Optional[MarketMappingStore] = None
    if database_url:
        store = ExecutionStore(database_url)
        await store.connect()
        await store.init_schema()
        logger.info("ExecutionStore connected, schema applied")

        mapping_store = MarketMappingStore(database_url)
        await mapping_store.connect()
        await mapping_store.init_schema()
        await mapping_store.seed_from_records()
        await mapping_store.refresh_runtime_cache()
        logger.info("MarketMappingStore connected, schema applied, runtime cache hydrated")
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

    kalshi_adapter = KalshiAdapter(
        config=config,
        session=shared_session,
        auth=kalshi.auth,  # KalshiAuth (not the collector)
        rate_limiter=kalshi_rate_limiter,
        circuit=kalshi_circuit,
    )
    poly_adapter = build_polymarket_adapter(
        config,
        engine=engine,
        collector=polymarket,
        rate_limiter=poly_rate_limiter,
        circuit=poly_circuit,
    )
    adapters = {"kalshi": kalshi_adapter}
    if poly_adapter is not None:
        adapters["polymarket"] = poly_adapter
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
    reconciler = PnLReconciler(
        log_to_disk=not api_only,
        pg_pool=store._pool if store is not None else None,
    )
    # Restore persisted starting balances and deposit history from PostgreSQL
    # so P&L tracking survives container restarts.
    if store is not None:
        restored = await reconciler.load_persisted_state()
        if restored:
            logger.info("PnL reconciler: restored persisted balances and deposits")
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
    if isinstance(polymarket, PolymarketCollector):
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
        mapping_store=mapping_store,
    )

    pm_us_metrics = {
        "auto_discovery_candidates_pending": 0,
        "auto_promote_rejections": {},
    }
    setattr(api, "_pm_us_metrics", pm_us_metrics)
    if mapping_store is not None:
        pm_us_metrics["auto_discovery_candidates_pending"] = await mapping_store.count_candidates()

    logger.info("=" * 60)
    logger.info("  ARBITER — Prediction Market Arbitrage System")
    logger.info("=" * 60)
    logger.info(f"  Mode: {'DRY RUN (simulation)' if config.scanner.dry_run else '🔴 LIVE TRADING'}")
    logger.info(f"  Min edge: {config.scanner.min_edge_cents}¢")
    logger.info(f"  Max position: ${config.scanner.max_position_usd}")
    logger.info(f"  Kalshi auth: {'✓' if kalshi.auth.is_authenticated else '✗ (public data only)'}")
    if config.polymarket is None:
        poly_auth = "disabled"
    elif isinstance(config.polymarket, PolymarketUSConfig):
        poly_auth = "✓" if (config.polymarket.api_key_id and config.polymarket.api_secret) else "✗"
    else:
        poly_auth = "✓" if getattr(config.polymarket, "private_key", None) else "✗"
    logger.info(f"  Polymarket auth: {poly_auth}")
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

    # ── Rehydrate execution history from database ──────────────
    # Populates the in-memory execution_history so the dashboard
    # shows historical trades and positions after a restart.
    if store is not None:
        try:
            past_executions = await store.load_execution_history(limit=200)
            if past_executions:
                engine._executions.extend(past_executions)
                logger.info(
                    "Rehydrated %d past execution(s) into engine",
                    len(past_executions),
                )
        except Exception as exc:
            logger.warning("Failed to rehydrate execution history: %s", exc)

    # ── AutoExecutor (Phase 6 Plan 06-01) ──────────────────────
    # Subscribes to scanner, executes on opportunities that pass all 7 policy
    # gates (enabled, is_armed, requires_manual, allow_auto_trade, duplicate,
    # notional cap, bootstrap cap). DEFAULT OFF — must set AUTO_EXECUTE_ENABLED=true.
    from .execution.auto_executor import (
        make_auto_executor_from_env,
        make_settings_mapping_adapter,
    )
    from .config.settings import MARKET_MAP

    auto_executor = make_auto_executor_from_env(
        scanner=scanner,
        engine=engine,
        supervisor=safety,
        mapping_store=mapping_store or make_settings_mapping_adapter(MARKET_MAP),
        config_env=os.environ,
        # Pre-flight deps: fresh quotes from price_store and orderbook depth
        # checks via the engine's adapters. Auto-disabled when adapters dict
        # is empty (dry-run / api-only).
        price_store=price_store,
        adapters_provider=lambda: dict(getattr(engine, "adapters", {}) or {}),
    )
    # Expose to the api server so /api/metrics can surface auto_executor stats
    # and so persisted operator settings can hydrate the runtime knobs.
    api.attach_auto_executor(auto_executor)

    # ── RetryScheduler ────────────────────────────────────────
    # Subscribes to ExecutionEngine and retries failed arbs once fresh quotes
    # arrive. Wired into the API so /api/failed-trades surfaces classified
    # failure reasons and per-arb retry history.
    from .execution.retry_scheduler import make_retry_scheduler_from_env

    retry_scheduler = make_retry_scheduler_from_env(
        engine=engine,
        price_store=price_store,
        supervisor=safety,
        config_env=os.environ,
    )
    api.attach_retry_scheduler(retry_scheduler)
    logger.info(
        f"  Auto-execute: {'✓ ENABLED' if auto_executor._config.enabled else '✗ disabled (AUTO_EXECUTE_ENABLED=false)'}"
    )
    logger.info(
        f"  Max position: ${auto_executor._config.max_position_usd:.2f}"
    )
    if auto_executor._config.bootstrap_trades is not None:
        logger.info(
            f"  Bootstrap cap: {auto_executor._config.bootstrap_trades} trades"
        )

    # ── Auto-resolve stale critical incidents from prior sessions ─
    # Without this, a single audit flag from a previous session blocks all
    # trades permanently. Resolve anything older than 60 seconds.
    stale_incidents = [
        inc for inc in getattr(engine, "_incidents", [])
        if getattr(inc, "status", "open") != "resolved"
        and str(getattr(inc, "severity", "")).lower() == "critical"
    ]
    if stale_incidents:
        for inc in stale_incidents:
            await engine.resolve_incident(
                inc.incident_id,
                note="Auto-resolved on startup: stale critical incident from previous session",
            )
        logger.info("Auto-resolved %d stale critical incidents on startup", len(stale_incidents))

    # ── Launch all tasks ───────────────────────────────────────
    tasks: list[asyncio.Task] = []
    shutdown_event = asyncio.Event()
    shutdown_state = {"in_progress": False}
    fatal_task_error: dict[str, BaseException | None] = {"exc": None}

    if not api_only:
        tasks.append(asyncio.create_task(kalshi.run(), name="kalshi-collector"))
        if polymarket is not None:
            tasks.append(asyncio.create_task(polymarket.run(), name="poly-collector"))
        if (
            mapping_store is not None
            and polymarket is not None
            and hasattr(kalshi, "list_all_markets")
            and hasattr(getattr(polymarket, "client", polymarket), "list_markets")
        ):
            tasks.append(
                asyncio.create_task(
                    run_market_discovery_loop(
                        kalshi,
                        polymarket,
                        mapping_store,
                        metrics=pm_us_metrics,
                    ),
                    name="market-discovery",
                )
            )
        tasks.extend([
            asyncio.create_task(scanner.run(), name="arb-scanner"),
            asyncio.create_task(monitor.run(alert_queue), name="balance-monitor"),
            asyncio.create_task(engine.run(arb_queue), name="execution-engine"),
            asyncio.create_task(portfolio.run(), name="portfolio-monitor"),
        ])
        if isinstance(poly_adapter, PolymarketAdapter):
            tasks.append(asyncio.create_task(engine.polymarket_heartbeat_loop(), name="poly-heartbeat"))
        # Auto-executor only runs outside api_only mode (it needs scanner+engine).
        await auto_executor.start()
        # Retry scheduler subscribes to engine executions and auto-retries
        # failed arbs with fresh quotes; safe to run in api_only-skipped paths.
        await retry_scheduler.start()

    # ── Auto-resolve stale critical incidents from previous runs ─────
    # On fresh startup, any leftover critical incidents from a prior session
    # will block the trade gate indefinitely. Since we're restarting with new
    # state, auto-resolve them so trading can begin clean.
    stale_incidents = [
        inc for inc in getattr(engine, "incidents", [])
        if getattr(inc, "status", "open") != "resolved"
        and str(getattr(inc, "severity", "")).lower() == "critical"
    ]
    if stale_incidents:
        logger.info(
            "Auto-resolving %d stale critical incidents from previous run",
            len(stale_incidents),
        )
        for inc in stale_incidents:
            try:
                await engine.resolve_incident(
                    inc.incident_id,
                    note="Auto-resolved on restart: stale incident from previous session",
                )
            except Exception as exc:
                logger.warning("Failed to auto-resolve incident %s: %s", inc.incident_id, exc)

    tasks.append(asyncio.create_task(profitability.run(), name="profitability-validator"))
    tasks.append(asyncio.create_task(run_reconciliation_loop(reconciler, monitor, engine), name="pnl-reconciler"))
    tasks.append(asyncio.create_task(run_incident_auto_resolve_loop(engine, interval=120.0, max_age=120.0), name="incident-auto-resolve"))

    # API server always runs
    tasks.append(asyncio.create_task(api.serve(), name="api-server"))

    # ── Graceful shutdown ──────────────────────────────────────
    # SAFE-05: cancel orders BEFORE cancelling tasks. A second SIGINT/SIGTERM
    # triggers an immediate forced exit so operators always have a hard exit
    # hatch if a hung adapter or deadlock ever blocks the 5s trip_kill window.
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

    for task in tasks:
        arm_critical_task_watch(
            task,
            shutdown_event=shutdown_event,
            shutdown_state=shutdown_state,
            logger=logger,
            fatal_error_holder=fatal_task_error,
        )

    api_start_timeout = max(float(os.getenv("ARBITER_API_STARTUP_TIMEOUT_S", "10.0")), 1.0)
    try:
        await api.wait_until_started(timeout=api_start_timeout)
    except Exception:
        shutdown_state["in_progress"] = True
        logger.critical(
            "API server failed to start on %s:%s",
            host,
            port,
            exc_info=True,
        )
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await cleanup_runtime(
            logger=logger,
            engine=engine,
            auto_executor=auto_executor,
            kalshi=kalshi,
            polymarket=polymarket,
            scanner=scanner,
            monitor=monitor,
            portfolio=portfolio,
            profitability=profitability,
            store=store,
            mapping_store=mapping_store,
            shared_session=shared_session,
            retry_scheduler=retry_scheduler,
        )
        raise

    # Wait for shutdown signal.
    await shutdown_event.wait()

    # Cancel orders BEFORE tasks (SAFE-05 fail-safe).
    await run_shutdown_sequence(safety, tasks, timeout=5.0)

    # Cleanup
    await cleanup_runtime(
        logger=logger,
        engine=engine,
        auto_executor=auto_executor,
        kalshi=kalshi,
        polymarket=polymarket,
        scanner=scanner,
        monitor=monitor,
        portfolio=portfolio,
        profitability=profitability,
        store=store,
        mapping_store=mapping_store,
        shared_session=shared_session,
        retry_scheduler=retry_scheduler,
    )

    # Final stats
    logger.info("─" * 40)
    logger.info(f"Scanner stats: {scanner.stats}")
    logger.info(f"Execution stats: {engine.stats}")
    logger.info(f"Profitability: {profitability.get_snapshot().to_dict()}")
    logger.info("ARBITER shutdown complete")
    if fatal_task_error["exc"] is not None:
        raise fatal_task_error["exc"]


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

    from .config.settings import update_market_mapping

    update_market_mapping(
        "DEM_HOUSE_2026",
        status="review",
        allow_auto_trade=False,
        resolution_criteria={
            "kalshi": {
                "source": "Kalshi rulebook / Speaker of the House on 2027-02-01",
                "rule": "If the Democratic Party has won control of the House in 2026, the market resolves Yes.",
                "settlement_date": "2027-02-01",
            },
            "polymarket": {
                "source": "Polymarket US retail market metadata",
                "rule": "Will the Democratic Party win the House in the 2026 Midterms?",
                "settlement_date": "2027-02-01",
            },
            "criteria_match": "pending_operator_review",
            "operator_note": "Smoke fixture pending-review state for confirm-guard coverage.",
        },
        resolution_match_status="pending_operator_review",
        actor="smoke-fixture",
    )

    update_market_mapping(
        "GOP_HOUSE_2026",
        status="confirmed",
        allow_auto_trade=False,
        resolution_match_status="identical",
        actor="smoke-fixture",
    )

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
        # Static config checks first — these catch missing/half-configured env
        # vars that would otherwise blow up hours into operation.
        from .config.settings import validate_live_config
        config_errors = validate_live_config(config)
        if config_errors:
            for err in config_errors:
                logging.getLogger("arbiter.main").critical(
                    "Live startup blocked (config): %s", err,
                )
            sys.exit(2)
        readiness = OperationalReadiness(config)
        failures = readiness.startup_failures()
        if failures:
            for failure in failures:
                logging.getLogger("arbiter.main").critical("Live startup blocked: %s", failure)
            sys.exit(2)

    try:
        with acquire_runtime_lock(api_only=args.api_only, port=args.port):
            asyncio.run(run_system(config, api_only=args.api_only, host=args.host, port=args.port))
    except RuntimeLockError as exc:
        logging.getLogger("arbiter.main").critical("%s", exc)
        sys.exit(3)
    except Exception as exc:
        logging.getLogger("arbiter.main").critical(
            "ARBITER failed to start: %s",
            exc,
            exc_info=True,
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
