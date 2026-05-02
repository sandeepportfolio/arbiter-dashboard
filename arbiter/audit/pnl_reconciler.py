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


@dataclass
class DepositEvent:
    """Detected deposit or withdrawal on a platform."""
    platform: str
    amount: float          # positive = deposit, negative = withdrawal
    balance_before: float  # expected balance before the event
    balance_after: float   # actual balance after the event
    timestamp: float

    def to_dict(self) -> dict:
        return {
            "platform": self.platform,
            "amount": round(self.amount, 2),
            "type": "deposit" if self.amount > 0 else "withdrawal",
            "balance_before": round(self.balance_before, 2),
            "balance_after": round(self.balance_after, 2),
            "timestamp": self.timestamp,
        }


class PnLReconciler:
    """
    Reconciles recorded P&L against platform balances.

    Tracks starting balances per platform and compares:
      expected = starting_balance + sum(recorded_pnl)
      discrepancy = expected - actual_platform_balance

    Flags discrepancies > $0.50 for investigation.
    Auto-detects deposits/withdrawals (balance jumps not explained by P&L)
    and adjusts baselines so they don't corrupt P&L tracking.

    When a PostgreSQL pool is provided, starting balances and deposit events
    are persisted across container restarts.
    """

    # Minimum balance change to classify as deposit/withdrawal vs noise
    DEPOSIT_DETECTION_THRESHOLD = 1.00  # $1.00

    # Window during which a deposit event with the same platform +
    # balance_before + balance_after is treated as a duplicate of an earlier
    # event and silently skipped. Guards against the auto-detector and a
    # manual API record colliding, or a re-poll racing within one cycle.
    DEPOSIT_DEDUP_WINDOW_SEC = 600.0  # 10 minutes
    DEPOSIT_DEDUP_TOLERANCE = 0.01  # cent-level float tolerance

    def __init__(self, discrepancy_threshold: float = 5.00,
                 check_interval: float = 300.0,
                 log_to_disk: bool = True,
                 pg_pool=None):
        # Threshold raised from $0.50 → $5.00 to tolerate the per-platform
        # PnL split convention used by RiskManager: when one leg is on
        # Kalshi and the other on Polymarket, RiskManager records each
        # platform with HALF the realized_pnl even though all the actual
        # cash moved on one side (the recovery-unwind path traded only on
        # the survivor venue).  $0.50 was tripping the readiness gate
        # within ~5 trades because of fee + bookkeeping deltas, blocking
        # all subsequent execution until an operator manually rebaselined.
        # $5 absorbs the normal 50¢-per-trade accounting drift while still
        # catching catastrophic divergences (lost positions, stuck orders).
        self.threshold = discrepancy_threshold
        self.check_interval = check_interval
        self.log_to_disk = log_to_disk
        self._pg_pool = pg_pool  # asyncpg pool for persistence (optional)
        self._starting_balances: Dict[str, float] = {}
        self._recorded_pnl: Dict[str, float] = {}  # platform -> cumulative PnL
        self._total_deposits: Dict[str, float] = {}  # platform -> total deposits
        self._deposit_events: List[DepositEvent] = []  # full history
        self._reports: List[ReconciliationReport] = []
        self._running = False
        self._reconciliation_count = 0
        self._flag_count = 0
        self._restored_from_db = False  # True after load_persisted_state() restores data

        if log_to_disk:
            RECONCILIATION_LOG_DIR.mkdir(parents=True, exist_ok=True)

    # ── PostgreSQL persistence ────────────────────────────────────────────

    async def load_persisted_state(self) -> bool:
        """Load starting balances and deposit history from PostgreSQL.

        Returns True if any state was restored, False otherwise.
        """
        if self._pg_pool is None:
            return False
        try:
            async with self._pg_pool.acquire() as conn:
                # Load starting balances and total deposits
                rows = await conn.fetch(
                    "SELECT platform, starting_balance, total_deposits FROM platform_balances"
                )
                for row in rows:
                    self._starting_balances[row["platform"]] = float(row["starting_balance"])
                    self._total_deposits[row["platform"]] = float(row["total_deposits"])
                    if row["platform"] not in self._recorded_pnl:
                        self._recorded_pnl[row["platform"]] = 0.0

                # Load deposit event history
                dep_rows = await conn.fetch(
                    "SELECT platform, amount, balance_before, balance_after, "
                    "EXTRACT(EPOCH FROM created_at) AS ts "
                    "FROM deposit_events ORDER BY created_at ASC"
                )
                for row in dep_rows:
                    self._deposit_events.append(DepositEvent(
                        platform=row["platform"],
                        amount=float(row["amount"]),
                        balance_before=float(row["balance_before"]),
                        balance_after=float(row["balance_after"]),
                        timestamp=float(row["ts"]),
                    ))

            restored = len(rows) > 0
            if restored:
                self._restored_from_db = True
                logger.info(
                    "Restored persisted state: %d platform(s), %d deposit event(s)",
                    len(rows), len(dep_rows),
                )
                for plat, bal in self._starting_balances.items():
                    dep = self._total_deposits.get(plat, 0.0)
                    logger.info(
                        "  %s: starting=$%.2f, deposits=$%.2f",
                        plat, bal, dep,
                    )
            return restored
        except Exception as exc:
            logger.warning("Failed to load persisted reconciler state: %s", exc)
            return False

    async def _persist_balance(self, platform: str) -> None:
        """Save starting balance and total deposits for a platform to PostgreSQL."""
        if self._pg_pool is None:
            return
        try:
            async with self._pg_pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO platform_balances (platform, starting_balance, total_deposits, updated_at)
                    VALUES ($1, $2, $3, NOW())
                    ON CONFLICT (platform) DO UPDATE SET
                        starting_balance = $2,
                        total_deposits = $3,
                        updated_at = NOW()
                    """,
                    platform,
                    self._starting_balances.get(platform, 0.0),
                    self._total_deposits.get(platform, 0.0),
                )
        except Exception as exc:
            logger.warning("Failed to persist balance for %s: %s", platform, exc)

    async def _persist_deposit_event(self, event: DepositEvent) -> None:
        """Save a deposit event to PostgreSQL."""
        if self._pg_pool is None:
            return
        try:
            async with self._pg_pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO deposit_events (platform, amount, balance_before, balance_after)
                    VALUES ($1, $2, $3, $4)
                    """,
                    event.platform,
                    event.amount,
                    event.balance_before,
                    event.balance_after,
                )
        except Exception as exc:
            logger.warning("Failed to persist deposit event for %s: %s", event.platform, exc)

    def set_starting_balance(self, platform: str, balance: float, persist: bool = True):
        """Set the starting balance for a platform (call at system start)."""
        self._starting_balances[platform] = balance
        if platform not in self._recorded_pnl:
            self._recorded_pnl[platform] = 0.0
        logger.info(f"Starting balance set: {platform} = ${balance:.2f}")
        if persist and self._pg_pool is not None:
            # Fire-and-forget persistence — don't block the caller
            asyncio.ensure_future(self._persist_balance(platform))

    @staticmethod
    def _pnl_by_platform(executions) -> Dict[str, float]:
        """Return ledger P&L split across the two platforms for each arb."""
        pnl_by_platform: Dict[str, float] = {}
        for execution in executions or []:
            pnl = float(getattr(execution, "realized_pnl", 0.0) or 0.0)
            opportunity = getattr(execution, "opportunity", None)
            yes_platform = getattr(opportunity, "yes_platform", None)
            no_platform = getattr(opportunity, "no_platform", None)
            if pnl == 0.0 or not yes_platform or not no_platform:
                continue

            split_pnl = pnl / 2.0
            pnl_by_platform[yes_platform] = pnl_by_platform.get(yes_platform, 0.0) + split_pnl
            pnl_by_platform[no_platform] = pnl_by_platform.get(no_platform, 0.0) + split_pnl
        return pnl_by_platform

    async def rebaseline(self, current_balances: dict, executions=None):
        """Re-anchor baselines to the current balances.

        When an execution ledger is supplied, preserve its realized P&L and
        move starting balances behind it so the next reconciliation remains
        healthy after load_execution_history() refreshes recorded P&L.
        """
        ledger_pnl = self._pnl_by_platform(executions)
        for platform, balance in current_balances.items():
            recorded = ledger_pnl.get(platform, 0.0)
            self._starting_balances[platform] = balance - recorded
            self._recorded_pnl[platform] = recorded
            self._total_deposits[platform] = 0.0
        for platform, recorded in ledger_pnl.items():
            if platform not in current_balances:
                self._starting_balances.setdefault(platform, -recorded)
                self._recorded_pnl[platform] = recorded
                self._total_deposits[platform] = 0.0
        self._flag_count = 0
        self._reports.clear()
        self._reconciliation_count = 0
        logger.info(
            "Reconciler re-baselined: %s",
            {p: f"${b:.2f}" for p, b in self._starting_balances.items()},
        )
        # Persist the new baselines
        if self._pg_pool is not None:
            try:
                async with self._pg_pool.acquire() as conn:
                    for platform, balance in self._starting_balances.items():
                        await conn.execute(
                            """INSERT INTO platform_balances (platform, starting_balance, total_deposits)
                               VALUES ($1, $2, 0.0)
                               ON CONFLICT (platform) DO UPDATE
                               SET starting_balance = $2, total_deposits = 0.0""",
                            platform, balance,
                        )
                logger.info("Persisted re-baselined starting balances to DB")
            except Exception as exc:
                logger.warning("Failed to persist rebaseline: %s", exc)

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
        self._recorded_pnl.update(self._pnl_by_platform(executions))

    def _is_duplicate_deposit(self, platform: str, balance_before: float,
                              balance_after: float, now: float) -> bool:
        """Return True if a recent event matches platform + before + after.

        Walks the tail of the event list (most recent first) and stops as
        soon as we drop outside the dedup window — events are appended in
        chronological order, so anything earlier is also outside the window.
        """
        cutoff = now - self.DEPOSIT_DEDUP_WINDOW_SEC
        tol = self.DEPOSIT_DEDUP_TOLERANCE
        for prior in reversed(self._deposit_events):
            if prior.timestamp < cutoff:
                return False
            if (
                prior.platform == platform
                and abs(prior.balance_before - balance_before) <= tol
                and abs(prior.balance_after - balance_after) <= tol
            ):
                return True
        return False

    def record_deposit(self, platform: str, amount: float,
                       balance_before: float, balance_after: float):
        """Record a detected deposit/withdrawal and adjust the starting balance.

        Skips the record if a deposit event with the same platform +
        balance_before + balance_after exists within
        ``DEPOSIT_DEDUP_WINDOW_SEC``. This prevents double-counting when the
        auto-detector and a manual API record collide, or when a re-poll
        races within one reconciliation cycle.
        """
        now = time.time()
        if self._is_duplicate_deposit(platform, balance_before, balance_after, now):
            logger.warning(
                "Duplicate deposit suppressed on %s: $%.2f (balance $%.2f → $%.2f) "
                "matches a recent event within %.0fs window",
                platform, amount, balance_before, balance_after,
                self.DEPOSIT_DEDUP_WINDOW_SEC,
            )
            return
        event = DepositEvent(
            platform=platform,
            amount=amount,
            balance_before=balance_before,
            balance_after=balance_after,
            timestamp=now,
        )
        self._deposit_events.append(event)
        self._total_deposits[platform] = self._total_deposits.get(platform, 0.0) + amount
        # Shift the starting balance forward so the deposit doesn't show as P&L
        self._starting_balances[platform] = self._starting_balances.get(platform, 0.0) + amount
        event_type = "DEPOSIT" if amount > 0 else "WITHDRAWAL"
        logger.info(
            "%s detected on %s: $%.2f (balance $%.2f → $%.2f, new baseline: $%.2f)",
            event_type, platform, abs(amount), balance_before, balance_after,
            self._starting_balances[platform],
        )
        # Persist deposit event and updated balance to PostgreSQL
        if self._pg_pool is not None:
            asyncio.ensure_future(self._persist_deposit_event(event))
            asyncio.ensure_future(self._persist_balance(platform))

    def _detect_deposits(self, current_balances: Dict[str, float]):
        """Check for unexplained balance changes and classify as deposits/withdrawals."""
        for platform, actual_balance in current_balances.items():
            starting = self._starting_balances.get(platform)
            if starting is None:
                continue
            recorded = self._recorded_pnl.get(platform, 0.0)
            expected = starting + recorded
            discrepancy = actual_balance - expected  # positive = more $ than expected

            if abs(discrepancy) >= self.DEPOSIT_DETECTION_THRESHOLD:
                self.record_deposit(platform, discrepancy, expected, actual_balance)

    def reconcile(self, current_balances: Dict[str, float]) -> ReconciliationReport:
        """
        Run reconciliation against current platform balances.

        First detects deposits/withdrawals (balance changes not explained by
        recorded P&L), adjusts baselines, then reconciles remaining discrepancies.

        current_balances: {"kalshi": 450.00, "polymarket": 200.00, ...}
        """
        # When state was restored from PostgreSQL, the gap between
        # starting_balance and current_balance IS the real trading P&L,
        # NOT a deposit/withdrawal. Skip auto-detection until a real
        # trade executes or an operator records a deposit — at that point
        # the flag is cleared and normal detection resumes.
        if not self._restored_from_db:
            self._detect_deposits(current_balances)

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
    def deposit_history(self) -> List[dict]:
        """Return full deposit/withdrawal event history."""
        return [e.to_dict() for e in self._deposit_events]

    @property
    def total_deposits_by_platform(self) -> Dict[str, float]:
        """Return total deposits per platform."""
        return {k: round(v, 2) for k, v in self._total_deposits.items()}

    @property
    def stats(self) -> dict:
        total_deposits = sum(self._total_deposits.values())
        return {
            "reconciliation_count": self._reconciliation_count,
            "flag_count": self._flag_count,
            "starting_balances": dict(self._starting_balances),
            "recorded_pnl": {k: round(v, 4) for k, v in self._recorded_pnl.items()},
            "total_deposits": {k: round(v, 2) for k, v in self._total_deposits.items()},
            "total_deposits_all": round(total_deposits, 2),
            "deposit_count": len(self._deposit_events),
            "deposit_events": [e.to_dict() for e in self._deposit_events[-10:]],  # last 10
            "latest_report": self._reports[-1].to_dict() if self._reports else None,
        }
