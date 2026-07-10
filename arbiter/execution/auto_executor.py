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
    min_edge_cents_preflight: float = 7.0
    # PROFITABILITY-01 (2026-05-27): per-venue-pair preflight floors so
    # FX-containing pairs (lower fees) can clear a smaller threshold
    # than the K×P-calibrated default. Keyed alphabetically:
    # "forecastex_kalshi", "forecastex_polymarket", "kalshi_polymarket".
    # Falls back to ``min_edge_cents_preflight`` when no entry matches.
    min_edge_cents_preflight_by_pair: dict = field(default_factory=dict)
    # Fail-closed default: unconfirmed mappings are EXCLUDED unless an operator
    # explicitly opts in by setting PREFLIGHT_REQUIRE_MAPPING_CONFIRMED=false.
    # Confirmed mappings have been audited end-to-end; unconfirmed ones are
    # exactly the candidates that drove the 2026-05-08 cascade losses.
    require_mapping_confirmed: bool = True
    # Suspicious-edge circuit breaker (2026-07-10 party-swap incident). A
    # coarse ABSURDITY backstop, NOT the primary defense — the mapping-level
    # coherence sweep (persistence + same-side cross-venue divergence) is what
    # actually catches crossed contracts. For a two-leg arb the edge equals
    # the cross-venue divergence, so this can't finely separate a moderate
    # phantom from a fat real arb; it must sit ABOVE the legitimate range.
    # A real K×P arb clears the 7c NET preflight floor => ~9.5c gross, and
    # genuine dislocations run higher, so the ceiling is 15c: it never blocks
    # normal trading but stops a catastrophically-wrong mapping (a 20c+
    # "edge") from auto-firing while the sweep catches up. Routes to operator
    # review. Env: MAX_AUTO_GROSS_EDGE_CENTS.
    max_auto_gross_edge_cents: float = 15.0
    # Auto-disable a mapping after this many consecutive losing recoveries.
    # Catches markets like DEM_HOUSE_2026 (2026-05-08 cascade: 22 trades /
    # -$105.80) where the secondary venue persistently rejects orders and
    # every primary fill turns into a forced unwind.
    loss_streak_disable_threshold: int = 3
    # When True, insufficient orderbook depth in pre-flight halves the
    # try_qty until depth is sufficient (or qty drops below 1, which skips).
    # When False (legacy), insufficient depth at the desired qty skips the
    # trade entirely. Engine-side depth gate already does adaptive halving;
    # this aligns pre-flight with that behaviour so partial-depth books
    # produce smaller trades instead of dropped trades.
    liquidity_adaptive_sizing: bool = True

    def edge_floor_for(self, yes_platform: str, no_platform: str) -> float:
        """Return the preflight edge floor (cents) for a given venue
        pair. Pair-specific entries can RELAX the global floor for
        known-lower-fee combinations (FX) but never raise it above
        the operator-set global ceiling. Order-independent."""
        a = (yes_platform or "").strip().lower()
        b = (no_platform or "").strip().lower()
        global_floor = float(self.min_edge_cents_preflight)
        if not a or not b or a == b:
            return global_floor
        key = "_".join(sorted([a, b]))
        pair_floor = self.min_edge_cents_preflight_by_pair.get(key)
        if pair_floor is None:
            return global_floor
        return float(min(global_floor, float(pair_floor)))



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
    skipped_suspicious_edge: int = 0
    skipped_depth_low: int = 0
    skipped_failed_cooldown: int = 0
    skipped_failure_pattern: int = 0
    skipped_forecastex_unavailable: int = 0
    skipped_gate_structural: int = 0
    auto_disabled_loss_streak: int = 0
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
            "skipped_suspicious_edge": self.skipped_suspicious_edge,
            "skipped_depth_low": self.skipped_depth_low,
            "skipped_failed_cooldown": self.skipped_failed_cooldown,
            "skipped_failure_pattern": self.skipped_failure_pattern,
            "skipped_forecastex_unavailable": self.skipped_forecastex_unavailable,
            "skipped_gate_structural": self.skipped_gate_structural,
            "auto_disabled_loss_streak": self.auto_disabled_loss_streak,
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
        failure_tracker=None,
        opportunity_queue: Optional[asyncio.Queue] = None,
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
        self._failure_tracker = failure_tracker
        self._queue: asyncio.Queue = opportunity_queue if opportunity_queue is not None else scanner.subscribe()
        self._task: Optional[asyncio.Task] = None
        self._running = False
        self._seen_dedup_keys: dict[str, float] = {}
        self._failed_cooldown: dict[str, float] = {}  # canonical_id -> cooldown_until
        self._failed_count: dict[str, int] = {}  # canonical_id -> consecutive failure count
        self._loss_streak: dict[str, int] = {}  # canonical_id -> consecutive losing trades
        # Structural-deny cooldown (CV-03, 2026-06-05): trade-gate denials
        # for reasons like "Platform profitability for X is not_profitable"
        # are NOT execution failures — they're operator-controlled veto
        # decisions that won't change until the operator acts. Track these
        # separately so we don't burn the exponential failure cooldown
        # (which grew to 60 min after 20 attempts on DEM_SENATE_2026)
        # and instead apply a flat suppression window per pair-and-reason.
        # Keyed by (canonical_id, reason) to a Unix expiry timestamp.
        self._gate_structural_cooldown: dict[tuple[str, str], float] = {}
        self._gate_structural_cooldown_s: float = 1800.0  # 30 minutes
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

        # ForecastEx-leg safety gate: if either leg trades on ForecastEx and
        # the mapping is negative-cached as forecastex_not_available OR has
        # an empty/zero forecastex_contract_id, refuse the trade. The
        # discovery resolver fingerprint matching can put unrelated markets
        # (e.g. NFL Eagles ↔ Philadelphia CPI) on a shared parent conid; the
        # FX collector skips those by not emitting prices, but if a stale
        # price slips through the scanner we must not place an order on the
        # wrong contract.
        involves_fx = (
            getattr(opp, "yes_platform", "") == "forecastex"
            or getattr(opp, "no_platform", "") == "forecastex"
        )
        if involves_fx:
            fx_unavailable = bool(
                getattr(mapping, "forecastex_not_available", False)
            )
            fx_conid = str(
                getattr(mapping, "forecastex_contract_id", "") or ""
            ).strip()
            if fx_unavailable or not fx_conid or fx_conid == "0":
                self.stats.skipped_forecastex_unavailable += 1
                log.warning(
                    "auto_executor.skip.forecastex_unavailable",
                    canonical_id=opp.canonical_id,
                    fx_unavailable=fx_unavailable,
                    fx_conid=fx_conid,
                    yes_platform=getattr(opp, "yes_platform", ""),
                    no_platform=getattr(opp, "no_platform", ""),
                )
                return
            # NO-side conid requirement (phantom-trade postmortem 2026-05-28).
            # ForecastEx binary contracts have SEPARATE conids for YES and NO.
            # When ForecastEx is the NO platform for this opp, the opportunity
            # MUST carry a NO conid distinct from the YES conid; otherwise the
            # executor would route the NO order to the YES contract — which
            # was the root cause of the ARB-000695/699 phantom-trade incident.
            no_is_fx = getattr(opp, "no_platform", "") == "forecastex"
            yes_is_fx = getattr(opp, "yes_platform", "") == "forecastex"
            opp_no_market_id = str(getattr(opp, "no_market_id", "") or "").strip()
            opp_yes_market_id = str(getattr(opp, "yes_market_id", "") or "").strip()
            if no_is_fx:
                if not opp_no_market_id or opp_no_market_id in ("0", "None"):
                    self.stats.skipped_forecastex_unavailable += 1
                    log.warning(
                        "auto_executor.skip.forecastex_no_conid_missing",
                        canonical_id=opp.canonical_id,
                        no_market_id=opp_no_market_id,
                        yes_market_id=opp_yes_market_id,
                    )
                    return
                # When BOTH legs route through ForecastEx (3-way scanner),
                # the YES and NO conids MUST differ. Same conid means the
                # collector failed NO-conid discovery and synthesized
                # against the YES conid.
                if yes_is_fx and opp_no_market_id == opp_yes_market_id:
                    self.stats.skipped_forecastex_unavailable += 1
                    log.error(
                        "auto_executor.skip.forecastex_conid_collision",
                        canonical_id=opp.canonical_id,
                        market_id=opp_yes_market_id,
                    )
                    return

        if self._config.require_mapping_confirmed:
            # MappingStatus is a (str, Enum); str(enum) returns "MappingStatus.CONFIRMED"
            # in Python 3.11+, not the value. Unwrap via .value when present so the
            # comparison below actually matches "confirmed".
            raw_status = getattr(mapping, "status", "")
            mapping_status = str(getattr(raw_status, "value", raw_status)).lower()
            if mapping_status != "confirmed":
                self.stats.skipped_mapping_unconfirmed += 1
                log.info(
                    "auto_executor.skip.mapping_unconfirmed",
                    canonical_id=opp.canonical_id,
                    mapping_status=mapping_status,
                )
                return

        # Suspicious-edge circuit breaker (2026-07-10 party-swap incident).
        # A too-good gross edge is, for a two-leg arb, indistinguishable from
        # a crossed-contract mapping (the divergence IS the edge). Route it
        # to operator review instead of auto-executing — closes the window
        # between a mapping going bad and the coherence sweep quarantining it.
        max_gross_edge_c = float(
            getattr(self._config, "max_auto_gross_edge_cents", 8.0)
        )
        gross_edge_c = float(getattr(opp, "gross_edge", 0.0) or 0.0) * 100.0
        if max_gross_edge_c > 0 and gross_edge_c > max_gross_edge_c:
            self.stats.skipped_suspicious_edge += 1
            log.critical(
                "auto_executor.skip.suspicious_edge",
                canonical_id=opp.canonical_id,
                gross_edge_cents=round(gross_edge_c, 2),
                ceiling_cents=max_gross_edge_c,
                reason=(
                    "gross edge exceeds ceiling — likely a wrong-contract "
                    "mapping (cross-venue divergence = edge for a 2-leg arb); "
                    "routed to operator review"
                ),
            )
            return

        # Cooldown after failed fill-or-kill (avoid spamming thin orderbooks)
        cooldown_until = self._failed_cooldown.get(opp.canonical_id, 0.0)
        if now < cooldown_until:
            self.stats.skipped_failed_cooldown += 1
            log.info(
                "auto_executor.skip.failed_cooldown",
                canonical_id=opp.canonical_id,
                remaining=round(cooldown_until - now, 1),
            )
            return

        # Failure-pattern backoff: if (canonical, side, price-bucket) has
        # tipped over the threshold in the trailing hour, skip both legs.
        if self._failure_tracker is not None:
            blocked_reason = ""
            for side_name, price in (
                ("yes", float(getattr(opp, "yes_price", 0.0) or 0.0)),
                ("no", float(getattr(opp, "no_price", 0.0) or 0.0)),
            ):
                try:
                    skip, reason = self._failure_tracker.should_skip(
                        opp.canonical_id, side_name, price,
                    )
                except Exception as exc:  # noqa: BLE001 — tracker must not break trading
                    log.warning(
                        "auto_executor.failure_tracker_error",
                        canonical_id=opp.canonical_id,
                        err=str(exc),
                    )
                    skip, reason = False, ""
                if skip:
                    blocked_reason = f"{side_name}: {reason}"
                    break
            if blocked_reason:
                self.stats.skipped_failure_pattern += 1
                log.info(
                    "auto_executor.skip.failure_pattern",
                    canonical_id=opp.canonical_id,
                    reason=blocked_reason,
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
            # If even a single contract exceeds max_position_usd, skip rather
            # than force-place 1 contract and breach the cap. Previously
            # ``max(1, int(...))`` would floor to 1 when pair cost > cap and
            # silently violate the per-trade USD ceiling.
            raw_qty = int(self._config.max_position_usd / price)
            if raw_qty < 1:
                self.stats.skipped_over_cap += 1
                log.info(
                    "auto_executor.skip.pair_cost_exceeds_cap",
                    canonical_id=opp.canonical_id,
                    pair_cost=round(price, 4),
                    cap=self._config.max_position_usd,
                )
                return
            clamped_qty = raw_qty
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
            # If recomputed edge falls below the executor pre-flight threshold,
            # skip here too. The price_store-backed pre-flight is optional in
            # tests/legacy wiring, so the clamp path must fail closed by itself.
            pair_floor = self._config.edge_floor_for(
                opp.yes_platform, opp.no_platform,
            )
            if opp.net_edge_cents < pair_floor:
                self.stats.skipped_over_cap += 1
                log.info(
                    "auto_executor.skip.edge_low_after_clamp",
                    canonical_id=opp.canonical_id,
                    new_net_edge=round(new_net_edge, 4),
                    new_net_edge_cents=round(opp.net_edge_cents, 2),
                    threshold_cents=pair_floor,
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

        # Trade-gate pre-check (CV-03, 2026-06-05): the engine will
        # silently return None if its trade-gate denies, which the
        # post-call handler can't distinguish from a real execution
        # failure (resulting in 20-attempt exponential backoff against a
        # deterministic operator-controlled veto). Probe the gate here
        # so structural denies — "Platform profitability for X is
        # not_profitable", kill-switch armed, low balance — become
        # flat-cooldown skips with their own stat, while only true
        # transient/execution failures grow the exponential failure
        # counter downstream.
        try:
            gate_check = getattr(self._engine, "check_trade_gate", None)
            if callable(gate_check):
                gate_allowed, gate_reason, _gate_context = await gate_check(opp)
            else:
                gate_allowed, gate_reason = True, ""
        except Exception as exc:  # noqa: BLE001 — must not block trading on probe error
            log.warning(
                "auto_executor.gate_precheck_error",
                canonical_id=opp.canonical_id,
                err=str(exc),
            )
            gate_allowed, gate_reason = True, ""
        if not gate_allowed:
            cooldown_key = (opp.canonical_id, gate_reason)
            cooldown_until = self._gate_structural_cooldown.get(cooldown_key, 0.0)
            now_ts = time.time()
            if now_ts < cooldown_until:
                self.stats.skipped_gate_structural += 1
                # Suppressed — no log spam; covered by the original entry.
                return
            self._gate_structural_cooldown[cooldown_key] = (
                now_ts + self._gate_structural_cooldown_s
            )
            self.stats.skipped_gate_structural += 1
            log.info(
                "auto_executor.skip.gate_structural",
                canonical_id=opp.canonical_id,
                reason=gate_reason,
                cooldown_minutes=round(self._gate_structural_cooldown_s / 60, 1),
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

        result_status = getattr(result, "status", "") if result is not None else ""
        result_pnl = float(getattr(result, "realized_pnl", 0.0) or 0.0) if result is not None else 0.0
        # "recovering" means one leg filled and the other was unwound — from a
        # system-correctness standpoint that is a failure even when the unwind
        # happens to capture profit. Before this fix only status="failed" or
        # result=None set cooldown, so the DEM_HOUSE_2026 cascade fired 22
        # times in 15 minutes despite the 5-min backoff.
        is_failure = result is None or result_status in ("failed", "recovering")
        is_clean_fill = result_status in ("filled", "submitted") and result_pnl >= 0
        if is_failure:
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
                result_status=result_status or "no_result",
                realized_pnl=result_pnl,
            )
        if result is not None:
            self.stats.executed += 1
            self.stats.last_executed_ts = time.time()
            if is_clean_fill:
                # Reset failure + loss-streak counters on a clean profitable fill
                self._failed_count.pop(opp.canonical_id, None)
                self._failed_cooldown.pop(opp.canonical_id, None)
                if result_pnl > 0:
                    self._loss_streak.pop(opp.canonical_id, None)
            if is_failure and result_pnl < 0:
                # Track consecutive losses per canonical and auto-disable when
                # we cross the threshold. This is the kill-switch the
                # DEM_HOUSE_2026 cascade needed.
                streak = self._loss_streak.get(opp.canonical_id, 0) + 1
                self._loss_streak[opp.canonical_id] = streak
                threshold = self._config.loss_streak_disable_threshold
                if threshold > 0 and streak >= threshold:
                    await self._auto_disable_mapping(
                        opp.canonical_id,
                        reason=f"auto-disabled after {streak} consecutive losing trades",
                    )
            log.info(
                "auto_executor.execute.complete",
                canonical_id=opp.canonical_id,
                arb_id=getattr(result, "arb_id", None),
                realized_pnl=result_pnl,
                status=result_status,
                loss_streak=self._loss_streak.get(opp.canonical_id, 0),
            )

    async def _auto_disable_mapping(self, canonical_id: str, *, reason: str) -> None:
        """Disable auto-trade on a mapping that's bleeding capital.

        Tries the mapping_store's ``disable_auto_trade(canonical_id, reason)``
        if it exposes one (DB-backed stores), and otherwise falls back to
        toggling ``allow_auto_trade`` on the cached mapping object so the
        in-memory adapter's next ``get()`` reflects the change.
        """
        log.warning(
            "auto_executor.mapping.auto_disabled",
            canonical_id=canonical_id,
            reason=reason,
        )
        self.stats.auto_disabled_loss_streak += 1
        # Reset the loss streak so a re-enabled mapping starts fresh
        self._loss_streak.pop(canonical_id, None)
        store_disable = getattr(self._mapping_store, "disable_auto_trade", None)
        if callable(store_disable):
            try:
                await store_disable(canonical_id, reason)
                return
            except Exception as exc:  # noqa: BLE001
                log.error(
                    "auto_executor.mapping.auto_disable_failed",
                    canonical_id=canonical_id,
                    err=str(exc),
                )
        # Fallback: mutate the cached mapping object so subsequent
        # ``mapping_store.get()`` calls see allow_auto_trade=False.
        try:
            cached = await self._mapping_store.get(canonical_id)
        except Exception:
            cached = None
        if cached is not None and hasattr(cached, "allow_auto_trade"):
            try:
                setattr(cached, "allow_auto_trade", False)
            except Exception:  # noqa: BLE001
                pass

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
        pair_floor = self._config.edge_floor_for(
            opp.yes_platform, opp.no_platform,
        )
        if net_edge_cents < pair_floor:
            self.stats.skipped_edge_collapsed += 1
            log.info(
                "auto_executor.skip.preflight.edge_collapsed",
                canonical_id=opp.canonical_id,
                cached_net_edge_cents=round(opp.net_edge_cents, 2),
                fresh_net_edge_cents=round(net_edge_cents, 2),
                threshold_cents=pair_floor,
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

        # Track the smallest qty the book can absorb across both sides when
        # adaptive sizing is on. Both legs must support the final qty.
        adaptive_qty = qty
        for side, platform, market_id, price in (
            ("yes", opp.yes_platform, opp.yes_market_id, yes_price),
            ("no", opp.no_platform, opp.no_market_id, no_price),
        ):
            adapter = adapters.get(platform)
            if adapter is None or not hasattr(adapter, "check_depth"):
                # H16: depth check is the only signal we have for "is the
                # secondary venue actually going to fill this size?". Silently
                # skipping when an adapter is missing check_depth has the same
                # observable result as a green-light, which is what drove the
                # 2026-05-08 cascade. Fail closed instead.
                self.stats.skipped_depth_low += 1
                log.warning(
                    "auto_executor.skip.preflight.depth_unavailable",
                    canonical_id=opp.canonical_id,
                    platform=platform,
                    side=side,
                    reason=(
                        "adapter_missing"
                        if adapter is None
                        else "adapter_lacks_check_depth"
                    ),
                )
                return None
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
            if sufficient:
                continue

            # Insufficient depth. If adaptive sizing is on, halve try_qty
            # progressively until the book has depth or we drop below 1.
            if not self._config.liquidity_adaptive_sizing:
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

            absorbed_qty = 0
            try_qty = max(1, required_qty_for_depth // 2)
            while try_qty >= 1:
                try:
                    side_ok, _ = await adapter.check_depth(market_id, side, try_qty)
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
                if side_ok:
                    absorbed_qty = try_qty
                    break
                try_qty //= 2

            if absorbed_qty < 1:
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

            adaptive_qty = min(adaptive_qty, absorbed_qty)
            log.info(
                "auto_executor.preflight.depth_reduced",
                canonical_id=opp.canonical_id,
                platform=platform,
                side=side,
                original_qty=qty,
                reduced_qty=adaptive_qty,
            )

        # If adaptive sizing reduced the qty, push the new size into the
        # opportunity so downstream (engine, auditor, alerting) sees the
        # actual size we will submit. Recompute fees + net edge at the
        # new qty because Kalshi fees are order-quantity-sensitive.
        if adaptive_qty < qty and self._config.liquidity_adaptive_sizing:
            qty = max(1, int(adaptive_qty))
            total_fees_per_unit = (
                compute_fee(opp.yes_platform, yes_price, qty, yes_fee_rate)
                + compute_fee(opp.no_platform, no_price, qty, no_fee_rate)
            ) / qty
            net_edge = gross_edge - total_fees_per_unit
            net_edge_cents = net_edge * 100.0
            pair_floor = self._config.edge_floor_for(
                opp.yes_platform, opp.no_platform,
            )
            if net_edge_cents < pair_floor:
                self.stats.skipped_edge_collapsed += 1
                log.info(
                    "auto_executor.skip.preflight.edge_collapsed_after_depth_reduce",
                    canonical_id=opp.canonical_id,
                    reduced_qty=qty,
                    net_edge_cents=round(net_edge_cents, 2),
                    threshold_cents=pair_floor,
                )
                return None
            opp.suggested_qty = qty
            opp.net_edge = net_edge
            opp.net_edge_cents = net_edge_cents
            opp.total_fees = total_fees_per_unit
            opp.max_profit_usd = round(net_edge * qty, 4)

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

    async def disable_auto_trade(self, canonical_id: str, reason: str) -> bool:
        """Mirror the DB-backed store's signature for the in-memory map so
        AutoExecutor's loss-streak kill-switch works in dev/test contexts too.
        """
        entry = self._market_map.get(canonical_id)
        if entry is None or not entry.get("allow_auto_trade"):
            return False
        entry["allow_auto_trade"] = False
        existing_notes = str(entry.get("notes", "") or "")
        entry["notes"] = f"{existing_notes}\n[auto-disabled: {reason}]".strip()
        return True


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
    failure_tracker=None,
    opportunity_queue: Optional[asyncio.Queue] = None,
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

    # PROFITABILITY-01: per-venue-pair preflight floors. Reads
    # PREFLIGHT_MIN_EDGE_CENTS_<VENUE1>_<VENUE2> first, falling back
    # to MIN_EDGE_CENTS_<VENUE1>_<VENUE2> (so the scanner gate and
    # preflight gate stay in lockstep), then the hardcoded defaults
    # tuned to the fee schedule of each pair.
    def _pair_floor(pair_key: str, default: float) -> float:
        a, b = pair_key.split("_")
        env_keys = [
            f"PREFLIGHT_MIN_EDGE_CENTS_{a.upper()}_{b.upper()}",
            f"PREFLIGHT_MIN_EDGE_CENTS_{b.upper()}_{a.upper()}",
            f"MIN_EDGE_CENTS_{a.upper()}_{b.upper()}",
            f"MIN_EDGE_CENTS_{b.upper()}_{a.upper()}",
        ]
        for k in env_keys:
            v = config_env.get(k)
            if v is not None and str(v).strip() != "":
                try:
                    return float(v)
                except (TypeError, ValueError):
                    continue
        return default

    pair_floors = {
        # FX×K: ~2¢ combined fees → 4.5¢ gross gives ~2.5¢ net.
        "forecastex_kalshi": _pair_floor("forecastex_kalshi", 4.5),
        # FX×P: ~1¢ combined fees → 4.0¢ gross gives ~3¢ net.
        "forecastex_polymarket": _pair_floor("forecastex_polymarket", 4.0),
        # K×P: keep the audit-calibrated 7¢ default.
        "kalshi_polymarket": _pair_floor("kalshi_polymarket", 7.0),
    }

    cfg = AutoExecutorConfig(
        enabled=_bool(config_env.get("AUTO_EXECUTE_ENABLED"), default=False),
        max_position_usd=_float(config_env.get("MAX_POSITION_USD"), 10.0),
        bootstrap_trades=_maybe_int(config_env.get("PHASE5_BOOTSTRAP_TRADES")),
        max_quote_age_s=_float(config_env.get("PREFLIGHT_MAX_QUOTE_AGE_S"), 30.0),
        min_depth_usd=_float(config_env.get("PREFLIGHT_MIN_DEPTH_USD"), 25.0),
        min_edge_cents_preflight=_float(
            # PREFLIGHT_MIN_EDGE_CENTS overrides if set; otherwise fall back to
            # MIN_EDGE_CENTS so the scanner / preflight / engine gates stay in
            # lockstep. Default 7.0 reflects the 2026-05 forensic audit.
            config_env.get("PREFLIGHT_MIN_EDGE_CENTS")
            or config_env.get("MIN_EDGE_CENTS"),
            7.0,
        ),
        min_edge_cents_preflight_by_pair=pair_floors,
        require_mapping_confirmed=_bool(
            config_env.get("PREFLIGHT_REQUIRE_MAPPING_CONFIRMED"), default=True,
        ),
        liquidity_adaptive_sizing=_bool(
            config_env.get("LIQUIDITY_ADAPTIVE_SIZING"), default=True,
        ),
        max_auto_gross_edge_cents=_float(
            config_env.get("MAX_AUTO_GROSS_EDGE_CENTS"), 8.0,
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
        failure_tracker=failure_tracker,
        opportunity_queue=opportunity_queue,
    )
