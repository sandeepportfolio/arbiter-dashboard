"""
ARBITER — Real-Time P&L Reconciliation System
Compares recorded trade P&L against actual platform balances.

Runs every 5 minutes (configurable). Alerts on discrepancy > $0.50.
Logs all reconciliation results for audit trail.
"""
import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger("arbiter.audit.reconciler")

# Reconciliation log directory
RECONCILIATION_LOG_DIR = Path(__file__).resolve().parent.parent / "logs" / "reconciliation"


@dataclass
class ReconciliationEntry:
    """One reconciliation check for a single platform."""
    platform: str
    recorded_pnl: float       # Sum of all recorded execution P&L
    platform_balance: float    # Actual balance from platform API
    starting_balance: float    # Balance at start of tracking period
    expected_balance: float    # starting_balance + recorded_pnl
    discrepancy: float         # expected_balance - platform_balance
    is_flagged: bool
    timestamp: float

    def to_dict(self) -> dict:
        return {
            "platform": self.platform,
            "recorded_pnl": round(self.recorded_pnl, 4),
            "platform_balance": round(self.platform_balance, 2),
            "starting_balance": round(self.starting_balance, 2),
            "expected_balance": round(self.expected_balance, 2),
            "discrepancy": round(self.discrepancy, 2),
            "is_flagged": self.is_flagged,
            "timestamp": self.timestamp,
        }


@dataclass
class ReconciliationReport:
    """Full reconciliation report across all platforms."""
    entries: List[ReconciliationEntry] = field(default_factory=list)
    total_recorded_pnl: float = 0.0
    total_discrepancy: float = 0.0
    has_flags: bool = False
    timestamp: float = 0.0

    def to_dict(self) -> dict:
        return {
            "entries": [e.to_dict() for e in self.entries],
            "total_recorded_pnl": round(self.total_recorded_pnl, 4),
            "total_discrepancy": round(self.total_discrepancy, 2),
            "has_flags": self.has_flags,
            "timestamp": self.timestamp,
        }


class PnLReconciler:
    """
    Reconciles recorded P&L against platform balances.

    Tracks starting balances per platform and compares:
      expected = starting_balance + sum(recorded_pnl)
      discrepancy = expected - actual_platform_balance

    Flags discrepancies > $0.50 for investigation.
    """

    def __init__(self, discrepancy_threshold: float = 0.50,
                 check_interval: float = 300.0,
                 log_to_disk: bool = True):
        self.threshold = discrepancy_threshold
        self.check_interval = check_interval
        self.log_to_disk = log_to_disk
        self._starting_balances: Dict[str, float] = {}
        self._recorded_pnl: Dict[str, float] = {}  # platform -> cumulative PnL
        self._reports: List[ReconciliationReport] = []
        self._running = False
        self._reconciliation_count = 0
        self._flag_count = 0

        if log_to_disk:
            RECONCILIATION_LOG_DIR.mkdir(parents=True, exist_ok=True)

    def set_starting_balance(self, platform: str, balance: float):
        """Set the starting balance for a platform (call at system start)."""
        self._starting_balances[platform] = balance
        if platform not in self._recorded_pnl:
            self._recorded_pnl[platform] = 0.0
        logger.info(f"Starting balance set: {platform} = ${balance:.2f}")

    def record_execution_pnl(self, platform: str, pnl: float):
        """Record P&L from a trade execution on a platform."""
        self._recorded_pnl[platform] = self._recorded_pnl.get(platform, 0.0) + pnl

    def record_arb_pnl(self, yes_platform: str, no_platform: str,
                        net_edge: float, qty: int):
        """
        Record P&L from an arb execution across two platforms.
        The profit is split conceptually — each platform contributes to the arb.
        For tracking, we attribute the full P&L to both platforms' exposure.
        """
        pnl = net_edge * qty
        # Each platform incurs cost on its leg; profit materializes at settlement.
        # Track as full PnL on both for conservative reconciliation.
        self._recorded_pnl[yes_platform] = self._recorded_pnl.get(yes_platform, 0.0) + pnl / 2
        self._recorded_pnl[no_platform] = self._recorded_pnl.get(no_platform, 0.0) + pnl / 2

    def load_execution_history(self, executions) -> None:
        """
        Rebuild recorded P&L from the current execution ledger.

        This keeps reconciliation deterministic across restarts and avoids
        double-counting when the runtime replays or rehydrates past executions.
        """
        self._recorded_pnl = {
            platform: 0.0
            for platform in self._starting_balances
        }

        for execution in executions or []:
            pnl = float(getattr(execution, "realized_pnl", 0.0) or 0.0)
            opportunity = getattr(execution, "opportunity", None)
            yes_platform = getattr(opportunity, "yes_platform", None)
            no_platform = getattr(opportunity, "no_platform", None)
            if pnl == 0.0 or not yes_platform or not no_platform:
                continue

            split_pnl = pnl / 2.0
            self._recorded_pnl[yes_platform] = self._recorded_pnl.get(yes_platform, 0.0) + split_pnl
            self._recorded_pnl[no_platform] = self._recorded_pnl.get(no_platform, 0.0) + split_pnl

    def reconcile(self, current_balances: Dict[str, float]) -> ReconciliationReport:
        """
        Run reconciliation against current platform balances.

        current_balances: {"kalshi": 450.00, "polymarket": 200.00, ...}
        """
        self._reconciliation_count += 1
        now = time.time()
        entries = []
        total_pnl = 0.0
        total_disc = 0.0
        has_flags = False

        for platform, actual_balance in current_balances.items():
            starting = self._starting_balances.get(platform, actual_balance)
            recorded = self._recorded_pnl.get(platform, 0.0)
            expected = starting + recorded
            discrepancy = expected - actual_balance

            is_flagged = abs(discrepancy) > self.threshold
            if is_flagged:
                has_flags = True
                self._flag_count += 1

            entry = ReconciliationEntry(
                platform=platform,
                recorded_pnl=recorded,
                platform_balance=actual_balance,
                starting_balance=starting,
                expected_balance=expected,
                discrepancy=discrepancy,
                is_flagged=is_flagged,
                timestamp=now,
            )
            entries.append(entry)
            total_pnl += recorded
            total_disc += discrepancy

            if is_flagged:
                logger.warning(
                    f"RECONCILIATION FLAG [{platform}]: "
                    f"expected=${expected:.2f} actual=${actual_balance:.2f} "
                    f"discrepancy=${discrepancy:.2f}"
                )
            else:
                logger.debug(
                    f"Reconciliation OK [{platform}]: "
                    f"expected=${expected:.2f} actual=${actual_balance:.2f} "
                    f"diff=${discrepancy:.2f}"
                )

        report = ReconciliationReport(
            entries=entries,
            total_recorded_pnl=total_pnl,
            total_discrepancy=total_disc,
            has_flags=has_flags,
            timestamp=now,
        )
        self._reports.append(report)

        if self.log_to_disk:
            self._write_log(report)

        return report

    def _write_log(self, report: ReconciliationReport):
        """Write reconciliation report to disk for audit trail."""
        ts = time.strftime("%Y%m%d_%H%M%S", time.localtime(report.timestamp))
        filename = RECONCILIATION_LOG_DIR / f"recon_{ts}.json"
        with open(filename, "w") as f:
            json.dump(report.to_dict(), f, indent=2)

    @property
    def stats(self) -> dict:
        return {
            "reconciliation_count": self._reconciliation_count,
            "flag_count": self._flag_count,
            "starting_balances": dict(self._starting_balances),
            "recorded_pnl": {k: round(v, 4) for k, v in self._recorded_pnl.items()},
            "latest_report": self._reports[-1].to_dict() if self._reports else None,
        }
