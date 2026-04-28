"""AutoExecutor — wires ArbitrageScanner opportunities into ExecutionEngine
with policy gates AND fresh-state pre-flight checks.

Policy decision order (first failing gate wins):
    1. AUTO_EXECUTE_ENABLED=false (global disable)     -> skip, log reason
    2. supervisor.is_armed                             -> skip, log reason
    3. opportunity.requires_manual                     -> skip, log reason
    4. mapping.allow_auto_trade is False / missing     -> skip, log reason
    5. mapping.status != "confirmed" (when required)   -> skip, log reason
    6. duplicate opportunity (canonical_id + ts bucket)-> skip, log reason
    7. notional > MAX_POSITION_USD                     -> skip (or clamp qty)
    8. PRE-FLIGHT (when price_store + adapters wired):
       a. cached quote age > max_quote_age_s           -> skip, log reason
       b. recomputed net_edge < min_edge_cents_preflight -> skip, log reason
       c. orderbook depth $ < min_depth_usd per side   -> skip, log reason
    9. bootstrap_counter >= PHASE5_BOOTSTRAP_TRADES    -> skip, log reason
    else -> engine.execute_opportunity(opp)

The engine performs its own pre-trade re-quote + depth check; the
AutoExecutor pre-flight is an EARLIER gate that rejects bad trades before
they ever reach the engine, with stricter thresholds (higher min edge,
explicit dollar-depth floor). This avoids spamming platforms with FOK
orders against thin books — every one of the 7 production failures we
observed was a Kalshi `fill_or_kill_insufficient_resting_volume`.

Failures in engine.execute_opportunity are caught + logged; the loop never dies.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field, replace
from typing import Awaitable, Callable, Dict, Optional

import structlog

from ..scanner.arbitrage import ArbitrageOpportunity, compute_fee

log = structlog.get_logger("arbiter.auto_executor")


@dataclass
class AutoExecutorConfig:
    enabled: bool = False
    max_position_usd: float = 10.0
    bootstrap_trades: Optional[int] = None  # None => no cap
    dedup_window_seconds: int = 5
    # Pre-flight thresholds (applied only when price_store + adapters wired).
    max_quote_age_s: float = 30.0
    min_depth_usd: float = 25.0
    min_edge_cents_preflight: float = 3.0
    require_mapping_confirmed: bool = False  # opt-in stricter mapping gate


@dataclass
class AutoExecutorStats:
    considered: int = 0
    executed: int = 0
    skipped_disabled: int = 0
    skipped_armed: int = 0
    skipped_requires_manual: int = 0
    skipped_not_allowed: int = 0
    skipped_mapping_unconfirmed: int = 0
    skipped_duplicate: int = 0
    skipped_over_cap: int = 0
    skipped_bootstrap_full: int = 0
    skipped_stale_quote: int = 0
    skipped_edge_collapsed: int = 0
    skipped_depth_low: int = 0
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
            "skipped_mapping_unconfirmed": self.skipped_mapping_unconfirmed,
            "skipped_duplicate": self.skipped_duplicate,
            "skipped_over_cap": self.skipped_over_cap,
            "skipped_bootstrap_full": self.skipped_bootstrap_full,
            "skipped_stale_quote": self.skipped_stale_quote,
            "skipped_edge_collapsed": self.skipped_edge_collapsed,
            "skipped_depth_low": self.skipped_depth_low,
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
        price_store=None,
        adapters_provider: Optional[Callable[[], Dict[str, object]]] = None,
    ):
        self._scanner = scanner
        self._engine = engine
        self._supervisor = supervisor
        self._mapping_store = mapping_store
        self._config = config
        # Pre-flight dependencies — when None, pre-flight checks are skipped
        # (the engine still performs its own re-quote + depth check).
        self._price_store = price_store
        self._adapters_provider = adapters_provider
        self._queue: asyncio.Queue = scanner.subscribe()
        self._task: Optional[asyncio.Task] = None
        self._running = False
        self._seen_dedup_keys: dict[str, float] = {}
        self._failed_cooldown: dict[str, float] = {}  # canonical_id -> cooldown_until
        self._failed_count: dict[str, int] = {}  # canonical_id -> consecutive failure count
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

        if self._config.require_mapping_confirmed:
            mapping_status = str(getattr(mapping, "status", "")).lower()
            if mapping_status != "confirmed":
                self.stats.skipped_mapping_unconfirmed += 1
                log.info(
                    "auto_executor.skip.mapping_unconfirmed",
                    canonical_id=opp.canonical_id,
                    mapping_status=mapping_status,
                )
                return

        # Cooldown after failed fill-or-kill (avoid spamming thin orderbooks)
        cooldown_until = self._failed_cooldown.get(opp.canonical_id, 0.0)
        if now < cooldown_until:
            log.info(
                "auto_executor.skip.failed_cooldown",
                canonical_id=opp.canonical_id,
                remaining=round(cooldown_until - now, 1),
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
            # Scale qty down to fit within the per-trade cap instead of skipping
            # Use pair cost (yes + no) to match auditor's _compute_position_size
            price = float(opp.yes_price or 0.01) + float(opp.no_price or 0.01)
            clamped_qty = max(1, int(self._config.max_position_usd / price))
            # ── Recompute per-unit fees for the clamped quantity ───────
            # Fee functions have order-level ceil rounding, so per-unit
            # cost changes with quantity.  We must recompute net_edge
            # with the actual qty we will trade.
            from ..scanner.arbitrage import compute_fee
            yes_fee_new = compute_fee(
                opp.yes_platform, opp.yes_price, clamped_qty,
                fee_rate=opp.yes_fee_rate,
            )
            no_fee_new = compute_fee(
                opp.no_platform, opp.no_price, clamped_qty,
                fee_rate=opp.no_fee_rate,
            )
            new_total_fees = (yes_fee_new + no_fee_new) / max(clamped_qty, 1)
            new_net_edge = opp.gross_edge - new_total_fees
            log.info(
                "auto_executor.clamp_qty",
                canonical_id=opp.canonical_id,
                original_qty=opp.suggested_qty,
                clamped_qty=clamped_qty,
                notional=round(price * clamped_qty, 2),
                cap=self._config.max_position_usd,
                old_net_edge=round(opp.net_edge, 4),
                new_net_edge=round(new_net_edge, 4),
            )
            opp.suggested_qty = clamped_qty
            opp.net_edge = new_net_edge
            opp.net_edge_cents = new_net_edge * 100.0
            opp.total_fees = new_total_fees
            opp.yes_fee = yes_fee_new / max(clamped_qty, 1)
            opp.no_fee = no_fee_new / max(clamped_qty, 1)
            opp.max_profit_usd = round(new_net_edge * clamped_qty, 4)
            # If recomputed edge is now negative, skip this opportunity
            if new_net_edge <= 0:
                self.stats.skipped_over_cap += 1
                log.info(
                    "auto_executor.skip.negative_after_clamp",
                    canonical_id=opp.canonical_id,
                    new_net_edge=round(new_net_edge, 4),
                )
                return

        # Pre-flight: fresh quotes, recomputed edge, and dollar-depth gate.
        # No-op when price_store is None (engine still re-quotes + checks depth).
        opp = await self._pre_flight_checks(opp)
        if opp is None:
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

        if result is None or getattr(result, "status", "") == "failed":
            # Exponential backoff: 5m, 10m, 20m, 40m, capped at 60m
            count = self._failed_count.get(opp.canonical_id, 0) + 1
            self._failed_count[opp.canonical_id] = count
            backoff_s = min(300.0 * (2 ** (count - 1)), 3600.0)
            self._failed_cooldown[opp.canonical_id] = time.time() + backoff_s
            log.info(
                "auto_executor.cooldown.set",
                canonical_id=opp.canonical_id,
                attempt=count,
                backoff_minutes=round(backoff_s / 60, 1),
            )
        if result is not None:
            self.stats.executed += 1
            self.stats.last_executed_ts = time.time()
            # Reset failure counter on successful execution
            if getattr(result, "status", "") in ("filled", "submitted"):
                self._failed_count.pop(opp.canonical_id, None)
                self._failed_cooldown.pop(opp.canonical_id, None)
            log.info(
                "auto_executor.execute.complete",
                canonical_id=opp.canonical_id,
                arb_id=getattr(result, "arb_id", None),
                realized_pnl=getattr(result, "realized_pnl", None),
            )

    # ─── pre-flight ───────────────────────────────────────────────────────

    async def _pre_flight_checks(
        self, opp: ArbitrageOpportunity
    ) -> Optional[ArbitrageOpportunity]:
        """Fresh-state checks before submitting a trade to the engine.

        Returns the (possibly re-quoted) opportunity to execute, or None to
        skip. No-op when ``price_store`` is None (legacy / unit-test path);
        the engine still performs its own pre-trade re-quote and depth check.
        """
        if self._price_store is None:
            return opp

        # 1. Fetch fresh quotes for both legs.
        try:
            current_yes = await self._price_store.get(
                opp.yes_platform, opp.canonical_id
            )
            current_no = await self._price_store.get(
                opp.no_platform, opp.canonical_id
            )
        except Exception as exc:  # noqa: BLE001
            self.stats.skipped_stale_quote += 1
            log.warning(
                "auto_executor.skip.preflight.price_store_error",
                canonical_id=opp.canonical_id,
                err=str(exc),
            )
            return None

        if not current_yes or not current_no:
            self.stats.skipped_stale_quote += 1
            log.info(
                "auto_executor.skip.preflight.no_fresh_quotes",
                canonical_id=opp.canonical_id,
                has_yes=bool(current_yes),
                has_no=bool(current_no),
            )
            return None

        age_yes = float(getattr(current_yes, "age_seconds", 0.0))
        age_no = float(getattr(current_no, "age_seconds", 0.0))
        max_age = max(age_yes, age_no)
        if max_age > self._config.max_quote_age_s:
            self.stats.skipped_stale_quote += 1
            log.info(
                "auto_executor.skip.preflight.stale_quote",
                canonical_id=opp.canonical_id,
                max_age_s=round(max_age, 2),
                threshold_s=self._config.max_quote_age_s,
                yes_age_s=round(age_yes, 2),
                no_age_s=round(age_no, 2),
            )
            return None

        # 2. Recompute net edge with fresh prices + venue-aware fees.
        yes_price = float(current_yes.yes_price)
        no_price = float(current_no.no_price)
        qty = max(int(opp.suggested_qty or 1), 1)
        gross_edge = 1.0 - yes_price - no_price
        yes_fee_rate = float(getattr(current_yes, "fee_rate", 0.0))
        no_fee_rate = float(getattr(current_no, "fee_rate", 0.0))
        total_fees_per_unit = (
            compute_fee(opp.yes_platform, yes_price, qty, yes_fee_rate)
            + compute_fee(opp.no_platform, no_price, qty, no_fee_rate)
        ) / qty
        net_edge = gross_edge - total_fees_per_unit
        net_edge_cents = net_edge * 100.0
        if net_edge_cents < self._config.min_edge_cents_preflight:
            self.stats.skipped_edge_collapsed += 1
            log.info(
                "auto_executor.skip.preflight.edge_collapsed",
                canonical_id=opp.canonical_id,
                cached_net_edge_cents=round(opp.net_edge_cents, 2),
                fresh_net_edge_cents=round(net_edge_cents, 2),
                threshold_cents=self._config.min_edge_cents_preflight,
            )
            return None

        # 3. Orderbook depth in dollars per side (skip when adapters unavailable).
        adapters: Dict[str, object] = {}
        if self._adapters_provider is not None:
            try:
                adapters = self._adapters_provider() or {}
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "auto_executor.preflight.adapters_provider_error",
                    canonical_id=opp.canonical_id,
                    err=str(exc),
                )
                adapters = {}

        for side, platform, market_id, price in (
            ("yes", opp.yes_platform, opp.yes_market_id, yes_price),
            ("no", opp.no_platform, opp.no_market_id, no_price),
        ):
            adapter = adapters.get(platform)
            if adapter is None or not hasattr(adapter, "check_depth"):
                continue
            required_qty_for_depth = max(
                qty,
                int((self._config.min_depth_usd / max(price, 0.01)) + 0.5),
            )
            try:
                sufficient, best_price = await adapter.check_depth(
                    market_id, side, required_qty_for_depth,
                )
            except Exception as exc:  # noqa: BLE001
                self.stats.skipped_depth_low += 1
                log.warning(
                    "auto_executor.skip.preflight.depth_check_error",
                    canonical_id=opp.canonical_id,
                    platform=platform,
                    side=side,
                    err=str(exc),
                )
                return None
            if not sufficient:
                self.stats.skipped_depth_low += 1
                log.info(
                    "auto_executor.skip.preflight.depth_low",
                    canonical_id=opp.canonical_id,
                    platform=platform,
                    side=side,
                    required_qty=required_qty_for_depth,
                    min_depth_usd=self._config.min_depth_usd,
                    best_price=best_price,
                )
                return None

        # All pre-flight checks passed — return a re-quoted opportunity so the
        # engine does not have to re-fetch (it will still do its own re-quote
        # to confirm freshness at submission time).
        return replace(
            opp,
            yes_price=yes_price,
            no_price=no_price,
            yes_market_id=getattr(current_yes, "yes_market_id", "") or opp.yes_market_id,
            no_market_id=getattr(current_no, "no_market_id", "") or opp.no_market_id,
            gross_edge=gross_edge,
            total_fees=total_fees_per_unit,
            net_edge=net_edge,
            net_edge_cents=net_edge_cents,
            quote_age_seconds=max_age,
            timestamp=time.time(),
            yes_fee_rate=yes_fee_rate,
            no_fee_rate=no_fee_rate,
        )

    # ─── helpers ──────────────────────────────────────────────────────────

    def _estimate_notional(self, opp: ArbitrageOpportunity) -> float:
        """Pair-cost notional = (yes_price + no_price) * suggested_qty.

        Uses total pair cost to match the auditor's _compute_position_size
        calculation, ensuring consistent qty sizing across the system.
        """
        qty = max(1, int(opp.suggested_qty or 1))
        price = float(opp.yes_price or 0.0) + float(opp.no_price or 0.0)
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
    price_store=None,
    adapters_provider: Optional[Callable[[], Dict[str, object]]] = None,
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
        max_quote_age_s=_float(config_env.get("PREFLIGHT_MAX_QUOTE_AGE_S"), 30.0),
        min_depth_usd=_float(config_env.get("PREFLIGHT_MIN_DEPTH_USD"), 25.0),
        min_edge_cents_preflight=_float(
            config_env.get("PREFLIGHT_MIN_EDGE_CENTS"), 3.0,
        ),
        require_mapping_confirmed=_bool(
            config_env.get("PREFLIGHT_REQUIRE_MAPPING_CONFIRMED"), default=False,
        ),
    )
    return AutoExecutor(
        scanner=scanner,
        engine=engine,
        supervisor=supervisor,
        mapping_store=mapping_store,
        config=cfg,
        price_store=price_store,
        adapters_provider=adapters_provider,
    )
