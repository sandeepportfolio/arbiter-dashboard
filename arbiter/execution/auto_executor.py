"""AutoExecutor — wires ArbitrageScanner opportunities into ExecutionEngine
with five policy gates: allow_auto_trade (mapping-level), PHASE5_BOOTSTRAP_TRADES
(rollout cap), MAX_POSITION_USD (notional cap), SafetySupervisor.is_armed
(kill-switch), and idempotency (duplicate opportunity dedup).

This closes the loop from "scanner emits opportunity" to "engine places the
trade" without requiring operator approval per trade. It is the Phase 6 Plan
06-01 deliverable.

Policy decision order (first failing gate wins):
    1. AUTO_EXECUTE_ENABLED=false (global disable)     -> skip, log reason
    2. supervisor.is_armed                             -> skip, log reason
    3. opportunity.requires_manual                     -> skip, log reason
    4. mapping.allow_auto_trade is False / missing     -> skip, log reason
    5. duplicate opportunity (canonical_id + ts bucket)-> skip, log reason
    6. notional > MAX_POSITION_USD                     -> skip, log reason
    7. bootstrap_counter >= PHASE5_BOOTSTRAP_TRADES    -> skip, log reason
    else -> engine.execute_opportunity(opp)

Failures in engine.execute_opportunity are caught + logged; the loop never dies.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional

import structlog

from ..scanner.arbitrage import ArbitrageOpportunity

log = structlog.get_logger("arbiter.auto_executor")


@dataclass
class AutoExecutorConfig:
    enabled: bool = False
    max_position_usd: float = 10.0
    bootstrap_trades: Optional[int] = None  # None => no cap
    dedup_window_seconds: int = 5


@dataclass
class AutoExecutorStats:
    considered: int = 0
    executed: int = 0
    skipped_disabled: int = 0
    skipped_armed: int = 0
    skipped_requires_manual: int = 0
    skipped_not_allowed: int = 0
    skipped_duplicate: int = 0
    skipped_over_cap: int = 0
    skipped_bootstrap_full: int = 0
    failures: int = 0
    last_considered_ts: float = 0.0
    last_executed_ts: float = 0.0

    def to_dict(self) -> dict:
        return {
            "considered": self.considered,
            "executed": self.executed,
            "skipped_disabled": self.skipped_disabled,
            "skipped_armed": self.skipped_armed,
            "skipped_requires_manual": self.skipped_requires_manual,
            "skipped_not_allowed": self.skipped_not_allowed,
            "skipped_duplicate": self.skipped_duplicate,
            "skipped_over_cap": self.skipped_over_cap,
            "skipped_bootstrap_full": self.skipped_bootstrap_full,
            "failures": self.failures,
            "last_considered_ts": self.last_considered_ts,
            "last_executed_ts": self.last_executed_ts,
        }


class AutoExecutor:
    """Consume opportunities from ArbitrageScanner and execute within policy."""

    def __init__(
        self,
        *,
        scanner,
        engine,
        supervisor,
        mapping_store,
        config: AutoExecutorConfig,
    ):
        self._scanner = scanner
        self._engine = engine
        self._supervisor = supervisor
        self._mapping_store = mapping_store
        self._config = config
        self._queue: asyncio.Queue = scanner.subscribe()
        self._task: Optional[asyncio.Task] = None
        self._running = False
        self._seen_dedup_keys: dict[str, float] = {}
        self.stats = AutoExecutorStats()

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._run_loop(), name="auto_executor")
        log.info(
            "auto_executor.started",
            enabled=self._config.enabled,
            max_position_usd=self._config.max_position_usd,
            bootstrap_trades=self._config.bootstrap_trades,
        )

    async def stop(self) -> None:
        if not self._running:
            return
        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        log.info("auto_executor.stopped", stats=self.stats.to_dict())

    async def _run_loop(self) -> None:
        while self._running:
            try:
                opp = await self._queue.get()
            except asyncio.CancelledError:
                return
            try:
                await self._consider_opportunity(opp)
            except Exception as exc:  # noqa: BLE001 — loop must not die
                self.stats.failures += 1
                log.error("auto_executor.loop.unexpected_error", err=str(exc))

    async def _consider_opportunity(self, opp: ArbitrageOpportunity) -> None:
        """Apply all gates and either execute or log-skip."""
        now = time.time()
        self.stats.considered += 1
        self.stats.last_considered_ts = now

        if not self._config.enabled:
            self.stats.skipped_disabled += 1
            log.info(
                "auto_executor.skip.disabled",
                canonical_id=opp.canonical_id,
                reason="AUTO_EXECUTE_ENABLED=false",
            )
            return

        if self._supervisor.is_armed:
            self.stats.skipped_armed += 1
            log.info(
                "auto_executor.skip.armed",
                canonical_id=opp.canonical_id,
                armed_by=getattr(self._supervisor, "armed_by", None),
            )
            return

        if opp.requires_manual:
            self.stats.skipped_requires_manual += 1
            log.info(
                "auto_executor.skip.requires_manual",
                canonical_id=opp.canonical_id,
                mapping_status=opp.mapping_status,
            )
            return

        mapping = await self._mapping_store.get(opp.canonical_id)
        if mapping is None or not getattr(mapping, "allow_auto_trade", False):
            self.stats.skipped_not_allowed += 1
            log.info(
                "auto_executor.skip.not_allowed",
                canonical_id=opp.canonical_id,
                has_mapping=mapping is not None,
            )
            return

        dedup_key = self._dedup_key(opp, now)
        if dedup_key in self._seen_dedup_keys:
            self.stats.skipped_duplicate += 1
            log.info(
                "auto_executor.skip.duplicate",
                canonical_id=opp.canonical_id,
                dedup_key=dedup_key,
            )
            return
        self._record_dedup(dedup_key, now)

        notional = self._estimate_notional(opp)
        if notional > self._config.max_position_usd:
            self.stats.skipped_over_cap += 1
            log.info(
                "auto_executor.skip.over_cap",
                canonical_id=opp.canonical_id,
                notional=round(notional, 2),
                cap=self._config.max_position_usd,
            )
            return

        if (
            self._config.bootstrap_trades is not None
            and self.stats.executed >= self._config.bootstrap_trades
        ):
            self.stats.skipped_bootstrap_full += 1
            log.info(
                "auto_executor.skip.bootstrap_full",
                canonical_id=opp.canonical_id,
                budget=self._config.bootstrap_trades,
                executed=self.stats.executed,
            )
            return

        log.info(
            "auto_executor.execute.begin",
            canonical_id=opp.canonical_id,
            notional=round(notional, 2),
            net_edge_cents=opp.net_edge_cents,
        )
        try:
            result = await self._engine.execute_opportunity(opp)
        except Exception as exc:  # noqa: BLE001
            self.stats.failures += 1
            log.error(
                "auto_executor.execute.failed",
                canonical_id=opp.canonical_id,
                err=str(exc),
            )
            return

        if result is not None:
            self.stats.executed += 1
            self.stats.last_executed_ts = time.time()
            log.info(
                "auto_executor.execute.complete",
                canonical_id=opp.canonical_id,
                arb_id=getattr(result, "arb_id", None),
                realized_pnl=getattr(result, "realized_pnl", None),
            )

    # ─── helpers ──────────────────────────────────────────────────────────

    def _estimate_notional(self, opp: ArbitrageOpportunity) -> float:
        """Per-leg notional = max(yes_price, no_price) * suggested_qty.

        The worst-case leg cost is the more expensive side; we cap on the
        larger-notional leg to ensure MAX_POSITION_USD is never exceeded on
        either platform.
        """
        qty = max(1, int(opp.suggested_qty or 1))
        price = max(float(opp.yes_price or 0.0), float(opp.no_price or 0.0))
        return price * qty

    def _dedup_key(self, opp: ArbitrageOpportunity, now: float) -> str:
        """Bucket opportunities into per-window slots so a scanner re-emit
        within dedup_window_seconds for the same market does not double-fire.
        """
        bucket = int(now // max(1, self._config.dedup_window_seconds))
        return f"{opp.canonical_id}:{opp.yes_platform}:{opp.no_platform}:{bucket}"

    def _record_dedup(self, key: str, ts: float) -> None:
        cutoff = ts - self._config.dedup_window_seconds * 10
        self._seen_dedup_keys = {
            k: t for k, t in self._seen_dedup_keys.items() if t >= cutoff
        }
        self._seen_dedup_keys[key] = ts


class _SettingsMappingAdapter:
    """Adapter that presents settings.MARKET_MAP with an async ``get()`` API.

    Wraps the in-memory ``Dict[canonical_id, dict]`` defined in
    ``arbiter.config.settings``. The returned object exposes
    ``allow_auto_trade`` as an attribute so AutoExecutor can use the same
    protocol regardless of whether the backing store is the in-process dict
    or the DB-backed MarketMappingStore.
    """

    def __init__(self, market_map_dict: dict):
        self._market_map = market_map_dict

    async def get(self, canonical_id: str):
        entry = self._market_map.get(canonical_id)
        if entry is None:
            return None
        # entry is a plain dict; wrap it so `.allow_auto_trade` is an attribute.
        return _MappingView(
            canonical_id=canonical_id,
            allow_auto_trade=bool(entry.get("allow_auto_trade", False)),
            status=str(entry.get("status", "candidate")),
            resolution_match_status=str(
                entry.get("resolution_match_status", "pending_operator_review")
            ),
        )


@dataclass
class _MappingView:
    canonical_id: str
    allow_auto_trade: bool
    status: str
    resolution_match_status: str


def make_settings_mapping_adapter(market_map_dict: dict) -> _SettingsMappingAdapter:
    """Factory callable for arbiter.main — returns an AutoExecutor-compatible
    mapping store backed by the in-memory settings.MARKET_MAP dict.
    """
    return _SettingsMappingAdapter(market_map_dict)


def make_auto_executor_from_env(
    *,
    scanner,
    engine,
    supervisor,
    mapping_store,
    config_env: dict,
) -> AutoExecutor:
    """Factory that reads env-style overrides without pulling in Pydantic."""
    def _bool(value: Optional[str], default: bool = False) -> bool:
        if value is None:
            return default
        return str(value).strip().lower() in ("1", "true", "yes", "on")

    def _maybe_int(value: Optional[str]) -> Optional[int]:
        if value is None or str(value).strip() == "":
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    def _float(value: Optional[str], default: float) -> float:
        try:
            return float(value) if value is not None else default
        except (TypeError, ValueError):
            return default

    cfg = AutoExecutorConfig(
        enabled=_bool(config_env.get("AUTO_EXECUTE_ENABLED"), default=False),
        max_position_usd=_float(config_env.get("MAX_POSITION_USD"), 10.0),
        bootstrap_trades=_maybe_int(config_env.get("PHASE5_BOOTSTRAP_TRADES")),
    )
    return AutoExecutor(
        scanner=scanner,
        engine=engine,
        supervisor=supervisor,
        mapping_store=mapping_store,
        config=cfg,
    )
