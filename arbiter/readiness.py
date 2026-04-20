"""
ARBITER live-readiness gating and operator-facing status snapshots.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from .config.settings import ArbiterConfig, iter_confirmed_market_mappings


@dataclass
class ReadinessCheck:
    key: str
    status: str
    summary: str
    blocking: bool = False
    details: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "status": self.status,
            "summary": self.summary,
            "blocking": self.blocking,
            "details": dict(self.details),
        }


@dataclass
class ReadinessSnapshot:
    timestamp: float
    mode: str
    ready_for_live_trading: bool
    blocking_reasons: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    checks: List[ReadinessCheck] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "timestamp": self.timestamp,
            "mode": self.mode,
            "ready_for_live_trading": self.ready_for_live_trading,
            "blocking_reasons": list(self.blocking_reasons),
            "warnings": list(self.warnings),
            "checks": [check.to_dict() for check in self.checks],
        }


class OperationalReadiness:
    """
    Builds a single readiness snapshot from the existing runtime components.

    The goal is simple: dry-run may continue collecting evidence, but live order
    submission must stay closed until profitability, balances, mappings,
    collector health, and credentials are all aligned.
    """

    def __init__(
        self,
        config: ArbiterConfig,
        *,
        engine=None,
        monitor=None,
        profitability=None,
        collectors: Optional[Dict[str, object]] = None,
        reconciler=None,
    ):
        self.config = config
        self.engine = engine
        self.monitor = monitor
        self.profitability = profitability
        self.collectors = collectors or {}
        self.reconciler = reconciler
        self._last_snapshot: Optional[ReadinessSnapshot] = None

    def refresh(self) -> ReadinessSnapshot:
        checks = [
            self._check_auto_trade_mappings(),
            self._check_platform_credentials(),
            self._check_alerting(),
            self._check_profitability(),
            self._check_incidents(),
            self._check_balances(),
            self._check_collectors(),
            self._check_reconciliation(),
        ]
        blocking_reasons = [check.summary for check in checks if check.blocking and check.status != "pass"]
        warnings = [check.summary for check in checks if check.status in {"warning", "manual"}]
        snapshot = ReadinessSnapshot(
            timestamp=time.time(),
            mode="dry-run" if self.config.scanner.dry_run else "live",
            ready_for_live_trading=not blocking_reasons,
            blocking_reasons=blocking_reasons,
            warnings=warnings,
            checks=checks,
        )
        self._last_snapshot = snapshot
        return snapshot

    def get_snapshot(self) -> ReadinessSnapshot:
        return self._last_snapshot or self.refresh()

    def startup_failures(self) -> List[str]:
        """
        Hard-fail startup only for configuration gaps that cannot self-heal at
        runtime. Operational evidence gates still run after startup.
        """
        failures: List[str] = []
        auto_trade = list(iter_confirmed_market_mappings(require_auto_trade=True))
        if not auto_trade:
            failures.append("No confirmed auto-trade mappings are enabled")

        kalshi = self.collectors.get("kalshi")
        kalshi_ready = bool(
            getattr(getattr(kalshi, "auth", None), "is_authenticated", False)
            or (self.config.kalshi.api_key_id and self.config.kalshi.private_key_path)
        )
        if not kalshi_ready:
            failures.append("Kalshi API credentials are not configured")

        if not self.config.polymarket.private_key:
            failures.append("Polymarket private key is not configured")

        return failures

    def allow_execution(self, opportunity) -> Tuple[bool, str, Dict[str, Any]]:
        if self.config.scanner.dry_run:
            return True, "dry-run mode collecting evidence", self.refresh().to_dict()

        snapshot = self.refresh()
        if not snapshot.ready_for_live_trading:
            reason = snapshot.blocking_reasons[0] if snapshot.blocking_reasons else "Live readiness is not satisfied"
            return False, reason, snapshot.to_dict()

        balances = getattr(self.monitor, "current_balances", {}) or {}
        low_platforms = [
            platform
            for platform in {opportunity.yes_platform, opportunity.no_platform}
            if platform in balances and balances[platform].is_low
        ]
        if low_platforms:
            return False, f"Low balances on live venues: {', '.join(sorted(low_platforms))}", snapshot.to_dict()

        return True, "ready for live execution", snapshot.to_dict()

    def _check_auto_trade_mappings(self) -> ReadinessCheck:
        mappings = [
            canonical_id
            for canonical_id, _ in iter_confirmed_market_mappings(require_auto_trade=True)
        ]
        if mappings:
            return ReadinessCheck(
                key="auto_trade_mappings",
                status="pass",
                summary=f"{len(mappings)} confirmed auto-trade mappings enabled",
                blocking=True,
                details={"canonical_ids": mappings},
            )
        return ReadinessCheck(
            key="auto_trade_mappings",
            status="fail",
            summary="No confirmed auto-trade mappings are enabled",
            blocking=True,
        )

    def _check_platform_credentials(self) -> ReadinessCheck:
        kalshi = self.collectors.get("kalshi")
        kalshi_ready = bool(
            getattr(getattr(kalshi, "auth", None), "is_authenticated", False)
            or (self.config.kalshi.api_key_id and self.config.kalshi.private_key_path)
        )
        polymarket_ready = bool(self.config.polymarket.private_key)
        details = {
            "kalshi_authenticated": kalshi_ready,
            "polymarket_private_key": polymarket_ready,
        }
        if kalshi_ready and polymarket_ready:
            return ReadinessCheck(
                key="platform_credentials",
                status="pass",
                summary="Live venue credentials are configured",
                blocking=True,
                details=details,
            )
        missing = []
        if not kalshi_ready:
            missing.append("Kalshi")
        if not polymarket_ready:
            missing.append("Polymarket")
        return ReadinessCheck(
            key="platform_credentials",
            status="fail",
            summary=f"Missing live venue credentials: {', '.join(missing)}",
            blocking=True,
            details=details,
        )

    def _check_alerting(self) -> ReadinessCheck:
        enabled = bool(self.config.alerts.telegram_bot_token and self.config.alerts.telegram_chat_id)
        if enabled:
            return ReadinessCheck(
                key="alerting",
                status="pass",
                summary="Telegram alerting is configured",
                blocking=True,
            )
        return ReadinessCheck(
            key="alerting",
            status="warning",
            summary="Telegram alerting is not configured yet",
            blocking=True,
        )

    def _check_profitability(self) -> ReadinessCheck:
        if not self.profitability:
            return ReadinessCheck(
                key="profitability",
                status="fail",
                summary="Profitability validator is unavailable",
                blocking=True,
            )

        snapshot = self.profitability.get_snapshot()

        # B-1 Q6: Phase 5 bootstrap override (chicken-and-egg resolution).
        # When PHASE5_BOOTSTRAP_TRADES is set to an int in [1, 5] AND the
        # completed-execution count is below that threshold, bypass the
        # profitability gate for the first N trades. Once the Nth trade
        # completes, this branch no longer fires and the normal logic below
        # re-engages. Unset env = existing behaviour (Phase 4 + dev unchanged).
        # Out-of-range / unparseable values fall through silently (preflight
        # check #8 is the second belt that catches invalid values).
        # This bootstrap short-circuits BEFORE the validated_profitable and
        # blocked branches: operator setting the env var = accepting the
        # override (05-RESEARCH.md Open Question #6).
        bootstrap_raw = os.getenv("PHASE5_BOOTSTRAP_TRADES")
        if bootstrap_raw:
            try:
                bootstrap_limit = int(bootstrap_raw)
            except ValueError:
                bootstrap_limit = 0
            if 1 <= bootstrap_limit <= 5:
                completed = int(getattr(snapshot, "completed_executions", 0) or 0)
                if completed < bootstrap_limit:
                    remaining = bootstrap_limit - completed
                    return ReadinessCheck(
                        key="profitability",
                        status="pass",
                        summary=(
                            f"Phase 5 bootstrap: {remaining} trade(s) remaining "
                            f"before profitability gate re-engages"
                        ),
                        blocking=False,
                        details={
                            "verdict": snapshot.verdict,
                            "completed_executions": completed,
                            "bootstrap_limit": bootstrap_limit,
                            "bootstrap_remaining": remaining,
                        },
                    )
            # bootstrap_raw set but out-of-range / unparseable: fall through to
            # the existing logic (treat as absent). Preflight check #8 blocks
            # invalid values at startup so this fallthrough is only reached in
            # local dev / test setups where the env var leaked from elsewhere.

        details = {
            "verdict": snapshot.verdict,
            "progress": round(snapshot.progress, 4),
            "total_realized_pnl": round(snapshot.total_realized_pnl, 4),
            "completed_executions": snapshot.completed_executions,
        }
        if snapshot.verdict == "validated_profitable":
            return ReadinessCheck(
                key="profitability",
                status="pass",
                summary="Profitability gate is validated",
                blocking=True,
                details=details,
            )
        if snapshot.verdict in {"blocked", "not_profitable"}:
            return ReadinessCheck(
                key="profitability",
                status="fail",
                summary=f"Profitability verdict is {snapshot.verdict}",
                blocking=True,
                details=details,
            )
        return ReadinessCheck(
            key="profitability",
            status="warning",
            summary=f"Profitability is still collecting evidence ({snapshot.verdict})",
            blocking=True,
            details=details,
        )

    def _check_incidents(self) -> ReadinessCheck:
        incidents = list(getattr(self.engine, "incidents", []) or [])
        open_incidents = [incident for incident in incidents if getattr(incident, "status", "open") != "resolved"]
        critical = [incident for incident in open_incidents if str(getattr(incident, "severity", "")).lower() == "critical"]
        if critical:
            return ReadinessCheck(
                key="incidents",
                status="fail",
                summary=f"{len(critical)} critical incidents remain unresolved",
                blocking=True,
                details={"critical_incidents": [incident.to_dict() for incident in critical[:5]]},
            )
        if open_incidents:
            return ReadinessCheck(
                key="incidents",
                status="warning",
                summary=f"{len(open_incidents)} non-critical incidents are still open",
                blocking=False,
            )
        return ReadinessCheck(
            key="incidents",
            status="pass",
            summary="No open incidents are blocking execution",
            blocking=True,
        )

    def _check_balances(self) -> ReadinessCheck:
        balances = getattr(self.monitor, "current_balances", {}) or {}
        if not balances:
            return ReadinessCheck(
                key="balances",
                status="warning",
                summary="Venue balances have not been observed yet",
                blocking=True,
            )
        low = [platform for platform, snapshot in balances.items() if snapshot.is_low]
        if low:
            return ReadinessCheck(
                key="balances",
                status="fail",
                summary=f"Low balances detected on {', '.join(sorted(low))}",
                blocking=True,
                details={
                    platform: {
                        "balance": round(snapshot.balance, 4),
                        "is_low": snapshot.is_low,
                        "timestamp": snapshot.timestamp,
                    }
                    for platform, snapshot in balances.items()
                },
            )
        return ReadinessCheck(
            key="balances",
            status="pass",
            summary="Venue balances are above configured thresholds",
            blocking=True,
            details={
                platform: {
                    "balance": round(snapshot.balance, 4),
                    "is_low": snapshot.is_low,
                    "timestamp": snapshot.timestamp,
                }
                for platform, snapshot in balances.items()
            },
        )

    def _check_collectors(self) -> ReadinessCheck:
        if not self.collectors:
            return ReadinessCheck(
                key="collectors",
                status="warning",
                summary="Collector telemetry is unavailable",
                blocking=True,
            )

        details: Dict[str, Dict[str, Any]] = {}
        failures: List[str] = []
        warming_up: List[str] = []
        for name, collector in self.collectors.items():
            circuit_stats = getattr(getattr(collector, "circuit", None), "stats", None)
            gamma_circuit = getattr(getattr(collector, "circuit_gamma", None), "stats", None)
            clob_circuit = getattr(getattr(collector, "circuit_clob", None), "stats", None)
            entry = {
                "total_fetches": int(getattr(collector, "total_fetches", 0)),
                "total_errors": int(getattr(collector, "total_errors", 0)),
                "consecutive_errors": int(getattr(collector, "consecutive_errors", 0)),
            }
            if circuit_stats:
                entry["circuit"] = dict(circuit_stats)
            if gamma_circuit:
                entry["gamma_circuit"] = dict(gamma_circuit)
            if clob_circuit:
                entry["clob_circuit"] = dict(clob_circuit)
            details[name] = entry

            if entry["total_fetches"] <= 0:
                warming_up.append(name)

            circuit_states = [
                entry.get("circuit", {}).get("state"),
                entry.get("gamma_circuit", {}).get("state"),
                entry.get("clob_circuit", {}).get("state"),
            ]
            if any(state == "open" for state in circuit_states if state):
                failures.append(name)
            elif entry["consecutive_errors"] >= 5:
                failures.append(name)

        if failures:
            return ReadinessCheck(
                key="collectors",
                status="fail",
                summary=f"Collector health is degraded: {', '.join(sorted(failures))}",
                blocking=True,
                details=details,
            )
        if warming_up:
            return ReadinessCheck(
                key="collectors",
                status="warning",
                summary=f"Collectors are still warming up: {', '.join(sorted(warming_up))}",
                blocking=True,
                details=details,
            )
        return ReadinessCheck(
            key="collectors",
            status="pass",
            summary="Collectors are publishing healthy live data",
            blocking=True,
            details=details,
        )

    def _check_reconciliation(self) -> ReadinessCheck:
        latest_report = getattr(getattr(self.reconciler, "stats", {}), "get", None)
        if self.reconciler is None or latest_report is None:
            return ReadinessCheck(
                key="reconciliation",
                status="manual",
                summary="PnL reconciliation is not wired into the runtime yet",
                blocking=False,
            )

        report = self.reconciler.stats.get("latest_report")
        if not report:
            return ReadinessCheck(
                key="reconciliation",
                status="warning",
                summary="PnL reconciliation has not produced a report yet",
                blocking=False,
            )
        if report.get("has_flags"):
            return ReadinessCheck(
                key="reconciliation",
                status="fail",
                summary="PnL reconciliation drift is flagged",
                blocking=True,
                details=report,
            )
        return ReadinessCheck(
            key="reconciliation",
            status="pass",
            summary="PnL reconciliation shows no drift",
            blocking=False,
            details=report,
        )
