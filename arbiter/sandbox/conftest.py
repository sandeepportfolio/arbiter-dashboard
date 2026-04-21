"""Shared fixtures and opt-in wiring for arbiter.sandbox tests (Phase 4 live-fire).

Root conftest.py owns async test dispatch via pytest_pyfunc_call; DO NOT redefine it here.
Sandbox scenarios are plain `async def` + @pytest.mark.live.
"""
from __future__ import annotations

import logging
import pathlib
from datetime import datetime, timezone

import pytest
import structlog
from structlog.stdlib import ProcessorFormatter

from arbiter.utils.logger import SHARED_PROCESSORS


# Re-export fixtures from fixtures/ submodule so scenario tests can consume them via conftest.
# pytest_plugins, --live option, and live marker moved to root conftest.py
# (pytest 8+ deprecates pytest_plugins in non-top-level conftests)


def pytest_collection_modifyitems(config, items):
    # Opt-in: if user passed --live OR -m live, do not skip.
    if config.getoption("--live"):
        return
    markexpr = config.getoption("-m", default="") or ""
    if "live" in markexpr:
        return
    skip_live = pytest.mark.skip(reason="Use -m live or --live to run Phase 4 scenarios")
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip_live)


@pytest.fixture
def evidence_dir(request):
    """Per-scenario evidence directory + structlog JSONL file handler.

    Creates `evidence/04/<scenario>_<UTC timestamp>/` and installs a logging.FileHandler
    that writes structured JSON (via structlog's ProcessorFormatter using the shared
    arbiter processor chain) to <evidence_dir>/run.log.jsonl. All `structlog.get_logger(...)`
    and stdlib `logging.getLogger("arbiter.*")` calls emitted during the test are captured.

    On teardown the handler is removed and closed; prior logging config is restored.
    This implements RESEARCH.md Pattern 5 point 2.
    """
    scenario = request.node.name
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    directory = pathlib.Path("evidence/04") / f"{scenario}_{timestamp}"
    directory.mkdir(parents=True, exist_ok=True)

    jsonl_path = directory / "run.log.jsonl"
    formatter = ProcessorFormatter(
        foreign_pre_chain=SHARED_PROCESSORS,
        processors=[
            ProcessorFormatter.remove_processors_meta,
            structlog.processors.JSONRenderer(),
        ],
    )
    file_handler = logging.FileHandler(jsonl_path, encoding="utf-8")
    file_handler.setFormatter(formatter)
    file_handler.setLevel(logging.DEBUG)

    # Attach handler to the "arbiter" namespace so we capture every
    # `logging.getLogger("arbiter.*")` and `structlog.get_logger("arbiter.*")`
    # event for the duration of this test, without touching non-arbiter loggers.
    arbiter_logger = logging.getLogger("arbiter")
    prior_level = arbiter_logger.level
    arbiter_logger.addHandler(file_handler)
    if prior_level == logging.NOTSET or prior_level > logging.DEBUG:
        arbiter_logger.setLevel(logging.DEBUG)

    try:
        yield directory
    finally:
        arbiter_logger.removeHandler(file_handler)
        file_handler.close()
        # Restore the prior effective level so non-sandbox tests see no leaked state.
        arbiter_logger.setLevel(prior_level)


@pytest.fixture
async def balance_snapshot(sandbox_db_pool):
    """Factory: call `await snapshot()` to capture current per-platform balances via BalanceMonitor.

    Builds BalanceMonitor with REAL KalshiCollector + PolymarketCollector. Fail-fast if either
    collector cannot be constructed - TEST-03 requires actual balance fetches, not silent None
    placeholders. Forbidden: `object()` substitutes for collectors (would make check_balances()
    silently return empty dicts and defeat reconciliation).
    """
    from arbiter.collectors.kalshi import KalshiCollector
    from arbiter.collectors.polymarket import PolymarketCollector
    from arbiter.config.settings import load_config
    from arbiter.monitor.balance import BalanceMonitor
    from arbiter.utils.price_store import PriceStore

    cfg = load_config()

    # Real collectors - required for TEST-03 reconciliation to have actual pre/post data.
    # If either fails to construct, fail loudly: silent None-balance fallback is forbidden.
    try:
        price_store = PriceStore()
    except Exception as exc:  # pragma: no cover - setup error
        raise AssertionError(
            f"PHASE 4: could not build PriceStore for balance_snapshot fixture: {exc!r}"
        )

    try:
        kalshi_collector = KalshiCollector(cfg.kalshi, price_store)
    except Exception as exc:
        raise AssertionError(
            f"PHASE 4: real KalshiCollector construction failed in balance_snapshot fixture: {exc!r}. "
            f"Inspect KalshiCollector.__init__ signature; do NOT substitute object() - TEST-03 requires "
            f"actual fetch_balance() calls."
        )

    try:
        polymarket_collector = PolymarketCollector(cfg.polymarket, price_store)
    except Exception as exc:
        raise AssertionError(
            f"PHASE 4: real PolymarketCollector construction failed in balance_snapshot fixture: {exc!r}. "
            f"Inspect PolymarketCollector.__init__ signature; do NOT substitute object() - TEST-03 "
            f"requires actual fetch_balance() calls against the test wallet."
        )

    collectors = {
        "kalshi": kalshi_collector,
        "polymarket": polymarket_collector,
    }
    monitor = BalanceMonitor(cfg.alerts, collectors)

    async def snapshot():
        """Return dict {platform: {'balance': float, 'timestamp': float}} via BalanceMonitor.check_balances()."""
        snaps = await monitor.check_balances()
        result = {}
        for platform, snap in snaps.items():
            # snap is a BalanceSnapshot(platform, balance, timestamp, is_low)
            result[platform] = {
                "balance": float(snap.balance) if snap.balance is not None else None,
                "timestamp": float(snap.timestamp),
            }
        return result

    try:
        yield snapshot
    finally:
        # BalanceMonitor.stop() closes its TelegramNotifier session; collectors are GC'd.
        stop_fn = getattr(monitor, "stop", None)
        if stop_fn is not None:
            result = stop_fn()
            if hasattr(result, "__await__"):
                await result
