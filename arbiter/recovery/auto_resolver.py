"""AutoResolver — verify-before-act recovery automation (2026-05-22).

This module automates the operator-applied recovery surface that the
2026-05-22 audit (commits 4b4cb0b, c95c861) built up manually. Every
operation here is gated on an independent piece of evidence — never on the
in-memory state of the thing being recovered — so we cannot silently undo
the safety invariants the audit established.

Design rules:

1. **Verify before act.** We never mark an arb reconciled, expire an
   incident, or recommend disarming the kill switch without an
   independent corroborating signal. The signal is documented in each
   method's docstring.

2. **The kill-switch classifier never disarms.** Today's ``persist=False``
   fix for SIGTERM (commit c95c861) already prevents the bug we used to
   need silent recovery for; Redis stays clean across deploys. Anything
   still armed past that point is operator-meaningful, so the most this
   module ever does is *recommend* a disarm via Telegram + safety_events
   audit row. Disarming itself stays on the dashboard.

3. **Active positions are sacred.** Stale-incident expiry and any future
   automated cleanup MUST skip records whose arb still has open legs in
   ``execution_orders`` or open positions in the ledger.

4. **Warm restart is opt-in and reversible.** Set
   ``WARM_RESTART_ENABLED=true`` and have at least one historical fully
   filled arb in the database; the module returns a ``WarmRestartDecision``
   the readiness gate can consult, but does not mutate any config itself.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Sequence, Tuple

from ..execution.store import NAKED_LEG_RECONCILED_SENTINEL

if TYPE_CHECKING:  # pragma: no cover
    from ..execution.store import ExecutionStore
    from ..monitor.balance import TelegramNotifier
    from ..safety.persistence import SafetyEventStore
    from ..safety.supervisor import SafetySupervisor

logger = logging.getLogger("arbiter.recovery.auto_resolver")


# ─── Configuration ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class AutoResolverConfig:
    """Tunables for the four AutoResolver mechanisms.

    Defaults are conservative on purpose: the half-recorded reconciler
    requires both the auto-unwind evidence AND an age threshold, the
    incident expirer keeps critical rows around twice as long as
    warnings, and warm-restart is OFF unless explicitly enabled.
    """

    # Component A — half-recorded arb auto-resolver
    half_recorded_min_age_seconds: float = 60.0 * 60.0  # 1 hour
    # Component D — stale incident auto-expirer
    non_critical_expiry_seconds: float = 24.0 * 60.0 * 60.0  # 24h
    critical_expiry_seconds: float = 48.0 * 60.0 * 60.0  # 48h
    # Component C — readiness gate warm restart
    warm_restart_enabled: bool = field(
        default_factory=lambda: os.getenv("WARM_RESTART_ENABLED", "false").lower()
        == "true"
    )
    warm_restart_min_historical_arbs: int = 1
    warm_restart_reduced_scans: int = 10
    warm_restart_reduced_opps: int = 3
    cold_start_scans: int = 245
    cold_start_opps: int = 25


# Strings (case-insensitive substrings) that mark an arm reason as
# OPERATOR-DRIVEN ("genuine safety") and therefore NOT eligible for an
# auto-disarm recommendation. Listed explicitly so additions are reviewed.
_GENUINE_ARM_MARKERS: Tuple[str, ...] = (
    "loss",
    "threshold",
    "limit",
    "operator",
    "manual",
    "failure",
    "failed",
    "error",
    "panic",
    "drift",
    "reconciliation",
    "exposure",
    "incident",
)

# Substrings that mark an arm reason as BENIGN (SIGTERM-induced or
# shutdown drain). The c95c861 fix means Redis should not retain these
# anymore, but real production state can still drift, and a benign arm
# observed at runtime via the dashboard is also worth recommending the
# operator clear.
_BENIGN_ARM_MARKERS: Tuple[str, ...] = (
    "shutdown",
    "sigterm",
    "drain",
    "restart",
    "graceful",
)


# ─── Result types ──────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ReconcileResult:
    """One arb's outcome from ``AutoResolver.reconcile_half_recorded``."""

    arb_id: str
    outcome: "ReconcileOutcome"
    reason: str
    unwind_pnl: Optional[float] = None


class ReconcileOutcome:
    """String constants for ReconcileResult.outcome."""

    APPLIED = "applied"
    SKIPPED_NO_EVIDENCE = "skipped_no_evidence"
    SKIPPED_TOO_YOUNG = "skipped_too_young"
    SKIPPED_ALREADY_RECONCILED = "skipped_already_reconciled"
    SKIPPED_OPEN_POSITION = "skipped_open_position"
    ERROR = "error"


class KillSwitchVerdict:
    """String constants for KillSwitchClassification.verdict."""

    NOT_ARMED = "not_armed"
    BENIGN = "benign"
    GENUINE = "genuine"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class KillSwitchClassification:
    """One pass of ``AutoResolver.classify_kill_switch``.

    ``recommend_manual_disarm`` is True only when the verdict is BENIGN
    AND every precondition (credentials, price feeds, no active loss
    event) passes. This is a *recommendation*; the dashboard / operator
    still performs the actual disarm.
    """

    verdict: str  # see KillSwitchVerdict
    armed_reason: str
    armed_by: str
    recommend_manual_disarm: bool
    blocking_reasons: Tuple[str, ...]


@dataclass(frozen=True)
class WarmRestartDecision:
    """Output of ``AutoResolver.warm_restart_decision``."""

    enabled: bool
    historical_arb_count: int
    scans_required: int
    opps_required: int
    reason: str


@dataclass(frozen=True)
class StaleIncidentExpiry:
    """One incident's outcome from ``AutoResolver.expire_stale_incidents``."""

    incident_id: str
    arb_id: Optional[str]
    severity: str
    age_seconds: float
    outcome: str  # "expired" | "skipped_active_position" | "skipped_too_young"


# ─── Main resolver ─────────────────────────────────────────────────────────


class AutoResolver:
    """Owns the four recovery automations.

    Constructor injection mirrors ``SafetySupervisor`` — the resolver
    takes the same Postgres-backed stores and the optional notifier, so
    a test can wire in stubs for everything without touching real I/O.
    """

    def __init__(
        self,
        *,
        store: "ExecutionStore",
        safety_store: Optional["SafetyEventStore"] = None,
        supervisor: Optional["SafetySupervisor"] = None,
        notifier: Optional["TelegramNotifier"] = None,
        config: Optional[AutoResolverConfig] = None,
        clock: Optional[Any] = None,
    ):
        self.store = store
        self.safety_store = safety_store
        self.supervisor = supervisor
        self.notifier = notifier
        self.config = config or AutoResolverConfig()
        self._clock = clock or time.time

    # ─── Component A: half-recorded arb auto-reconciler ───────────────────

    async def reconcile_half_recorded(self) -> List[ReconcileResult]:
        """Reconcile asymmetric-fill arbs whose unwind is already booked.

        Verification gate (this is the safety contract):
        We never mark an arb as ``[naked-leg-reconciled]`` purely from the
        DB state of its leg rows. We only do so when there is an
        ``Auto-unwind closed N/N`` warning incident on the SAME arb_id —
        that incident is written by ``arbiter.execution.engine`` only
        after a successful platform API call closed the venue position
        and the engine confirmed the fill count. So a present auto-unwind
        incident is independent evidence that the exchange already booked
        the closure (this is exactly what the manual 2026-05-22 backfill
        relied on).

        Arbs younger than ``half_recorded_min_age_seconds`` are skipped —
        a fresh asymmetric fill might still be inside the engine's own
        runtime-recovery loop and reconciling it from underneath the
        engine would clobber that loop.

        For each arb we report a ``ReconcileResult`` so callers (and
        operator audits) can see what was done and what was deferred.
        """
        try:
            arbs = await self.store.list_half_recorded_arbs()
        except Exception as exc:
            logger.exception(
                "auto_resolver: list_half_recorded_arbs failed: %s", exc
            )
            return []

        results: List[ReconcileResult] = []
        now = float(self._clock())
        for arb in arbs:
            arb_id = arb.get("arb_id")
            if not arb_id:
                continue
            try:
                result = await self._reconcile_one(arb, now)
            except Exception as exc:
                logger.exception(
                    "auto_resolver: reconcile_one failed for arb=%s: %s",
                    arb_id, exc,
                )
                result = ReconcileResult(
                    arb_id=arb_id,
                    outcome=ReconcileOutcome.ERROR,
                    reason=f"exception: {exc!r}",
                )
            results.append(result)
            await self._notify_reconcile(result)
        return results

    async def _reconcile_one(
        self, arb: Dict[str, Any], now: float
    ) -> ReconcileResult:
        arb_id: str = arb["arb_id"]

        # Age gate — fresh arbs are still owned by the engine's own
        # recovery loop. We only touch arbs that have aged past the
        # configured threshold.
        created_at = arb.get("created_at")
        age_seconds = _age_seconds(created_at, now)
        if age_seconds < self.config.half_recorded_min_age_seconds:
            return ReconcileResult(
                arb_id=arb_id,
                outcome=ReconcileOutcome.SKIPPED_TOO_YOUNG,
                reason=(
                    f"age {age_seconds:.0f}s "
                    f"< {self.config.half_recorded_min_age_seconds:.0f}s"
                ),
            )

        # Independent-evidence gate — was an auto-unwind incident
        # recorded for this arb? That incident is the engine's proof
        # that a platform API call closed the venue position.
        unwind = await self._lookup_auto_unwind_incident(arb_id)
        if unwind is None:
            return ReconcileResult(
                arb_id=arb_id,
                outcome=ReconcileOutcome.SKIPPED_NO_EVIDENCE,
                reason="no auto_unwind incident found for arb_id",
            )

        # Active-position gate — if the arb still has any non-terminal
        # leg, do NOT mark it reconciled, even with auto-unwind
        # evidence. This protects against an exchange API call that
        # recorded the incident but failed to terminate the leg row.
        if await self._has_open_legs(arb_id):
            return ReconcileResult(
                arb_id=arb_id,
                outcome=ReconcileOutcome.SKIPPED_OPEN_POSITION,
                reason="execution_orders still has a non-terminal leg for arb_id",
            )

        unwind_pnl = _extract_unwind_pnl(unwind)
        note = (
            f"AutoResolver (2026-05-22): unwind_pnl={unwind_pnl} sourced "
            f"from auto_unwind incident metadata."
        )
        try:
            await self.store.mark_naked_leg_reconciled(
                arb_id, unwind_pnl=unwind_pnl, note=note
            )
        except Exception as exc:
            logger.exception(
                "auto_resolver: mark_naked_leg_reconciled failed arb=%s: %s",
                arb_id, exc,
            )
            return ReconcileResult(
                arb_id=arb_id,
                outcome=ReconcileOutcome.ERROR,
                reason=f"mark_naked_leg_reconciled raised: {exc!r}",
                unwind_pnl=unwind_pnl,
            )

        # Audit log — CRITICAL so the action is visible in the same
        # logging tier as kill-switch trips (per 2026-05-22 audit).
        logger.critical(
            "auto_resolver: APPLIED naked-leg sentinel arb=%s unwind_pnl=%s",
            arb_id, unwind_pnl,
        )
        return ReconcileResult(
            arb_id=arb_id,
            outcome=ReconcileOutcome.APPLIED,
            reason="auto_unwind evidence present; sentinel applied",
            unwind_pnl=unwind_pnl,
        )

    async def _lookup_auto_unwind_incident(
        self, arb_id: str
    ) -> Optional[Dict[str, Any]]:
        """Find an ``Auto-unwind closed N/N`` warning incident for an arb.

        Returns the row dict (with ``metadata`` already parsed) or None.
        We use a SELECT against ``execution_incidents`` because the
        ``ExecutionStore`` does not expose a typed list-by-arb helper —
        keeping the SQL local to this module means we don't have to
        widen the store's surface for a recovery-only consumer.
        """
        pool = getattr(self.store, "_pool", None)
        if pool is None:
            try:
                await self.store.connect()
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning(
                    "auto_resolver: store.connect failed during evidence "
                    "lookup arb=%s: %s",
                    arb_id, exc,
                )
                return None
            pool = getattr(self.store, "_pool", None)
            if pool is None:
                return None
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT incident_id, metadata
                  FROM execution_incidents
                 WHERE arb_id = $1
                   AND message LIKE 'Auto-unwind closed%'
                 ORDER BY created_at DESC
                 LIMIT 1
                """,
                arb_id,
            )
        if row is None:
            return None
        return {
            "incident_id": row["incident_id"],
            "metadata": _coerce_jsonb(row["metadata"]),
        }

    async def _has_open_legs(self, arb_id: str) -> bool:
        pool = getattr(self.store, "_pool", None)
        if pool is None:
            return False
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT COUNT(*)::int AS open_count
                  FROM execution_orders
                 WHERE arb_id = $1
                   AND status IN ('pending', 'submitted', 'partial')
                """,
                arb_id,
            )
        if row is None:
            return False
        return int(row["open_count"] or 0) > 0

    async def _notify_reconcile(self, result: ReconcileResult) -> None:
        """Telegram-alert the operator when we actually applied the sentinel.

        Skips for all the "skipped" outcomes — those are observability,
        not actions. Failures in the notifier never propagate.
        """
        if result.outcome != ReconcileOutcome.APPLIED:
            return
        if self.notifier is None:
            return
        try:
            from ..notifiers.fmt import DIVIDER, h, usd as fmt_usd
            pnl_val = float(result.unwind_pnl or 0.0)
            pnl_icon = "\U0001f4c8" if pnl_val >= 0 else "\U0001f4c9"
            msg = (
                f"♻️ <b>RECONCILED ARB</b>\n"
                f"{DIVIDER}\n"
                f"\U0001f4cb Arb: <code>{h(result.arb_id)}</code>\n"
                f"{pnl_icon} Unwind P&amp;L: <code>{fmt_usd(pnl_val, signed=True)}</code>\n"
                f"{DIVIDER}\n"
                f"<i>Half-recorded arb resolved via auto_unwind metadata.</i>"
            )
            await self.notifier.send(
                msg, dedup_key=f"auto_resolver_reconcile:{result.arb_id}"
            )
        except Exception as exc:
            logger.warning(
                "auto_resolver: telegram reconcile alert failed arb=%s: %s",
                result.arb_id, exc,
            )

    # ─── Component B: kill-switch classifier (never disarms) ──────────────

    async def classify_kill_switch(
        self,
        *,
        credentials_ok: bool = True,
        price_feeds_flowing: bool = True,
        active_loss_event: bool = False,
    ) -> KillSwitchClassification:
        """Classify the current kill-switch arm without changing state.

        The caller hands in three preconditions that the resolver alone
        cannot verify (the resolver has no adapter handles). If the arm
        is BENIGN AND every precondition is True, the resolver
        recommends a manual disarm via Telegram + a ``recommend_disarm``
        row in ``safety_events``. It does NOT call ``reset_kill`` —
        that path stays operator-only.

        The classification taxonomy:
          NOT_ARMED — supervisor reports armed=False; nothing to do.
          BENIGN    — arm reason matches a known SIGTERM/restart string.
          GENUINE   — arm reason matches a loss/incident/operator marker.
          UNKNOWN   — arm reason doesn't match either list; we keep our
                      hands off until an operator labels it.
        """
        supervisor = self.supervisor
        if supervisor is None or not getattr(supervisor, "is_armed", False):
            return KillSwitchClassification(
                verdict=KillSwitchVerdict.NOT_ARMED,
                armed_reason="",
                armed_by="",
                recommend_manual_disarm=False,
                blocking_reasons=(),
            )

        armed_by = str(getattr(supervisor, "armed_by", "") or "")
        armed_reason = str(
            getattr(getattr(supervisor, "_state", None), "armed_reason", "")
            or ""
        )

        verdict = _classify_arm_reason(armed_by, armed_reason)

        # Recovery probe: when the kill switch was armed by a transient DB
        # failure, check whether the database has since recovered.  If it
        # has AND there are no open positions, reclassify as BENIGN so the
        # auto_resolver recommends a manual disarm instead of letting the
        # system stay muted indefinitely.  This covers the common scenario
        # where a momentary Postgres hiccup arms the kill switch and the
        # DB comes back seconds later, but the flag stays latched forever.
        if (
            verdict == KillSwitchVerdict.GENUINE
            and armed_by == "execution_engine:db_failure"
        ):
            try:
                db_ok = await self._db_is_healthy_now()
                no_exposure = not await self._has_any_open_positions() if db_ok else False
                if db_ok and no_exposure:
                    verdict = KillSwitchVerdict.BENIGN
                    logger.warning(
                        "auto_resolver: db_failure arm reclassified BENIGN -- "
                        "DB is healthy, no open positions "
                        "(armed_by=%s reason=%s)",
                        armed_by, armed_reason,
                    )
            except Exception as exc:
                logger.warning(
                    "auto_resolver: db_failure recovery probe failed: %s", exc,
                )

        blocking: List[str] = []
        if not credentials_ok:
            blocking.append("credentials check failed")
        if not price_feeds_flowing:
            blocking.append("price feeds are not flowing")
        if active_loss_event:
            blocking.append("an active loss event is in progress")

        recommend = (
            verdict == KillSwitchVerdict.BENIGN and not blocking
        )

        if recommend:
            await self._propose_disarm(armed_by, armed_reason)
            logger.critical(
                "auto_resolver: kill-switch BENIGN arm detected — "
                "recommending operator disarm (armed_by=%s reason=%s)",
                armed_by, armed_reason,
            )
        else:
            logger.warning(
                "auto_resolver: kill-switch classified verdict=%s armed_by=%s "
                "reason=%s blocking=%s",
                verdict, armed_by, armed_reason, blocking,
            )

        return KillSwitchClassification(
            verdict=verdict,
            armed_reason=armed_reason,
            armed_by=armed_by,
            recommend_manual_disarm=recommend,
            blocking_reasons=tuple(blocking),
        )

    async def _propose_disarm(self, armed_by: str, armed_reason: str) -> None:
        # Telegram first — best-effort; never propagate failures.
        if self.notifier is not None:
            try:
                from ..notifiers.fmt import DIVIDER, h
                await self.notifier.send(
                    f"\U0001f7e1 <b>RECOMMEND RESET</b>\n"
                    f"{DIVIDER}\n"
                    f"\U0001f464 Armed by: <code>{h(armed_by or 'unknown')}</code>\n"
                    f"\U0001f4dd Reason: {h(armed_reason or 'unknown')}\n"
                    f"{DIVIDER}\n"
                    f"✅ Pre-checks passed. Safe to reset.\n"
                    f"⚠️ <i>Operator action required — AutoResolver does NOT auto-disarm.</i>",
                    dedup_key=f"auto_resolver_disarm_propose:{armed_by}",
                )
            except Exception as exc:
                logger.warning(
                    "auto_resolver: telegram disarm-proposal failed: %s", exc,
                )
        # Audit row — write a ``recommend_disarm`` event so the operator
        # can see in /api/safety/events that the resolver fired.
        if self.safety_store is not None:
            try:
                await self.safety_store.insert_safety_event(
                    event_type="recommend_disarm",
                    actor="system:auto_resolver",
                    reason=(
                        f"benign arm classified (armed_by={armed_by}, "
                        f"reason={armed_reason}); operator action recommended"
                    ),
                    state={},
                    cancelled_counts=None,
                )
            except Exception as exc:
                logger.warning(
                    "auto_resolver: safety_events recommend_disarm insert "
                    "failed: %s", exc,
                )


    async def _db_is_healthy_now(self) -> bool:
        """Probe the database with a trivial SELECT to confirm connectivity.

        Used by ``classify_kill_switch`` to detect when a transient
        ``execution_engine:db_failure`` arm has healed -- the DB is back and
        there are no open positions, so the kill switch can safely be
        recommended for disarm rather than staying armed indefinitely.

        Returns True only when the pool is reachable AND the query succeeds;
        any exception returns False (fail-closed).
        """
        pool = getattr(self.store, "_pool", None)
        if pool is None:
            try:
                await self.store.connect()
            except Exception:
                return False
            pool = getattr(self.store, "_pool", None)
            if pool is None:
                return False
        try:
            async with pool.acquire() as conn:
                row = await conn.fetchrow("SELECT 1 AS ok")
                return row is not None and row["ok"] == 1
        except Exception:
            return False

    async def _has_any_open_positions(self) -> bool:
        """Return True if any execution_orders row is still non-terminal.

        A non-terminal order means the system may have live venue exposure.
        The kill switch must NOT be recommended for disarm while exposure
        exists, even if the DB is healthy.
        """
        pool = getattr(self.store, "_pool", None)
        if pool is None:
            return True  # fail-closed: assume exposure exists
        try:
            async with pool.acquire() as conn:
                row = await conn.fetchrow(
                    """
                    SELECT COUNT(*)::int AS n
                      FROM execution_orders
                     WHERE status IN ('pending', 'submitted', 'partial')
                    """
                )
            return int(row["n"] or 0) > 0 if row else True
        except Exception:
            return True  # fail-closed

    # ─── Component C: warm-restart eligibility for readiness gate ─────────

    async def warm_restart_decision(self) -> WarmRestartDecision:
        """Return the readiness gate's scan/opp thresholds for this boot.

        When ``WARM_RESTART_ENABLED`` is True AND the database shows at
        least ``warm_restart_min_historical_arbs`` historical successful
        arb executions, we return the reduced sample sizes
        (``warm_restart_reduced_*``). Otherwise we return the
        cold-start defaults. The readiness gate is responsible for
        actually consulting this; we never mutate the gate from here.
        """
        if not self.config.warm_restart_enabled:
            return WarmRestartDecision(
                enabled=False,
                historical_arb_count=0,
                scans_required=self.config.cold_start_scans,
                opps_required=self.config.cold_start_opps,
                reason="WARM_RESTART_ENABLED is false",
            )

        try:
            count = await self._count_historical_successful_arbs()
        except Exception as exc:
            logger.exception(
                "auto_resolver: historical arb count failed; "
                "falling back to cold-start defaults: %s", exc,
            )
            return WarmRestartDecision(
                enabled=False,
                historical_arb_count=0,
                scans_required=self.config.cold_start_scans,
                opps_required=self.config.cold_start_opps,
                reason=f"historical-count lookup failed: {exc!r}",
            )

        if count < self.config.warm_restart_min_historical_arbs:
            return WarmRestartDecision(
                enabled=False,
                historical_arb_count=count,
                scans_required=self.config.cold_start_scans,
                opps_required=self.config.cold_start_opps,
                reason=(
                    f"only {count} historical successful arb(s), need "
                    f">= {self.config.warm_restart_min_historical_arbs}"
                ),
            )

        logger.warning(
            "auto_resolver: warm restart engaged; %d historical successful "
            "arb(s); reduced gates: scans=%d opps=%d",
            count,
            self.config.warm_restart_reduced_scans,
            self.config.warm_restart_reduced_opps,
        )
        return WarmRestartDecision(
            enabled=True,
            historical_arb_count=count,
            scans_required=self.config.warm_restart_reduced_scans,
            opps_required=self.config.warm_restart_reduced_opps,
            reason=f"{count} historical successful arb(s) found",
        )

    async def _count_historical_successful_arbs(self) -> int:
        pool = getattr(self.store, "_pool", None)
        if pool is None:
            try:
                await self.store.connect()
            except Exception:
                return 0
            pool = getattr(self.store, "_pool", None)
            if pool is None:
                return 0
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT COUNT(*)::int AS n
                  FROM execution_arbs
                 WHERE status IN ('filled', 'closed')
                   AND COALESCE(realized_pnl, 0) > 0
                """
            )
        return int(row["n"] or 0) if row else 0

    # ─── Component D: stale incident auto-expirer ─────────────────────────

    async def expire_stale_incidents(self) -> List[StaleIncidentExpiry]:
        """Close incidents that have aged past their severity-specific TTL.

        Hard rule: if the incident's ``arb_id`` has ANY non-terminal leg
        in ``execution_orders``, we skip it. An open leg is the strongest
        possible "do not touch" signal — silently expiring an incident
        attached to a real position would mute exactly the kind of risk
        the incident exists to surface.

        We update ``status='expired'`` (not ``resolved``) and stamp
        ``resolution_note`` so the audit trail distinguishes operator
        resolution from automated expiry.
        """
        pool = getattr(self.store, "_pool", None)
        if pool is None:
            try:
                await self.store.connect()
            except Exception:
                return []
            pool = getattr(self.store, "_pool", None)
            if pool is None:
                return []

        now = float(self._clock())
        results: List[StaleIncidentExpiry] = []
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT incident_id, arb_id, severity,
                       EXTRACT(EPOCH FROM (NOW() - created_at)) AS age_s
                  FROM execution_incidents
                 WHERE status = 'open'
                """
            )
        for row in rows or []:
            severity = str(row["severity"] or "").lower()
            age_s = float(row["age_s"] or 0.0)
            threshold = (
                self.config.critical_expiry_seconds
                if severity == "critical"
                else self.config.non_critical_expiry_seconds
            )
            incident_id = row["incident_id"]
            arb_id = row["arb_id"]
            if age_s < threshold:
                results.append(
                    StaleIncidentExpiry(
                        incident_id=incident_id,
                        arb_id=arb_id,
                        severity=severity,
                        age_seconds=age_s,
                        outcome="skipped_too_young",
                    )
                )
                continue
            # Stranded-position incidents use a synthetic arb_id
            # (``STRAND-{venue}-{hash}``, stranded_reconciler.py) that never
            # has a matching execution_orders row, so ``_has_open_legs``
            # below is always blind to them — the exact incident type that
            # represents real, still-open naked risk. 2026-07-15: this let
            # incidents for 8 real stranded positions auto-expire at the 24h
            # TTL and then never re-alert (the reconciler only emits on a
            # *new* tracked key, not on TTL re-expiry), leaving a ~24h
            # window with zero visibility into live venue exposure. Treat
            # any STRAND-* incident as inherently non-terminal — require
            # explicit operator resolution via the dashboard resolve
            # endpoint instead of a silent TTL expiry.
            has_open = (arb_id or "").startswith("STRAND-") or (
                arb_id and await self._has_open_legs(arb_id)
            )
            if has_open:
                logger.warning(
                    "auto_resolver: skipping stale-incident expiry — "
                    "arb=%s has open legs (incident=%s)",
                    arb_id, incident_id,
                )
                results.append(
                    StaleIncidentExpiry(
                        incident_id=incident_id,
                        arb_id=arb_id,
                        severity=severity,
                        age_seconds=age_s,
                        outcome="skipped_active_position",
                    )
                )
                continue

            note = (
                f"AutoResolver expired stale {severity} incident at "
                f"age={age_s:.0f}s (threshold={threshold:.0f}s)."
            )
            try:
                async with pool.acquire() as conn:
                    await conn.execute(
                        """
                        UPDATE execution_incidents
                           SET status = 'expired',
                               resolved_at = NOW(),
                               resolution_note = $2
                         WHERE incident_id = $1
                           AND status = 'open'
                        """,
                        incident_id,
                        note,
                    )
                logger.warning(
                    "auto_resolver: EXPIRED stale incident=%s severity=%s "
                    "age=%.0fs arb=%s",
                    incident_id, severity, age_s, arb_id,
                )
                results.append(
                    StaleIncidentExpiry(
                        incident_id=incident_id,
                        arb_id=arb_id,
                        severity=severity,
                        age_seconds=age_s,
                        outcome="expired",
                    )
                )
            except Exception as exc:
                logger.exception(
                    "auto_resolver: failed to expire incident=%s: %s",
                    incident_id, exc,
                )
        return results

    # ─── Run-once orchestrator ────────────────────────────────────────────

    async def run_once(
        self,
        *,
        credentials_ok: bool = True,
        price_feeds_flowing: bool = True,
        active_loss_event: bool = False,
    ) -> Dict[str, Any]:
        """Execute components A, B, D in sequence and return a summary.

        Component C (warm-restart) is read-only and consumed by the
        readiness gate at startup; we don't run it here. The order
        A → D matters: reconciling a half-recorded arb first means a
        downstream stale-incident pass won't trip the "open leg" guard
        unnecessarily.
        """
        reconciled = await self.reconcile_half_recorded()
        classification = await self.classify_kill_switch(
            credentials_ok=credentials_ok,
            price_feeds_flowing=price_feeds_flowing,
            active_loss_event=active_loss_event,
        )
        expired = await self.expire_stale_incidents()
        return {
            "reconciled": [
                {"arb_id": r.arb_id, "outcome": r.outcome, "reason": r.reason}
                for r in reconciled
            ],
            "kill_switch": {
                "verdict": classification.verdict,
                "recommend_manual_disarm": classification.recommend_manual_disarm,
                "blocking_reasons": list(classification.blocking_reasons),
            },
            "expired_incidents": [
                {
                    "incident_id": e.incident_id,
                    "outcome": e.outcome,
                    "severity": e.severity,
                    "age_seconds": e.age_seconds,
                }
                for e in expired
            ],
        }


# ─── Module-level helpers ──────────────────────────────────────────────────


def _classify_arm_reason(armed_by: str, armed_reason: str) -> str:
    """Pure-function classifier for an arm reason.

    Priority: GENUINE wins ties — if a reason matches both a benign and
    a genuine marker, we err on the side of NOT recommending a disarm.
    Operators can always recover from "too cautious"; the inverse is
    much worse.
    """
    haystack = f"{armed_by} {armed_reason}".lower()
    if any(marker in haystack for marker in _GENUINE_ARM_MARKERS):
        return KillSwitchVerdict.GENUINE
    if any(marker in haystack for marker in _BENIGN_ARM_MARKERS):
        return KillSwitchVerdict.BENIGN
    return KillSwitchVerdict.UNKNOWN


def _age_seconds(created_at: Any, now: float) -> float:
    """Best-effort epoch-seconds age. Handles datetimes and numeric timestamps."""
    if created_at is None:
        return 0.0
    try:
        # asyncpg returns timezone-aware datetimes.
        ts = float(created_at.timestamp())  # type: ignore[union-attr]
    except AttributeError:
        try:
            ts = float(created_at)
        except (TypeError, ValueError):
            return 0.0
    age = now - ts
    return max(age, 0.0)


def _extract_unwind_pnl(unwind_incident: Dict[str, Any]) -> float:
    metadata = unwind_incident.get("metadata") or {}
    raw = metadata.get("realized_pnl") or metadata.get("unwind_pnl") or 0.0
    try:
        return float(raw)
    except (TypeError, ValueError):
        return 0.0


def _coerce_jsonb(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return value
    if value is None:
        return {}
    try:
        import json

        loaded = json.loads(value)
        return loaded if isinstance(loaded, dict) else {}
    except Exception:
        return {}
