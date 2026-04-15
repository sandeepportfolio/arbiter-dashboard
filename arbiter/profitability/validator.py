"""
Continuous profitability validation loop for ARBITER.

The engine can already run forever; this module answers a different question:
"Do we have enough evidence to trust that the system is actually profitable?"

It does that by re-scoring the runtime continuously against strict thresholds
for sample size, realized P&L, audit quality, and incident rate. The loop keeps
running and will not mark the system as profitable until all gates are met.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from statistics import mean
from typing import Deque, Dict, List, Optional

from ..execution.engine import ArbExecution, ExecutionEngine, ExecutionIncident
from ..scanner.arbitrage import ArbitrageOpportunity, ArbitrageScanner

logger = logging.getLogger("arbiter.profitability")

TERMINAL_VERDICTS = {"validated_profitable", "not_profitable", "blocked"}
COMPLETED_EXECUTION_STATUSES = {"simulated", "filled", "manual_closed"}


@dataclass
class ProfitabilityConfig:
    evaluation_interval: float = 5.0
    min_scan_count: int = 250
    min_published_opportunities: int = 25
    min_completed_executions: int = 10
    min_total_realized_pnl: float = 5.0
    min_average_realized_pnl: float = 0.25
    min_average_edge_cents: float = 3.0
    min_profitable_execution_ratio: float = 0.90
    min_audit_pass_rate: float = 0.995
    max_incident_rate: float = 0.15
    max_critical_incidents: int = 0

    def to_dict(self) -> dict:
        return {
            "evaluation_interval": self.evaluation_interval,
            "min_scan_count": self.min_scan_count,
            "min_published_opportunities": self.min_published_opportunities,
            "min_completed_executions": self.min_completed_executions,
            "min_total_realized_pnl": round(self.min_total_realized_pnl, 4),
            "min_average_realized_pnl": round(self.min_average_realized_pnl, 4),
            "min_average_edge_cents": round(self.min_average_edge_cents, 4),
            "min_profitable_execution_ratio": round(self.min_profitable_execution_ratio, 4),
            "min_audit_pass_rate": round(self.min_audit_pass_rate, 6),
            "max_incident_rate": round(self.max_incident_rate, 4),
            "max_critical_incidents": self.max_critical_incidents,
        }


@dataclass
class ProfitabilitySnapshot:
    timestamp: float
    runtime_seconds: float
    verdict: str
    is_profitable: bool
    is_determined: bool
    progress: float
    reasons: List[str] = field(default_factory=list)
    scan_count: int = 0
    published_opportunities: int = 0
    active_opportunities: int = 0
    completed_executions: int = 0
    profitable_executions: int = 0
    losing_executions: int = 0
    breakeven_executions: int = 0
    total_realized_pnl: float = 0.0
    average_realized_pnl: float = 0.0
    average_edge_cents: float = 0.0
    best_edge_cents: float = 0.0
    incident_count: int = 0
    critical_incidents: int = 0
    incident_rate: float = 0.0
    audit_pass_rate: float = 1.0
    targets: Dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "timestamp": self.timestamp,
            "runtime_seconds": round(self.runtime_seconds, 1),
            "verdict": self.verdict,
            "is_profitable": self.is_profitable,
            "is_determined": self.is_determined,
            "progress": round(self.progress, 4),
            "reasons": list(self.reasons),
            "scan_count": self.scan_count,
            "published_opportunities": self.published_opportunities,
            "active_opportunities": self.active_opportunities,
            "completed_executions": self.completed_executions,
            "profitable_executions": self.profitable_executions,
            "losing_executions": self.losing_executions,
            "breakeven_executions": self.breakeven_executions,
            "total_realized_pnl": round(self.total_realized_pnl, 4),
            "average_realized_pnl": round(self.average_realized_pnl, 4),
            "average_edge_cents": round(self.average_edge_cents, 4),
            "best_edge_cents": round(self.best_edge_cents, 4),
            "incident_count": self.incident_count,
            "critical_incidents": self.critical_incidents,
            "incident_rate": round(self.incident_rate, 4),
            "audit_pass_rate": round(self.audit_pass_rate, 6),
            "targets": dict(self.targets),
        }


class ProfitabilityValidator:
    """
    Continuously evaluates whether ARBITER has enough evidence to call itself
    profitable.

    The validator is intentionally strict:
    - it refuses to mark the system as profitable without enough observations
    - it blocks profitability when audit integrity or incident severity degrade
    - it exposes an explicit verdict for the API/dashboard
    """

    def __init__(
        self,
        config: ProfitabilityConfig,
        scanner: ArbitrageScanner,
        engine: ExecutionEngine,
    ):
        self.config = config
        self.scanner = scanner
        self.engine = engine
        self._running = False
        self._started_at = time.time()
        self._last_snapshot: Optional[ProfitabilitySnapshot] = None
        self._history: Deque[dict] = deque(maxlen=240)
        self._determined = asyncio.Event()
        self.refresh()

    def refresh(self) -> ProfitabilitySnapshot:
        scanner_stats = self.scanner.stats
        engine_stats = self.engine.stats
        executions = list(self.engine.execution_history)
        incidents = list(self.engine.incidents)
        current_opportunities = list(self.scanner.current_opportunities)

        completed = self._completed_executions(executions)
        profitable = [execution for execution in completed if execution.realized_pnl > 0]
        losing = [execution for execution in completed if execution.realized_pnl < 0]
        breakeven = [execution for execution in completed if execution.realized_pnl == 0]

        total_realized_pnl = sum(execution.realized_pnl for execution in completed)
        average_realized_pnl = (
            total_realized_pnl / len(completed)
            if completed
            else 0.0
        )

        edge_samples = [
            execution.opportunity.net_edge_cents
            for execution in completed
            if getattr(execution, "opportunity", None) is not None
        ]
        average_edge_cents = mean(edge_samples) if edge_samples else 0.0
        best_edge_cents = max(
            [
                *(opportunity.net_edge_cents for opportunity in current_opportunities),
                *edge_samples,
            ],
            default=0.0,
        )

        critical_incidents = sum(
            1 for incident in incidents if str(incident.severity).lower() == "critical"
        )
        total_executions = max(engine_stats.get("total_executions", len(executions)), 0)
        incident_count = len(incidents)
        incident_rate = (
            incident_count / total_executions
            if total_executions > 0
            else float(incident_count > 0)
        )
        audit_pass_rate = float(engine_stats.get("audit", {}).get("pass_rate", 1.0))
        profitable_ratio = (
            len(profitable) / len(completed)
            if completed
            else 0.0
        )

        reasons = self._build_reasons(
            scan_count=scanner_stats.get("scan_count", 0),
            published_opportunities=scanner_stats.get("published", 0),
            completed_executions=len(completed),
            total_realized_pnl=total_realized_pnl,
            average_realized_pnl=average_realized_pnl,
            average_edge_cents=average_edge_cents,
            profitable_ratio=profitable_ratio,
            audit_pass_rate=audit_pass_rate,
            incident_rate=incident_rate,
            critical_incidents=critical_incidents,
        )
        verdict = self._resolve_verdict(
            scan_count=scanner_stats.get("scan_count", 0),
            published_opportunities=scanner_stats.get("published", 0),
            completed_executions=len(completed),
            total_realized_pnl=total_realized_pnl,
            average_realized_pnl=average_realized_pnl,
            average_edge_cents=average_edge_cents,
            profitable_ratio=profitable_ratio,
            audit_pass_rate=audit_pass_rate,
            incident_rate=incident_rate,
            critical_incidents=critical_incidents,
        )
        progress = self._compute_progress(
            scan_count=scanner_stats.get("scan_count", 0),
            published_opportunities=scanner_stats.get("published", 0),
            completed_executions=len(completed),
            total_realized_pnl=total_realized_pnl,
            average_realized_pnl=average_realized_pnl,
            average_edge_cents=average_edge_cents,
            profitable_ratio=profitable_ratio,
            audit_pass_rate=audit_pass_rate,
            incident_rate=incident_rate,
            critical_incidents=critical_incidents,
        )

        snapshot = ProfitabilitySnapshot(
            timestamp=time.time(),
            runtime_seconds=max(time.time() - self._started_at, 0.0),
            verdict=verdict,
            is_profitable=verdict == "validated_profitable",
            is_determined=verdict in TERMINAL_VERDICTS,
            progress=progress,
            reasons=reasons,
            scan_count=int(scanner_stats.get("scan_count", 0)),
            published_opportunities=int(scanner_stats.get("published", 0)),
            active_opportunities=int(scanner_stats.get("active_opportunities", 0)),
            completed_executions=len(completed),
            profitable_executions=len(profitable),
            losing_executions=len(losing),
            breakeven_executions=len(breakeven),
            total_realized_pnl=total_realized_pnl,
            average_realized_pnl=average_realized_pnl,
            average_edge_cents=average_edge_cents,
            best_edge_cents=best_edge_cents,
            incident_count=incident_count,
            critical_incidents=critical_incidents,
            incident_rate=incident_rate,
            audit_pass_rate=audit_pass_rate,
            targets=self.config.to_dict(),
        )

        previous_verdict = self._last_snapshot.verdict if self._last_snapshot else None
        self._last_snapshot = snapshot
        self._history.append(
            {
                "timestamp": snapshot.timestamp,
                "verdict": snapshot.verdict,
                "progress": round(snapshot.progress, 4),
                "total_realized_pnl": round(snapshot.total_realized_pnl, 4),
                "completed_executions": snapshot.completed_executions,
                "published_opportunities": snapshot.published_opportunities,
            }
        )

        if snapshot.is_determined:
            self._determined.set()

        if previous_verdict != snapshot.verdict:
            logger.info(
                "Profitability verdict changed: %s -> %s (pnl=$%.2f, completed=%s, published=%s, audit=%.4f)",
                previous_verdict or "none",
                snapshot.verdict,
                snapshot.total_realized_pnl,
                snapshot.completed_executions,
                snapshot.published_opportunities,
                snapshot.audit_pass_rate,
            )

        return snapshot

    async def run(self) -> None:
        self._running = True
        logger.info(
            "Profitability validator started (interval=%ss, min_executions=%s, min_total_pnl=$%.2f)",
            self.config.evaluation_interval,
            self.config.min_completed_executions,
            self.config.min_total_realized_pnl,
        )

        while self._running:
            try:
                self.refresh()
                await asyncio.sleep(self.config.evaluation_interval)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("Profitability validator error: %s", exc)
                await asyncio.sleep(2.0)

        logger.info("Profitability validator stopped")

    async def wait_until_determined(self, timeout: Optional[float] = None) -> ProfitabilitySnapshot:
        if self._last_snapshot and self._last_snapshot.is_determined:
            return self._last_snapshot

        if timeout is None:
            await self._determined.wait()
        else:
            await asyncio.wait_for(self._determined.wait(), timeout=timeout)
        return self.get_snapshot()

    def stop(self) -> None:
        self._running = False

    def get_snapshot(self) -> ProfitabilitySnapshot:
        return self._last_snapshot or self.refresh()

    @property
    def history(self) -> List[dict]:
        return list(self._history)

    def _resolve_verdict(
        self,
        *,
        scan_count: int,
        published_opportunities: int,
        completed_executions: int,
        total_realized_pnl: float,
        average_realized_pnl: float,
        average_edge_cents: float,
        profitable_ratio: float,
        audit_pass_rate: float,
        incident_rate: float,
        critical_incidents: int,
    ) -> str:
        if (
            audit_pass_rate < self.config.min_audit_pass_rate
            or critical_incidents > self.config.max_critical_incidents
        ):
            return "blocked"

        if (
            scan_count < self.config.min_scan_count
            or published_opportunities < self.config.min_published_opportunities
            or completed_executions < self.config.min_completed_executions
        ):
            return "collecting_evidence"

        if (
            total_realized_pnl < self.config.min_total_realized_pnl
            or average_realized_pnl < self.config.min_average_realized_pnl
            or average_edge_cents < self.config.min_average_edge_cents
            or profitable_ratio < self.config.min_profitable_execution_ratio
            or incident_rate > self.config.max_incident_rate
        ):
            return "not_profitable"

        return "validated_profitable"

    def _build_reasons(
        self,
        *,
        scan_count: int,
        published_opportunities: int,
        completed_executions: int,
        total_realized_pnl: float,
        average_realized_pnl: float,
        average_edge_cents: float,
        profitable_ratio: float,
        audit_pass_rate: float,
        incident_rate: float,
        critical_incidents: int,
    ) -> List[str]:
        reasons: List[str] = []

        if audit_pass_rate < self.config.min_audit_pass_rate:
            reasons.append(
                f"Audit pass rate {audit_pass_rate:.4f} is below the required {self.config.min_audit_pass_rate:.4f}"
            )
        if critical_incidents > self.config.max_critical_incidents:
            reasons.append(
                f"Critical incidents {critical_incidents} exceed the allowed {self.config.max_critical_incidents}"
            )

        if scan_count < self.config.min_scan_count:
            reasons.append(
                f"Need {self.config.min_scan_count - scan_count} more scans before profitability can be determined"
            )
        if published_opportunities < self.config.min_published_opportunities:
            reasons.append(
                f"Need {self.config.min_published_opportunities - published_opportunities} more published opportunities"
            )
        if completed_executions < self.config.min_completed_executions:
            reasons.append(
                f"Need {self.config.min_completed_executions - completed_executions} more completed executions"
            )

        if completed_executions >= self.config.min_completed_executions:
            if total_realized_pnl < self.config.min_total_realized_pnl:
                reasons.append(
                    f"Total realized P&L ${total_realized_pnl:.2f} is below the required ${self.config.min_total_realized_pnl:.2f}"
                )
            if average_realized_pnl < self.config.min_average_realized_pnl:
                reasons.append(
                    f"Average realized P&L ${average_realized_pnl:.2f} is below the required ${self.config.min_average_realized_pnl:.2f}"
                )
            if average_edge_cents < self.config.min_average_edge_cents:
                reasons.append(
                    f"Average edge {average_edge_cents:.2f}¢ is below the required {self.config.min_average_edge_cents:.2f}¢"
                )
            if profitable_ratio < self.config.min_profitable_execution_ratio:
                reasons.append(
                    f"Profitable execution ratio {profitable_ratio:.2%} is below the required {self.config.min_profitable_execution_ratio:.2%}"
                )
            if incident_rate > self.config.max_incident_rate:
                reasons.append(
                    f"Incident rate {incident_rate:.2%} exceeds the allowed {self.config.max_incident_rate:.2%}"
                )

        if not reasons:
            reasons.append("All profitability gates are currently satisfied")
        return reasons

    def _compute_progress(
        self,
        *,
        scan_count: int,
        published_opportunities: int,
        completed_executions: int,
        total_realized_pnl: float,
        average_realized_pnl: float,
        average_edge_cents: float,
        profitable_ratio: float,
        audit_pass_rate: float,
        incident_rate: float,
        critical_incidents: int,
    ) -> float:
        progress_inputs = [
            self._ratio(scan_count, self.config.min_scan_count),
            self._ratio(published_opportunities, self.config.min_published_opportunities),
            self._ratio(completed_executions, self.config.min_completed_executions),
            self._ratio(total_realized_pnl, self.config.min_total_realized_pnl),
            self._ratio(average_realized_pnl, self.config.min_average_realized_pnl),
            self._ratio(average_edge_cents, self.config.min_average_edge_cents),
            self._ratio(profitable_ratio, self.config.min_profitable_execution_ratio),
            self._inverse_ratio(incident_rate, self.config.max_incident_rate),
            self._inverse_ratio(float(critical_incidents), float(self.config.max_critical_incidents)),
            self._ratio(audit_pass_rate, self.config.min_audit_pass_rate),
        ]
        return max(0.0, min(mean(progress_inputs), 1.0))

    @staticmethod
    def _completed_executions(executions: List[ArbExecution]) -> List[ArbExecution]:
        return [
            execution
            for execution in executions
            if execution.status in COMPLETED_EXECUTION_STATUSES or execution.realized_pnl != 0.0
        ]

    @staticmethod
    def _ratio(value: float, threshold: float) -> float:
        if threshold <= 0:
            return 1.0
        if value <= 0:
            return 0.0
        return max(0.0, min(value / threshold, 1.0))

    @staticmethod
    def _inverse_ratio(value: float, limit: float) -> float:
        if limit <= 0:
            return 1.0 if value <= 0 else 0.0
        if value <= 0:
            return 1.0
        if value >= limit:
            return 0.0
        return max(0.0, min(1.0 - (value / limit), 1.0))
