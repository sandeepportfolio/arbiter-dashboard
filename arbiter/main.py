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
from .execution.engine import ExecutionEngine, ExecutionIncident
from .execution.adapters import ForecastExAdapter, KalshiAdapter, PolymarketAdapter
from .execution.failure_tracker import (
    FailureTracker,
    make_failure_tracker_from_env,
)
from .execution.recovery import (
    RecoveryInitError,
    reconcile_half_recorded_arbs,
    reconcile_non_terminal_orders,
)
from .execution.store import ExecutionStore
from .execution.stuck_trade_recovery import (
    StuckTradeRecoveryStats,
    recover_stuck_trades,
)
from .portfolio import PortfolioConfig, PortfolioMonitor
from .profitability import ProfitabilityConfig, ProfitabilityValidator
from .readiness import OperationalReadiness
from .recovery import AutoResolver, AutoResolverConfig
from .safety.persistence import RedisStateShim, SafetyEventStore
from .safety.supervisor import SafetySupervisor
from .utils.retry import CircuitBreaker, RateLimiter
from .mapping.auto_discovery import discover as discover_market_mappings
from .mapping.forecastex_discovery import discover as discover_forecastex_mappings
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


def _build_shared_session() -> aiohttp.ClientSession:
    """Build the shared aiohttp session used by every platform adapter.

    Without an explicit timeout, aiohttp defaults to 300s total — far too
    generous for a trading hot path where a stuck request can pin engine
    state on SUBMITTED while real money is on the venue's books. The 30s
    total / 10s connect budget mirrors the per-leg execution timeout in
    ``run_system`` so a single request cannot exceed the engine's
    own deadlines.
    """
    timeout = aiohttp.ClientTimeout(total=30.0, connect=10.0)
    return aiohttp.ClientSession(timeout=timeout)


async def _build_redis_client(logger: logging.Logger):
    """Build an async Redis client when REDIS_URL is set, else return None.

    Returning None keeps PriceStore in pure-in-memory mode — the rest of the
    system tolerates that, but cross-restart persistence is lost.
    """
    redis_url = os.getenv("REDIS_URL", "").strip()
    if not redis_url:
        logger.info("REDIS_URL not set — running PriceStore in-memory only")
        return None

    try:
        import redis.asyncio as redis_async  # type: ignore[import-not-found]
    except ImportError:
        logger.warning(
            "REDIS_URL=%s set but redis package is not installed — falling back "
            "to in-memory PriceStore",
            redis_url,
        )
        return None

    try:
        client = redis_async.from_url(redis_url, decode_responses=True)
        # Verify connectivity up-front so failures surface during startup
        # rather than mid-trade.
        await client.ping()
        logger.info("Redis connected: %s", redis_url)
        return client
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "Redis connection failed for %s: %s — falling back to in-memory PriceStore",
            redis_url, exc,
        )
        return None


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


def build_forecastex_collector(config: ArbiterConfig, price_store: PriceStore):
    """Return the ForecastEx collector when the third platform is enabled.

    FORECASTEX_ENABLED=false collapses the build to ``None`` so existing
    two-platform deployments aren't forced to talk to an IBKR gateway that
    isn't running.
    """
    cfg = getattr(config, "forecastex", None)
    if cfg is None or not cfg.enabled:
        return None
    from .collectors.forecastex import ForecastExClient, ForecastExCollector

    client = ForecastExClient(
        gateway_url=cfg.gateway_url,
        account_id=cfg.account_id,
        verify_ssl=cfg.verify_ssl,
        paper_trading=cfg.paper_trading,
    )
    return ForecastExCollector(config=cfg, store=price_store, client=client)


def build_forecastex_adapter(config: ArbiterConfig, collector):
    """Return the ForecastEx adapter, reusing the collector's HTTP client."""
    cfg = getattr(config, "forecastex", None)
    if cfg is None or not cfg.enabled or collector is None:
        return None
    client = getattr(collector, "client", None)
    if client is None:
        return None
    return ForecastExAdapter(
        client=client,
        phase4_max_usd=_float_env("PHASE4_MAX_ORDER_USD"),
        phase5_max_usd=_float_env("PHASE5_MAX_ORDER_USD"),
    )


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

    return reconciler.reconcile(current_balances, engine.execution_history)


def surface_half_recorded_recovery_summary(
    engine: ExecutionEngine,
    half_recorded_arbs: list[dict],
) -> Optional[ExecutionIncident]:
    """Expose persisted half-recorded startup blockers to readiness/API state."""
    if not half_recorded_arbs:
        return None
    incident = ExecutionIncident(
        incident_id="INC-HALF-RECORDED-SUMMARY",
        arb_id="MULTIPLE",
        canonical_id="HALF_RECORDED_ARBS",
        severity="critical",
        message=(
            f"{len(half_recorded_arbs)} half-recorded arb(s) remain unresolved "
            "after startup recovery. Check /api/errors for per-arb details and "
            "reconcile venue exposure manually before enabling auto-trade."
        ),
        timestamp=time.time(),
        metadata={
            "event_type": "half_recorded_arb_summary",
            "count": len(half_recorded_arbs),
            "sample_arb_ids": [
                row.get("arb_id")
                for row in half_recorded_arbs[:10]
            ],
            "sample_canonical_ids": sorted({
                str(row.get("canonical_id") or "")
                for row in half_recorded_arbs
            })[:10],
        },
    )
    incidents = getattr(engine, "_incidents", None)
    if incidents is not None and not any(
        getattr(existing, "incident_id", "") == incident.incident_id
        for existing in incidents
    ):
        incidents.appendleft(incident)
    return incident


_MANUAL_CRITICAL_EVENT_TYPES = {
    "half_recorded_arb",
    "half_recorded_arb_summary",
    "one_leg_exposure",
    "soft_naked_leg",
    "db_write_failure",
}


def _is_stale_auto_resolvable_incident(
    incident,
    *,
    now: float,
    max_age: float,
) -> bool:
    if getattr(incident, "status", "open") == "resolved":
        return False
    if str(getattr(incident, "severity", "")).lower() != "critical":
        return False
    metadata = getattr(incident, "metadata", {}) or {}
    event_type = ""
    if isinstance(metadata, dict):
        event_type = str(metadata.get("event_type", "")).lower()
    if event_type in _MANUAL_CRITICAL_EVENT_TYPES:
        return False
    inc_ts = float(getattr(incident, "timestamp", now) or now)
    return now - inc_ts > max_age


async def rehydrate_open_incidents(
    store, engine: ExecutionEngine, logger: logging.Logger,
) -> int:
    """Pull every open execution_incidents row into ``engine._incidents``.

    The deque is bounded (``maxlen=200``) and starts empty on each boot, so
    without this rehydration any open critical incident from a prior container
    session vanishes from the readiness gate on restart and the trade gate
    re-arms despite the blocking exposure. Idempotent — skips incidents
    already in the deque so repeat invocations (e.g. live-tests) don't grow
    duplicates.

    Returns the count of incidents newly appended. Errors are logged and
    swallowed; a failure here must not block startup.
    """
    try:
        open_incidents = await store.list_incidents(status="open", limit=200)
    except Exception as exc:  # noqa: BLE001 — startup-safe
        logger.warning("Failed to rehydrate open incidents: %s", exc)
        return 0
    if not open_incidents:
        return 0
    existing_ids = {
        getattr(inc, "incident_id", "") for inc in engine._incidents
    }
    rehydrated = 0
    for inc in open_incidents:
        if getattr(inc, "incident_id", "") in existing_ids:
            continue
        engine._incidents.appendleft(inc)
        rehydrated += 1
    if rehydrated:
        logger.info(
            "Rehydrated %d open incident(s) into engine._incidents", rehydrated,
        )
    return rehydrated


def _component_stats(component) -> dict:
    """Return a component stats snapshot whether exposed as property or method."""
    raw = getattr(component, "stats", {}) if component is not None else {}
    if callable(raw):
        raw = raw()
    return raw if isinstance(raw, dict) else {}


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


async def _run_reconciler_lifecycle(reconciler, shutdown_event):
    """Drive the StrandedPositionReconciler periodic loop until shutdown.

    Lives in main.py rather than the reconciler module so the shutdown
    handshake matches the other long-running tasks here. The
    reconciler's own ``start()`` schedules an inner task; we keep
    that alive until the global ``shutdown_event`` fires and then
    request a clean ``stop()``.
    """
    await reconciler.start()
    try:
        await shutdown_event.wait()
    finally:
        await reconciler.stop()


async def run_stuck_trade_recovery_loop(
    store: ExecutionStore,
    adapters: dict,
    *,
    engine: Optional[ExecutionEngine] = None,
    interval_s: float = 300.0,
    max_age_seconds: float = 86400.0,
    stats: Optional[StuckTradeRecoveryStats] = None,
):
    """Re-poll the venue for every arb stuck >max_age_seconds, every interval_s.

    Pairs with the startup recovery in ``arbiter.execution.recovery``. That
    one runs once; this one runs forever so trades that go stuck *after*
    the process starts also get reconciled.
    """
    log = logging.getLogger("arbiter.main.stuck_trade_recovery")
    # Wait one interval before the first run so we don't double up with the
    # startup recovery that already executed.
    try:
        await asyncio.sleep(min(interval_s, 60.0))
    except asyncio.CancelledError:
        return

    while True:
        try:
            outcomes = await recover_stuck_trades(
                store, adapters, max_age_seconds=max_age_seconds, stats=stats,
            )
            if outcomes and engine is not None:
                # Reflect any status changes in the in-memory execution_history
                # so the dashboard updates without waiting for a restart.
                by_arb = {
                    e.arb_id: e for e in getattr(engine, "_executions", [])
                }
                for outcome in outcomes:
                    execution = by_arb.get(outcome.arb_id)
                    if execution is None:
                        continue
                    if outcome.new_status and outcome.new_status != execution.status:
                        execution.status = outcome.new_status
        except asyncio.CancelledError:
            return
        except Exception as exc:  # noqa: BLE001 — never let the loop die
            log.error("stuck_trade_recovery loop error: %s", exc)
        try:
            await asyncio.sleep(interval_s)
        except asyncio.CancelledError:
            return


async def run_incident_auto_resolve_loop(engine: ExecutionEngine, interval: float = 120.0, max_age: float = 120.0):
    """Periodically auto-resolve stale critical incidents so one audit flag
    doesn't permanently block the trade gate."""
    _logger = logging.getLogger("arbiter.main.incident_cleanup")
    while True:
        try:
            await asyncio.sleep(interval)
            now = time.time()
            for inc in list(getattr(engine, "_incidents", [])):
                if _is_stale_auto_resolvable_incident(
                    inc,
                    now=now,
                    max_age=max_age,
                ):
                    inc_ts = float(getattr(inc, "timestamp", now) or now)
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


async def run_auto_resolver_loop(
    auto_resolver: AutoResolver,
    *,
    interval_s: float = 300.0,
):
    """Periodically run the AutoResolver's three runtime mechanisms.

    Mechanisms invoked each pass (Components A, B, D from
    ``arbiter.recovery.auto_resolver``):
      - Reconcile half-recorded arbs whose unwind is already booked.
      - Classify (not act on) the kill-switch arm reason.
      - Expire stale incidents past their severity TTL.

    Component C (warm-restart eligibility) is consumed at startup by the
    readiness gate, not in this loop.

    The loop never dies on per-iteration errors; AutoResolver.run_once
    handles its own per-component error paths.
    """
    log = logging.getLogger("arbiter.main.auto_resolver")
    while True:
        try:
            summary = await auto_resolver.run_once()
            applied = [
                r for r in summary.get("reconciled", [])
                if r.get("outcome") == "applied"
            ]
            expired = [
                e for e in summary.get("expired_incidents", [])
                if e.get("outcome") == "expired"
            ]
            ks_verdict = summary.get("kill_switch", {}).get("verdict")
            if applied or expired or ks_verdict not in (None, "not_armed"):
                log.warning(
                    "auto_resolver pass: reconciled=%d expired=%d "
                    "kill_switch_verdict=%s",
                    len(applied), len(expired), ks_verdict,
                )
        except asyncio.CancelledError:
            return
        except Exception as exc:  # noqa: BLE001 — keep the loop alive
            log.error("auto_resolver loop error: %s", exc)
        try:
            await asyncio.sleep(interval_s)
        except asyncio.CancelledError:
            return


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
    failure_tracker=None,
    forecastex=None,
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
    if failure_tracker is not None:
        await _await_cleanup("failure_tracker.stop", failure_tracker.stop())
    await _await_cleanup("kalshi.stop", kalshi.stop())
    if polymarket is not None:
        await _await_cleanup("polymarket.stop", polymarket.stop())
    if forecastex is not None:
        await _await_cleanup("forecastex.stop", forecastex.stop())
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


async def run_expire_settled_mappings_loop(
    mapping_store: MarketMappingStore,
    *,
    interval_s: float = 3600.0,
) -> None:
    """Hourly sweep that demotes confirmed sports mappings whose embedded
    event date has already passed.

    The discovery pipeline only walks ``status='candidate'``, so without
    this sweep the sports-game tail accumulates indefinitely with
    ``allow_auto_trade=True`` pointing at long-settled markets. See
    ``arbiter.mapping.expire_settled`` for the date-parsing rules — only
    canonical_ids matching ``PREFIX_LEAGUE_YYYYMMDD_TEAM_…`` are eligible.
    """
    from .mapping.expire_settled import expire_settled_confirmed_mappings

    log = logging.getLogger("arbiter.main.expire_settled")
    while True:
        try:
            expired = await expire_settled_confirmed_mappings(mapping_store)
            if expired:
                log.info(
                    "expire_settled pass: demoted %d settled-date mapping(s) to EXPIRED",
                    len(expired),
                )
                # Push the status change into the in-memory MARKET_MAP that
                # the scanner and auto-executor read from; otherwise the
                # next pass would re-demote rows already flagged in DB.
                await mapping_store.refresh_runtime_cache()
        except asyncio.CancelledError:
            return
        except Exception as exc:  # noqa: BLE001 — keep the loop alive
            log.error("expire_settled loop error: %s", exc)
        try:
            await asyncio.sleep(interval_s)
        except asyncio.CancelledError:
            return


async def run_revalidate_review_mappings_loop(
    kalshi: KalshiCollector,
    polymarket,
    mapping_store: MarketMappingStore,
    *,
    interval_s: float = 3600.0,
) -> None:
    """Hourly sweep that re-runs the auto-promote 9-gate stack against
    ``status='review'`` mappings.

    Counterpart to the candidate→confirmed path inside
    ``run_market_discovery_loop``. Without this loop the records demoted
    by past live audits stay frozen at status='review' until an operator
    runs ``scripts/revalidate_review_mappings.py`` manually — see the
    script's docstring for the original demotion context.
    """
    from scripts.revalidate_review_mappings import revalidate

    log = logging.getLogger("arbiter.main.revalidate_review")
    poly_client = getattr(polymarket, "client", polymarket)

    # Stagger first run so we don't pile on the venues during restart-storm.
    try:
        await asyncio.sleep(min(interval_s, 60.0))
    except asyncio.CancelledError:
        return

    while True:
        try:
            promoted = await revalidate(
                dry_run=False,
                filter_prefix=None,
                limit=None,
                kalshi=kalshi,
                poly_client=poly_client,
                store=mapping_store,
            )
            if promoted:
                log.info(
                    "revalidate_review pass: promoted %d review mapping(s) to confirmed",
                    promoted,
                )
                await mapping_store.refresh_runtime_cache()
        except asyncio.CancelledError:
            return
        except Exception as exc:  # noqa: BLE001 — keep the loop alive
            log.error("revalidate_review loop error: %s", exc)
        try:
            await asyncio.sleep(interval_s)
        except asyncio.CancelledError:
            return


async def run_market_discovery_loop(
    kalshi: KalshiCollector,
    polymarket,
    mapping_store: MarketMappingStore,
    *,
    forecastex=None,
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
            # ForecastEx-side discovery: walk confirmed K↔P mappings and
            # attach IBKR conids for ones that have a matching FORECASTX
            # event title. Runs after the K↔P pass so newly confirmed
            # mappings get a shot in the same loop iteration.
            forecastex_attached = 0
            if forecastex is not None:
                fx_client = getattr(forecastex, "client", None)
                if fx_client is not None:
                    try:
                        forecastex_attached = await discover_forecastex_mappings(
                            fx_client, mapping_store,
                        )
                    except Exception as exc:  # noqa: BLE001
                        logger.warning(
                            "ForecastEx discovery pass failed: %s", exc,
                        )

            await mapping_store.refresh_runtime_cache()
            if hasattr(kalshi, "refresh_tracked_markets"):
                kalshi.refresh_tracked_markets()
            if hasattr(polymarket, "refresh_tracked_markets"):
                polymarket.refresh_tracked_markets()
            if forecastex is not None and hasattr(forecastex, "refresh_tracked_markets"):
                forecastex.refresh_tracked_markets()
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
    # Connect to Redis when REDIS_URL is configured so PriceStore survives
    # process restarts. Failure to connect is non-fatal — the store works
    # in-memory only — but is logged loudly so operators notice.
    redis_client = await _build_redis_client(logger)
    # TTL=30s matches the scanner's max_quote_age window (15s) plus a small
    # buffer. The previous 120s TTL combined with the get_all_prices ttl*6
    # window let 12-minute-stale quotes flow through downstream consumers.
    price_store = PriceStore(redis_client=redis_client, ttl=30)

    # Shared aiohttp session for adapter HTTP calls. Engine keeps its own
    # internal session for legacy paths; Phase 3 can consolidate.
    shared_session = _build_shared_session()

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
    forecastex = build_forecastex_collector(config, price_store)

    # ── Monitor ────────────────────────────────────────────────
    collectors_dict = {
        "kalshi": kalshi,
    }
    if polymarket is not None:
        collectors_dict["polymarket"] = polymarket
    if forecastex is not None:
        collectors_dict["forecastex"] = forecastex
    monitor = BalanceMonitor(config.alerts, collectors_dict)

    # ── Scanner (with balance-proportioned sizing) ────────────
    def _balance_provider():
        """Return current platform balances for position sizing."""
        return {
            platform: snapshot.balance
            for platform, snapshot in monitor.current_balances.items()
        }

    scanner = ArbitrageScanner(config.scanner, price_store, balance_provider=_balance_provider)
    alert_queue = scanner.subscribe()  # balance monitor approves before execution

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
    forecastex_adapter = build_forecastex_adapter(config, forecastex)
    adapters = {"kalshi": kalshi_adapter}
    if poly_adapter is not None:
        adapters["polymarket"] = poly_adapter
    if forecastex_adapter is not None:
        adapters["forecastex"] = forecastex_adapter
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
        price_store=price_store,
    )

    # ── Safety supervisor (SAFE-01, plan 03-01) ────────────────────
    safety_events_store = SafetyEventStore(
        pool=store._pool if store is not None else None
    )
    # Redis-backed kill-switch persistence: when the boolean is set in
    # Redis we restore the armed state on startup so a kill switch armed
    # before a container restart stays armed until an operator resets it.
    safety_redis = RedisStateShim(redis_client=redis_client) if redis_client is not None else None
    safety = SafetySupervisor(
        config=config.safety,
        engine=engine,
        adapters=adapters,
        notifier=monitor.notifier,  # reuse the single BalanceMonitor-owned Telegram client
        redis=safety_redis,
        store=store,
        safety_store=safety_events_store,
    )
    engine._safety = safety  # late injection for plan 03-03 one-leg hook
    await safety.restore_from_redis()

    # ── AutoResolver (verify-before-act recovery automation) ───────
    # Build once here, run periodically below. Operates on the same
    # store / safety_events / supervisor / notifier so a single test can
    # stub them. See arbiter/recovery/auto_resolver.py for the four
    # mechanisms (A reconcile, B classify, C warm-restart, D expire).
    auto_resolver: Optional[AutoResolver] = None
    if store is not None:
        auto_resolver = AutoResolver(
            store=store,
            safety_store=safety_events_store,
            supervisor=safety,
            notifier=monitor.notifier,
            config=AutoResolverConfig(),
        )

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
        execution_store=store,
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
    if config.forecastex is None:
        forecastex_auth = "disabled"
    elif config.forecastex.account_id:
        forecastex_auth = f"✓ ({'paper' if config.forecastex.paper_trading else 'live'})"
    else:
        forecastex_auth = "✗ (missing IBKR_ACCOUNT_ID)"
    logger.info(f"  ForecastEx auth: {forecastex_auth}")
    logger.info(f"  Telegram alerts: {'✓' if config.alerts.telegram_bot_token else '✗'}")
    logger.info("=" * 60)

    # ── Restart reconciliation (Pitfall 5 / D-17) ──────────────
    if store is not None:
        try:
            orphaned = await reconcile_non_terminal_orders(store, adapters)
        except RecoveryInitError as exc:
            # Cannot trust the in-memory engine state — arm SafetySupervisor
            # so no new trades are submitted until an operator clears the
            # underlying problem. Keep the API/dashboard up so they can
            # diagnose. We intentionally do NOT exit the process here so
            # the operator retains visibility into what state is on the
            # platform vs what we knew about.
            logger.critical(
                "Restart reconciliation failed (%s) — arming SafetySupervisor",
                exc,
            )
            try:
                await safety.trip_kill(
                    by="system:recovery",
                    reason=f"recovery_init_failed: {exc}",
                )
            except Exception as arm_exc:  # noqa: BLE001
                logger.error(
                    "Failed to arm SafetySupervisor after recovery error: %s",
                    arm_exc,
                )
            orphaned = []
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

        # Half-recorded-arb reconciliation: a process kill between primary
        # fill and the atomic ``record_arb`` (both-legs upsert) leaves an
        # ``execution_arbs`` stub with only one leg persisted. The engine's
        # exception-escape guard now prevents the synchronous source of
        # these orphans, but genuine SIGKILL / OOM / power-loss between
        # writes can still produce them — and any pre-fix rows already
        # in the DB also need flagging.
        half_recorded_arbs = []
        try:
            half_recorded_arbs = await reconcile_half_recorded_arbs(store)
        except RecoveryInitError as exc:
            logger.critical(
                "Half-recorded arb reconciliation failed (%s) — arming SafetySupervisor",
                exc,
            )
            try:
                await safety.trip_kill(
                    by="system:recovery",
                    reason=f"half_recorded_arb_recovery_failed: {exc}",
                )
            except Exception as arm_exc:  # noqa: BLE001
                logger.error(
                    "Failed to arm SafetySupervisor after half-recorded-arb error: %s",
                    arm_exc,
                )
        surface_half_recorded_recovery_summary(engine, half_recorded_arbs)

    # ── Rehydrate execution history from database ──────────────
    # Populates the in-memory execution_history so reconciliation sees the
    # full realized-P&L ledger and the dashboard can show historical trades
    # and positions after a restart.
    if store is not None:
        try:
            past_executions = await store.load_execution_history(limit=None)
            if past_executions:
                engine._executions.extend(past_executions)
                logger.info(
                    "Rehydrated %d past execution(s) into engine",
                    len(past_executions),
                )
        except Exception as exc:
            logger.warning("Failed to rehydrate execution history: %s", exc)

    # ── Rehydrate open incidents from database ─────────────────
    # engine._incidents is a bounded deque initialised empty at boot. Without
    # this rehydration, any critical incident raised by a prior container
    # session vanishes from the readiness gate on restart — auto-trade would
    # re-arm despite the blocking exposure that incident represents. The
    # helper is extracted so unit tests can exercise it without spinning the
    # full main() startup path.
    if store is not None:
        await rehydrate_open_incidents(store, engine, logger)

    # Seed engine._execution_count from the highest arb_id in the DB so
    # newly-minted ARB-NNN identifiers don't collide with persisted rows
    # from prior container sessions.  Without this, every restart re-uses
    # the same low ARB-000001..ARB-000020 ids, and execution_orders ends
    # up with multiple rows for the same arb_id from different trades
    # entirely.  Rehydration then mis-attributes leg statuses, which
    # poisoned the reconciler's survivor-credit logic and re-introduced
    # the drift block this fix is meant to resolve.
    if store is not None:
        try:
            async with store._pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT MAX(arb_id) AS max_id FROM execution_arbs WHERE arb_id LIKE 'ARB-%'"
                )
                max_id = (row["max_id"] if row else None) or "ARB-000000"
                try:
                    engine._execution_count = int(max_id.split("-")[1])
                    logger.info(
                        "Seeded engine._execution_count=%d from DB max %s",
                        engine._execution_count, max_id,
                    )
                except (IndexError, ValueError):
                    pass
        except Exception as exc:
            logger.warning("Failed to seed _execution_count from DB: %s", exc)

    # ── AutoExecutor (Phase 6 Plan 06-01) ──────────────────────
    # Subscribes to scanner, executes on opportunities that pass all 7 policy
    # gates (enabled, is_armed, requires_manual, allow_auto_trade, duplicate,
    # notional cap, bootstrap cap). DEFAULT OFF — must set AUTO_EXECUTE_ENABLED=true.
    from .execution.auto_executor import (
        make_auto_executor_from_env,
        make_settings_mapping_adapter,
    )
    from .config.settings import MARKET_MAP

    # FailureTracker — sliding-window backoff per (market, side, price-bucket).
    # Subscribes to the engine in start(); auto-executor consults it in the
    # pre-flight gate to skip submissions on tuples that have failed 3+ times
    # in the trailing hour.
    failure_tracker = make_failure_tracker_from_env(
        engine=engine, config_env=os.environ,
    )

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
        failure_tracker=failure_tracker,
        opportunity_queue=monitor.approved_opportunity_queue,
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
    # Stuck-trade recovery loop and failure tracker stats surface in /api/metrics.
    stuck_recovery_stats = StuckTradeRecoveryStats()
    api.attach_failure_tracker(failure_tracker)
    api.attach_stuck_recovery_stats(stuck_recovery_stats)
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
    now = time.time()
    stale_incidents = [
        inc for inc in getattr(engine, "_incidents", [])
        if _is_stale_auto_resolvable_incident(inc, now=now, max_age=60.0)
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
        if forecastex is not None:
            tasks.append(asyncio.create_task(forecastex.run(), name="forecastex-collector"))
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
                        forecastex=forecastex,
                        metrics=pm_us_metrics,
                    ),
                    name="market-discovery",
                )
            )
        tasks.extend([
            asyncio.create_task(scanner.run(), name="arb-scanner"),
            asyncio.create_task(monitor.run(alert_queue), name="balance-monitor"),
            asyncio.create_task(portfolio.run(), name="portfolio-monitor"),
        ])
        if isinstance(poly_adapter, PolymarketAdapter):
            tasks.append(asyncio.create_task(engine.polymarket_heartbeat_loop(), name="poly-heartbeat"))
        # Auto-executor only runs outside api_only mode (it needs scanner+engine).
        await auto_executor.start()
        # Retry scheduler subscribes to engine executions and auto-retries
        # failed arbs with fresh quotes; safe to run in api_only-skipped paths.
        await retry_scheduler.start()
        # FailureTracker also subscribes to engine executions; start AFTER the
        # other subscribers so its queue is wired in the same order.
        await failure_tracker.start()

    # ── Auto-resolve stale critical incidents from previous runs ─────
    # On fresh startup, any leftover critical incidents from a prior session
    # will block the trade gate indefinitely. Since we're restarting with new
    # state, auto-resolve them so trading can begin clean.
    stale_incidents = [
        inc for inc in getattr(engine, "incidents", [])
        if _is_stale_auto_resolvable_incident(inc, now=time.time(), max_age=60.0)
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

    # ── Telegram heartbeat ─────────────────────────────────────────
    # Confirms the alert pipe is alive even when no trade events
    # have fired. Reuses the BalanceMonitor-owned TelegramNotifier so
    # token/dedup/burst-guard config is shared. Gated on
    # AUTO_EXECUTE_ENABLED inside run_heartbeat — silent in dev.
    if monitor is not None and getattr(monitor, "notifier", None) is not None:
        from .notifiers.heartbeat import run_heartbeat, HeartbeatStatus

        heartbeat_interval = int(os.getenv("ARBITER_HEARTBEAT_INTERVAL_S", "1800"))

        def _heartbeat_status() -> HeartbeatStatus:
            try:
                eng_stats = _component_stats(engine)
                scan_stats = _component_stats(scanner)
                pf_snap = profitability.get_snapshot() if profitability else None
                ae_stats = getattr(auto_executor, "stats", None)
                realized_pnl = float(eng_stats.get("total_pnl", 0.0) or 0.0)
                if reconciler is not None:
                    recon_pnl = (getattr(reconciler, "stats", {}) or {}).get("recorded_pnl", {})
                    if recon_pnl:
                        realized_pnl = float(sum(float(v or 0.0) for v in recon_pnl.values()))
                balances = {
                    pid: round(float(snap.balance), 2)
                    for pid, snap in (monitor._balances or {}).items()
                    if snap is not None
                } if hasattr(monitor, "_balances") else {}
                extra = {
                    "auto_execute": os.getenv("AUTO_EXECUTE_ENABLED", "false"),
                    "scans": scan_stats.get("scan_count", 0),
                    "active_opps": scan_stats.get("active_opportunities", 0),
                    "published": scan_stats.get("published", 0),
                    "best_edge_c": scan_stats.get("best_edge_cents", 0),
                    "executed": getattr(ae_stats, "executed", 0) if ae_stats else 0,
                    "considered": getattr(ae_stats, "considered", 0) if ae_stats else 0,
                    "balances": ", ".join(f"{k}=${v}" for k, v in balances.items()),
                    "verdict": pf_snap.verdict if pf_snap else "unknown",
                    "naked_legs": eng_stats.get("naked_leg_count", 0),
                }
                return HeartbeatStatus(
                    realized_pnl=realized_pnl,
                    open_order_count=int(eng_stats.get("open_orders", 0) or 0),
                    extra=extra,
                )
            except Exception:
                return HeartbeatStatus()

        tasks.append(asyncio.create_task(
            run_heartbeat(monitor.notifier, interval_sec=heartbeat_interval, get_status=_heartbeat_status),
            name="telegram-heartbeat",
        ))

    # AutoResolver periodic loop (5-min default). Reconciles half-recorded
    # arbs whose unwind is already booked, classifies the kill-switch arm
    # reason, and expires stale incidents past their TTL. Construction was
    # done above next to the safety supervisor.
    if auto_resolver is not None:
        ar_interval = float(os.getenv("AUTO_RESOLVER_INTERVAL_S", "300"))
        tasks.append(asyncio.create_task(
            run_auto_resolver_loop(auto_resolver, interval_s=ar_interval),
            name="auto-resolver",
        ))

    # Mapping-lifecycle loops (hourly default each). Both only run when a
    # MarketMappingStore exists — DATABASE_URL must be set. The expire pass
    # demotes confirmed sports mappings whose event date has passed; the
    # revalidate pass walks status='review' rows and promotes any that now
    # clear the 9-gate auto-promote stack.
    if mapping_store is not None:
        expire_interval_s = float(os.getenv("EXPIRE_SETTLED_INTERVAL_S", "3600"))
        tasks.append(asyncio.create_task(
            run_expire_settled_mappings_loop(mapping_store, interval_s=expire_interval_s),
            name="expire-settled-mappings",
        ))
        if (
            polymarket is not None
            and hasattr(kalshi, "list_all_markets")
            and hasattr(getattr(polymarket, "client", polymarket), "list_markets")
        ):
            revalidate_interval_s = float(os.getenv("REVALIDATE_REVIEW_INTERVAL_S", "3600"))
            tasks.append(asyncio.create_task(
                run_revalidate_review_mappings_loop(
                    kalshi, polymarket, mapping_store,
                    interval_s=revalidate_interval_s,
                ),
                name="revalidate-review-mappings",
            ))

    # Stuck-trade recovery loop (5-min default). Only runs when a Postgres
    # store and at least one platform adapter are configured — otherwise the
    # loop has nothing to query or reconcile against.
    if store is not None and adapters:
        interval_s = float(os.getenv("STUCK_TRADE_RECOVERY_INTERVAL_S", "300"))
        max_age_s = float(os.getenv("STUCK_TRADE_MAX_AGE_S", "86400"))
        tasks.append(asyncio.create_task(
            run_stuck_trade_recovery_loop(
                store,
                adapters,
                engine=engine,
                interval_s=interval_s,
                max_age_seconds=max_age_s,
                stats=stuck_recovery_stats,
            ),
            name="stuck-trade-recovery",
        ))

    # ── Stranded-position reconciler ──────────────────────────────
    # Periodic venue-truth audit that closes the gap between what the
    # engine THINKS exists and what venues actually hold. Surfaces the
    # 25-lot stranded inventory exposed by the FILL-01 audit and
    # protects against future drift from crashed sessions or adapter
    # exceptions during unwind. Conservative default (auto_close=False
    # so operator sees every incident before any close happens).
    if adapters:
        from .recovery.stranded_reconciler import reconciler_from_env
        stranded_reconciler = reconciler_from_env(
            config=config, adapters=adapters, engine=engine,
            forecastex_client=(forecastex.client if forecastex is not None else None),
            price_store=price_store,
            notifier=monitor.notifier if monitor is not None else None,
        )
        # Stash on the API so /api/system can render the latest
        # snapshot without re-querying the venues.
        api.stranded_reconciler = stranded_reconciler
        tasks.append(asyncio.create_task(
            _run_reconciler_lifecycle(stranded_reconciler, shutdown_event),
            name="stranded-position-reconciler",
        ))

    # ── ForecastEx child-conid auto-resolver ──────────────────────
    # The 24 confirmed FX mappings have parent EVENT conids that don't
    # trade — they need YES child conids. Resolver retries IBKR's
    # /iserver/secdef/* endpoints periodically (they're 503 on weekends)
    # and persists the child conid back to the DB so the collector
    # picks it up on the next poll cycle. Without this loop the system
    # is permanently stuck on parent-only conids until an operator
    # manually fixes each mapping.
    if forecastex is not None and mapping_store is not None:
        from .recovery.forecastex_resolver import resolver_from_env as fx_resolver_from_env
        fx_resolver = fx_resolver_from_env(
            forecastex_client=forecastex.client,
            forecastex_collector=forecastex,
            mapping_store=mapping_store,
        )
        if fx_resolver is not None:
            api.forecastex_resolver = fx_resolver
            # Wire the collector → resolver disable trigger so cycle 1
            # doesn't race ahead of the first parent-conid disable. With
            # a fixed startup sleep the resolver saw candidates=0 every
            # restart and waited 30 min for cycle 2; the callback makes
            # the next cycle fire within ~60s of the first disable
            # event instead.
            try:
                forecastex.set_disable_callback(
                    lambda _conid: fx_resolver.trigger()
                )
            except AttributeError:
                # Older collector without the hook — harmless; the
                # periodic interval still picks up disables eventually.
                logger.warning(
                    "ForecastEx collector missing set_disable_callback; "
                    "resolver will rely on periodic interval only"
                )
            tasks.append(asyncio.create_task(
                _run_reconciler_lifecycle(fx_resolver, shutdown_event),
                name="forecastex-child-resolver",
            ))
            logger.info(
                "forecastex-child-resolver task scheduled (interval=%ss, dry_run=%s)",
                fx_resolver._interval_s, fx_resolver._dry_run,
            )
        else:
            logger.warning(
                "forecastex-child-resolver NOT scheduled: resolver_from_env returned None"
            )
    else:
        logger.warning(
            "forecastex-child-resolver NOT scheduled: forecastex=%s mapping_store=%s",
            forecastex is not None, mapping_store is not None,
        )

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
            failure_tracker=failure_tracker,
            forecastex=forecastex,
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
        failure_tracker=failure_tracker,
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
