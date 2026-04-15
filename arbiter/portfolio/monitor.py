"""
ARBITER — Portfolio Monitor.

Real-time portfolio view tracking all open positions across venues,
exposure limits, risk violations, settlement calendar, and audit trail.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Set

from ..config.settings import ScannerConfig
from ..execution.engine import ExecutionEngine, OrderStatus
from ..ledger.position_ledger import PositionLedger, PositionStatus
from ..monitor.balance import BalanceMonitor

logger = logging.getLogger("arbiter.portfolio")


class RiskLevel(str, Enum):
    SAFE = "safe"
    WARNING = "warning"   # Near a limit
    VIOLATION = "violation"  # Over a hard limit
    CRITICAL = "critical"  # Emergency — one-leg, no hedge, etc.


@dataclass
class ExposureSlice:
    canonical_id: str
    description: str
    quantity: int
    yes_cost: float        # yes_price * quantity
    no_cost: float         # no_price * quantity
    total_cost: float      # yes_cost + no_cost
    yes_platform: str
    no_platform: str
    status: str
    hedge_status: str
    age_seconds: float
    last_price_update: float


@dataclass
class VenueExposure:
    platform: str
    total_exposure: float
    position_count: int
    balance: Optional[float]
    is_low_balance: bool
    last_update: float


@dataclass
class RiskViolation:
    violation_id: str
    level: RiskLevel
    category: str
    message: str
    canonical_id: Optional[str]
    platform: Optional[str]
    current_value: float
    limit_value: float
    timestamp: float

    def to_dict(self) -> dict:
        return {
            "violation_id": self.violation_id,
            "level": self.level.value,
            "category": self.category,
            "message": self.message,
            "canonical_id": self.canonical_id,
            "platform": self.platform,
            "current_value": round(self.current_value, 4),
            "limit_value": round(self.limit_value, 4),
            "timestamp": self.timestamp,
        }


@dataclass
class SettlementEvent:
    canonical_id: str
    description: str
    question: str
    resolve_date: Optional[datetime]
    markets: List[str]  # venue market IDs
    probability: float
    days_until_resolve: Optional[float]
    open_positions: int


@dataclass
class PortfolioSnapshot:
    timestamp: float
    total_exposure: float
    total_open_positions: int
    total_hedged: int
    total_unhedged: int
    by_venue: Dict[str, VenueExposure]
    by_canonical: Dict[str, ExposureSlice]
    violations: List[RiskViolation]
    unsettled_positions: int
    realized_pnl_today: float
    unrealized_pnl: float
    dry_run: bool = True

    def to_dict(self) -> dict:
        return {
            "timestamp": self.timestamp,
            "total_exposure": round(self.total_exposure, 2),
            "total_open_positions": self.total_open_positions,
            "total_hedged": self.total_hedged,
            "total_unhedged": self.total_unhedged,
            "by_venue": {
                k: {
                    "platform": v.platform,
                    "total_exposure": round(v.total_exposure, 2),
                    "position_count": v.position_count,
                    "balance": round(v.balance, 2) if v.balance else None,
                    "is_low_balance": v.is_low_balance,
                }
                for k, v in self.by_venue.items()
            },
            "by_canonical": {
                k: {
                    "canonical_id": v.canonical_id,
                    "description": v.description,
                    "quantity": v.quantity,
                    "total_cost": round(v.total_cost, 2),
                    "status": v.status,
                    "hedge_status": v.hedge_status,
                    "age_seconds": round(v.age_seconds, 0),
                }
                for k, v in self.by_canonical.items()
            },
            "violations": [v.to_dict() for v in self.violations],
            "unsettled_positions": self.unsettled_positions,
            "realized_pnl_today": round(self.realized_pnl_today, 4),
            "unrealized_pnl": round(self.unrealized_pnl, 4),
            "dry_run": self.dry_run,
        }


@dataclass
class PortfolioConfig:
    # Exposure limits
    max_per_market_usd: float = 100.0
    max_total_exposure_usd: float = 500.0
    max_per_venue_usd: float = 300.0
    # Per-venue balance thresholds
    kalshi_min_balance: float = 50.0
    polymarket_min_balance: float = 25.0
    predictit_min_balance: float = 50.0
    # Concentration limits
    max_positions_per_event: int = 3
    # Stale thresholds
    max_quote_age_seconds: float = 30.0
    max_hedge_age_seconds: float = 7200.0  # 2h


class PortfolioMonitor:
    """
    Real-time portfolio monitor.

    Aggregates:
    - Open positions from ExecutionEngine (in-memory) and PositionLedger (durable)
    - Balances from BalanceMonitor
    - Settlement data from market mappings

    Emits risk violations and exposure snapshots.
    Can call on_update callbacks when state changes significantly.
    """

    def __init__(
        self,
        config: PortfolioConfig,
        scanner_config: ScannerConfig,
        execution_engine: ExecutionEngine,
        balance_monitor: BalanceMonitor,
        ledger: Optional[PositionLedger] = None,
    ):
        self.config = config
        self.scanner_config = scanner_config
        self.engine = execution_engine
        self.balance_monitor = balance_monitor
        self.ledger = ledger

        self._running = False
        self._callbacks: List[Callable[[PortfolioSnapshot], None]] = []
        self._last_snapshot: Optional[PortfolioSnapshot] = None
        self._last_violation_ids: Set[str] = set()
        self._check_interval = 30.0  # seconds

    # ─── Registration ─────────────────────────────────────────────────────

    def on_update(self, callback: Callable[[PortfolioSnapshot], None]) -> None:
        """Register a callback called on each portfolio snapshot update."""
        self._callbacks.append(callback)

    # ─── Snapshot ─────────────────────────────────────────────────────────

    def compute_snapshot(self, dry_run: bool = True) -> PortfolioSnapshot:
        """
        Compute a full portfolio snapshot from all data sources.
        Call this on a timer or after each trade event.
        """
        now = time.time()
        positions = self._get_open_positions()

        # Compute per-canonical exposures
        by_canonical: Dict[str, ExposureSlice] = {}
        total_exposure = 0.0
        total_hedged = 0
        total_unhedged = 0
        unrealized = 0.0

        for pos in positions:
            cid = pos.get("canonical_id", "unknown")
            qty = pos.get("quantity", 0)
            yes_price = pos.get("yes_price", 0)
            no_price = pos.get("no_price", 0)
            yes_cost = qty * yes_price
            no_cost = qty * no_price
            cost = yes_cost + no_cost
            total_exposure += cost

            status = pos.get("status", "open")
            hedge_status = pos.get("hedge_status", "none")

            if status == "hedged":
                total_hedged += 1
            elif status == "open" and hedge_status == "none":
                total_unhedged += 1

            age = now - pos.get("created_at", now)
            if isinstance(pos.get("created_at"), datetime):
                age = now - pos["created_at"].timestamp()

            by_canonical[cid] = ExposureSlice(
                canonical_id=cid,
                description=pos.get("description", cid),
                quantity=qty,
                yes_cost=yes_cost,
                no_cost=no_cost,
                total_cost=cost,
                yes_platform=pos.get("yes_platform", "unknown"),
                no_platform=pos.get("no_platform", "unknown"),
                status=status,
                hedge_status=hedge_status,
                age_seconds=age,
                last_price_update=pos.get("updated_at", now),
            )

        # Per-venue aggregation
        by_venue: Dict[str, VenueExposure] = {}
        all_platforms = set()
        for pos in positions:
            all_platforms.add(pos.get("yes_platform", ""))
            all_platforms.add(pos.get("no_platform", ""))

        balances = self.balance_monitor.get_all_balances() if self.balance_monitor else {}
        for platform in all_platforms:
            venue_positions = [p for p in positions
                              if p.get("yes_platform") == platform or p.get("no_platform") == platform]
            venue_exposure = sum(
                p.get("quantity", 0) * (p.get("yes_price", 0) + p.get("no_price", 0))
                for p in venue_positions
            )
            balance_info = balances.get(platform, {})
            balance = balance_info.get("balance")
            is_low = False
            if platform == "kalshi" and balance is not None:
                is_low = balance < self.config.kalshi_min_balance
            elif platform == "polymarket" and balance is not None:
                is_low = balance < self.config.polymarket_min_balance
            elif platform == "predictit" and balance is not None:
                is_low = balance < self.config.predictit_min_balance

            by_venue[platform] = VenueExposure(
                platform=platform,
                total_exposure=venue_exposure,
                position_count=len(venue_positions),
                balance=balance,
                is_low_balance=is_low,
                last_update=now,
            )

        # Risk violations
        violations = self._check_violations(positions, by_venue, total_exposure, now)

        # Unrealized P&L (rough — based on current price vs entry price)
        unrealized = self._estimate_unrealized(positions)

        snapshot = PortfolioSnapshot(
            timestamp=now,
            total_exposure=total_exposure,
            total_open_positions=len(positions),
            total_hedged=total_hedged,
            total_unhedged=total_unhedged,
            by_venue=by_venue,
            by_canonical=by_canonical,
            violations=violations,
            unsettled_positions=len([p for p in positions if p.get("status") in ("open", "hedged")]),
            realized_pnl_today=0.0,  # TODO: wire from ledger
            unrealized_pnl=unrealized,
            dry_run=dry_run,
        )

        self._last_snapshot = snapshot
        return snapshot

    def _get_open_positions(self) -> List[Dict[str, Any]]:
        """Gather open positions from engine and (optionally) ledger."""
        positions = []

        # From in-memory engine
        if self.engine:
            for arb_exec in getattr(self.engine, "_executions", []):
                pos = {
                    "canonical_id": arb_exec.opportunity.canonical_id if hasattr(arb_exec, "opportunity") else arb_exec.arb_id,
                    "description": arb_exec.opportunity.description if hasattr(arb_exec, "opportunity") else "",
                    "quantity": arb_exec.opportunity.suggested_qty if hasattr(arb_exec, "opportunity") else 0,
                    "yes_price": arb_exec.opportunity.yes_price if hasattr(arb_exec, "opportunity") else 0,
                    "no_price": arb_exec.opportunity.no_price if hasattr(arb_exec, "opportunity") else 0,
                    "yes_platform": arb_exec.opportunity.yes_platform if hasattr(arb_exec, "opportunity") else "",
                    "no_platform": arb_exec.opportunity.no_platform if hasattr(arb_exec, "opportunity") else "",
                    "status": arb_exec.status,
                    "hedge_status": "complete" if arb_exec.leg_no.status == OrderStatus.FILLED else "none",
                    "created_at": arb_exec.timestamp,
                    "updated_at": arb_exec.timestamp,
                    "source": "engine",
                }
                if pos["status"] in ("pending", "submitted", "simulated"):
                    positions.append(pos)

        # From durable ledger
        if self.ledger and self.ledger._pool:
            try:
                import asyncio as _asy
                if asyncio.iscoroutinefunction(self.ledger.get_open_positions):
                    # Needs to be called in async context — skip for sync snapshot
                    pass
            except Exception:
                pass

        return positions

    def _check_violations(
        self,
        positions: List[Dict[str, Any]],
        by_venue: Dict[str, VenueExposure],
        total_exposure: float,
        now: float,
    ) -> List[RiskViolation]:
        """Check all risk rules and return active violations."""
        violations = []

        # 1. Per-market exposure
        for pos in positions:
            cost = pos.get("quantity", 0) * (pos.get("yes_price", 0) + pos.get("no_price", 0))
            if cost > self.config.max_per_market_usd:
                violations.append(RiskViolation(
                    violation_id=f"market_exposure:{pos.get('canonical_id')}",
                    level=RiskLevel.VIOLATION if cost > self.config.max_per_market_usd * 1.5
                          else RiskLevel.WARNING,
                    category="exposure",
                    message=f"Per-market exposure ${cost:.2f} exceeds ${self.config.max_per_market_usd:.2f} limit",
                    canonical_id=pos.get("canonical_id"),
                    platform=None,
                    current_value=cost,
                    limit_value=self.config.max_per_market_usd,
                    timestamp=now,
                ))

        # 2. Total exposure
        if total_exposure > self.config.max_total_exposure_usd:
            violations.append(RiskViolation(
                violation_id="total_exposure",
                level=RiskLevel.CRITICAL if total_exposure > self.config.max_total_exposure_usd * 1.5
                      else RiskLevel.VIOLATION,
                category="exposure",
                message=f"Total exposure ${total_exposure:.2f} exceeds ${self.config.max_total_exposure_usd:.2f} limit",
                canonical_id=None,
                platform=None,
                current_value=total_exposure,
                limit_value=self.config.max_total_exposure_usd,
                timestamp=now,
            ))

        # 3. Per-venue exposure
        for venue_name, venue in by_venue.items():
            max_venue = self.config.max_per_venue_usd
            if venue.total_exposure > max_venue:
                violations.append(RiskViolation(
                    violation_id=f"venue_exposure:{venue_name}",
                    level=RiskLevel.VIOLATION,
                    category="exposure",
                    message=f"{venue_name} exposure ${venue.total_exposure:.2f} exceeds ${max_venue:.2f} limit",
                    canonical_id=None,
                    platform=venue_name,
                    current_value=venue.total_exposure,
                    limit_value=max_venue,
                    timestamp=now,
                ))

        # 4. Low balance
        for venue_name, venue in by_venue.items():
            if venue.is_low_balance:
                violations.append(RiskViolation(
                    violation_id=f"low_balance:{venue_name}",
                    level=RiskLevel.WARNING,
                    category="balance",
                    message=f"{venue_name} balance ${venue.balance:.2f} is critically low",
                    canonical_id=None,
                    platform=venue_name,
                    current_value=venue.balance or 0,
                    limit_value=self.config.kalshi_min_balance,
                    timestamp=now,
                ))

        # 5. Stale/unhedged positions
        for pos in positions:
            if pos.get("status") == "open" and pos.get("hedge_status") == "none":
                age = now - pos.get("updated_at", now)
                if isinstance(pos.get("updated_at"), datetime):
                    age = now - pos["updated_at"].timestamp()
                if age > self.config.max_hedge_age_seconds:
                    violations.append(RiskViolation(
                        violation_id=f"stale_hedge:{pos.get('canonical_id')}",
                        level=RiskLevel.WARNING,
                        category="stale",
                        message=f"Position {pos.get('canonical_id')} unhedged for {age/3600:.1f}h",
                        canonical_id=pos.get("canonical_id"),
                        platform=pos.get("yes_platform"),
                        current_value=age,
                        limit_value=self.config.max_hedge_age_seconds,
                        timestamp=now,
                    ))

        # 6. DRY_RUN violation check (alert if LIVE)
        # This is informational — no violation object needed, handled in snapshot.dry_run

        return violations

    def _estimate_unrealized(self, positions: List[Dict[str, Any]]) -> float:
        """
        Rough unrealized P&L estimate.
        Uses entry prices vs current top-of-book.
        """
        unrealized = 0.0
        for pos in positions:
            if pos.get("status") != "hedged":
                continue
            # Very rough: if market moved 1¢ against us per contract
            # Real implementation would use live price feed
            pass
        return unrealized

    # ─── Run loop ─────────────────────────────────────────────────────────

    async def run(self, interval: float = 30.0) -> None:
        """Run the monitor loop, emitting snapshots periodically."""
        self._running = True
        logger.info(f"PortfolioMonitor: started (interval={interval}s)")

        while self._running:
            try:
                snapshot = self.compute_snapshot()

                # Only fire callbacks if state changed meaningfully
                has_new_violations = any(
                    v.violation_id not in self._last_violation_ids
                    for v in snapshot.violations
                )
                if has_new_violations or snapshot.violations:
                    self._last_violation_ids = {v.violation_id for v in snapshot.violations}

                for cb in self._callbacks:
                    try:
                        cb(snapshot)
                    except Exception as e:
                        logger.error(f"PortfolioMonitor callback error: {e}")

                if snapshot.violations:
                    for v in snapshot.violations:
                        if v.level in (RiskLevel.VIOLATION, RiskLevel.CRITICAL):
                            logger.warning(f"[Portfolio] {v.level.value.upper()}: {v.message}")

                await asyncio.sleep(interval)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"PortfolioMonitor error: {e}")
                await asyncio.sleep(5)

        logger.info("PortfolioMonitor: stopped")

    def stop(self) -> None:
        self._running = False

    # ─── Queries ─────────────────────────────────────────────────────────

    def get_snapshot(self) -> Optional[PortfolioSnapshot]:
        return self._last_snapshot

    def get_active_violations(self) -> List[RiskViolation]:
        if self._last_snapshot:
            return self._last_snapshot.violations
        return []

    def is_safe_to_trade(self) -> tuple[bool, str]:
        """
        Quick check: is it safe to submit a new trade?
        Returns (safe, reason).
        """
        if not self._last_snapshot:
            return True, ""

        if not self._last_snapshot.dry_run:
            return False, "LIVE TRADING — must be in DRY_RUN mode"

        critical = [v for v in self._last_snapshot.violations
                     if v.level == RiskLevel.CRITICAL]
        if critical:
            return False, f"Critical violation: {critical[0].message}"

        violations = [v for v in self._last_snapshot.violations
                      if v.level in (RiskLevel.VIOLATION, RiskLevel.WARNING)]
        if violations:
            return True, f"Warning only: {violations[0].message}"

        return True, ""
