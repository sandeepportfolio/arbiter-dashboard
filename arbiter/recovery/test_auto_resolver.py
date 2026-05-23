"""Tests for AutoResolver — the verify-before-act recovery automation.

These tests assert the load-bearing safety contracts:

  * Component A (half-recorded reconciler) ONLY applies the sentinel when an
    Auto-unwind incident is present AND the arb has no open legs AND it has
    aged past the threshold. It must NOT apply the sentinel from DB state
    alone.

  * Component B (kill-switch classifier) NEVER auto-disarms. It only
    recommends a disarm for benign arm reasons and only when every
    precondition passes; a genuine arm reason must keep the switch armed.

  * Component C (warm restart) is OFF unless the env flag is true AND there
    is database evidence of historical successful trading.

  * Component D (stale-incident expirer) NEVER expires an incident whose
    arb still has open legs in execution_orders.
"""
from __future__ import annotations

import datetime as dt
import time
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock

import pytest

from arbiter.execution.store import NAKED_LEG_RECONCILED_SENTINEL
from arbiter.recovery.auto_resolver import (
    AutoResolver,
    AutoResolverConfig,
    KillSwitchVerdict,
    ReconcileOutcome,
    _classify_arm_reason,
)


# ─── Stub Postgres pool ────────────────────────────────────────────────────


@dataclass
class _StubConn:
    """Single asyncpg connection that dispatches by SQL substring."""

    parent: "_StubPool"

    async def fetch(self, query: str, *params):
        if "FROM execution_incidents" in query and "status = 'open'" in query:
            return list(self.parent.open_incidents)
        return []

    async def fetchrow(self, query: str, *params):
        if "Auto-unwind closed" in query:
            arb_id = params[0]
            return self.parent.auto_unwind_by_arb.get(arb_id)
        if "FROM execution_orders" in query and "open_count" in query:
            arb_id = params[0]
            n = self.parent.open_legs_by_arb.get(arb_id, 0)
            return {"open_count": n}
        if "FROM execution_arbs" in query and "realized_pnl" in query:
            return {"n": self.parent.historical_arb_count}
        return None

    async def execute(self, query: str, *params):
        if query.lstrip().upper().startswith("UPDATE EXECUTION_INCIDENTS"):
            self.parent.expired_incidents.append(
                {"incident_id": params[0], "note": params[1]}
            )

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


@dataclass
class _StubPool:
    """In-memory pool. Tests mutate the public fields to set up scenarios."""

    auto_unwind_by_arb: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    open_legs_by_arb: Dict[str, int] = field(default_factory=dict)
    open_incidents: List[Dict[str, Any]] = field(default_factory=list)
    historical_arb_count: int = 0
    expired_incidents: List[Dict[str, Any]] = field(default_factory=list)

    def acquire(self):
        # async context manager — return a fresh _StubConn
        return _StubConn(parent=self)


class _StubStore:
    """Stand-in for ExecutionStore with a stubbed pool."""

    def __init__(self, pool: _StubPool, half_recorded: Optional[List[Dict]] = None):
        self._pool = pool
        self._half_recorded = list(half_recorded or [])
        self.mark_calls: List[Dict[str, Any]] = []

    async def connect(self):
        return None

    async def list_half_recorded_arbs(self) -> List[Dict[str, Any]]:
        return list(self._half_recorded)

    async def mark_naked_leg_reconciled(self, arb_id, *, unwind_pnl, note):
        self.mark_calls.append(
            {"arb_id": arb_id, "unwind_pnl": unwind_pnl, "note": note}
        )


class _StubSafetyStore:
    def __init__(self):
        self.events: List[Dict[str, Any]] = []

    async def insert_safety_event(
        self, *, event_type, actor, reason, state, cancelled_counts=None
    ):
        self.events.append(
            {
                "event_type": event_type,
                "actor": actor,
                "reason": reason,
                "state": state,
            }
        )


def _make_supervisor(armed: bool, armed_by: str = "", armed_reason: str = ""):
    state = SimpleNamespace(armed_reason=armed_reason)
    return SimpleNamespace(
        is_armed=armed,
        armed_by=armed_by if armed else None,
        _state=state,
    )


def _ts(seconds_ago: float, now: float) -> dt.datetime:
    return dt.datetime.fromtimestamp(now - seconds_ago, tz=dt.timezone.utc)


# ─── Component A: half-recorded reconciler ─────────────────────────────────


@pytest.mark.asyncio
async def test_reconcile_applies_sentinel_when_auto_unwind_evidence_present():
    now = 1_700_000_000.0
    pool = _StubPool(
        auto_unwind_by_arb={
            "ARB-000999": {
                "incident_id": "INC-1",
                "metadata": {"realized_pnl": -4.7, "event_type": "auto_unwind"},
            }
        },
    )
    store = _StubStore(
        pool,
        half_recorded=[
            {
                "arb_id": "ARB-000999",
                "canonical_id": "X",
                "status": "failed",
                "created_at": _ts(seconds_ago=2 * 3600, now=now),
            }
        ],
    )
    resolver = AutoResolver(store=store, clock=lambda: now)

    results = await resolver.reconcile_half_recorded()

    assert len(results) == 1
    assert results[0].outcome == ReconcileOutcome.APPLIED
    assert results[0].unwind_pnl == pytest.approx(-4.7)
    assert store.mark_calls == [
        {
            "arb_id": "ARB-000999",
            "unwind_pnl": -4.7,
            "note": (
                "AutoResolver (2026-05-22): unwind_pnl=-4.7 sourced from "
                "auto_unwind incident metadata."
            ),
        }
    ]


@pytest.mark.asyncio
async def test_reconcile_skips_when_no_auto_unwind_evidence():
    now = 1_700_000_000.0
    pool = _StubPool()  # no auto-unwind incident registered
    store = _StubStore(
        pool,
        half_recorded=[
            {
                "arb_id": "ARB-001000",
                "canonical_id": "X",
                "status": "failed",
                "created_at": _ts(seconds_ago=2 * 3600, now=now),
            }
        ],
    )
    resolver = AutoResolver(store=store, clock=lambda: now)

    results = await resolver.reconcile_half_recorded()

    assert results[0].outcome == ReconcileOutcome.SKIPPED_NO_EVIDENCE
    assert store.mark_calls == []


@pytest.mark.asyncio
async def test_reconcile_skips_when_arb_has_open_legs():
    """Even with auto-unwind evidence, an open leg blocks reconciliation."""
    now = 1_700_000_000.0
    pool = _StubPool(
        auto_unwind_by_arb={
            "ARB-001001": {
                "incident_id": "INC-2",
                "metadata": {"realized_pnl": 0.0},
            }
        },
        open_legs_by_arb={"ARB-001001": 1},  # one leg still pending/submitted
    )
    store = _StubStore(
        pool,
        half_recorded=[
            {
                "arb_id": "ARB-001001",
                "canonical_id": "X",
                "status": "recovering",
                "created_at": _ts(seconds_ago=2 * 3600, now=now),
            }
        ],
    )
    resolver = AutoResolver(store=store, clock=lambda: now)

    results = await resolver.reconcile_half_recorded()

    assert results[0].outcome == ReconcileOutcome.SKIPPED_OPEN_POSITION
    assert store.mark_calls == []


@pytest.mark.asyncio
async def test_reconcile_skips_when_arb_too_young():
    now = 1_700_000_000.0
    pool = _StubPool(
        auto_unwind_by_arb={
            "ARB-001002": {
                "incident_id": "INC-3",
                "metadata": {"realized_pnl": 0.0},
            }
        },
    )
    store = _StubStore(
        pool,
        half_recorded=[
            {
                "arb_id": "ARB-001002",
                "canonical_id": "X",
                "status": "failed",
                "created_at": _ts(seconds_ago=120, now=now),  # 2 min old
            }
        ],
    )
    resolver = AutoResolver(store=store, clock=lambda: now)

    results = await resolver.reconcile_half_recorded()

    assert results[0].outcome == ReconcileOutcome.SKIPPED_TOO_YOUNG
    assert store.mark_calls == []


@pytest.mark.asyncio
async def test_reconcile_sentinel_is_unchanged_constant():
    """Regression guard: tests and prod must share the sentinel string."""
    assert NAKED_LEG_RECONCILED_SENTINEL == "[naked-leg-reconciled]"


# ─── Component B: kill-switch classifier ───────────────────────────────────


def test_classify_arm_reason_treats_loss_keywords_as_genuine():
    assert (
        _classify_arm_reason("operator:risk", "loss threshold breached")
        == KillSwitchVerdict.GENUINE
    )
    assert (
        _classify_arm_reason("engine", "PnL reconciliation drift")
        == KillSwitchVerdict.GENUINE
    )
    assert (
        _classify_arm_reason("operator:ops", "manual arm")
        == KillSwitchVerdict.GENUINE
    )


def test_classify_arm_reason_treats_shutdown_keywords_as_benign():
    assert (
        _classify_arm_reason("system:shutdown", "Process shutdown signal")
        == KillSwitchVerdict.BENIGN
    )
    assert (
        _classify_arm_reason("system:shutdown", "SIGTERM drain")
        == KillSwitchVerdict.BENIGN
    )


def test_classify_arm_reason_genuine_wins_when_ambiguous():
    # "shutdown" is benign but "loss" is genuine — the safer
    # classification wins so a mixed reason keeps the switch armed.
    assert (
        _classify_arm_reason(
            "system:shutdown", "loss threshold + shutdown drain"
        )
        == KillSwitchVerdict.GENUINE
    )


def test_classify_arm_reason_unknown_when_no_marker():
    assert _classify_arm_reason("system:foo", "something else") == (
        KillSwitchVerdict.UNKNOWN
    )


@pytest.mark.asyncio
async def test_classify_kill_switch_not_armed_returns_not_armed():
    pool = _StubPool()
    store = _StubStore(pool)
    supervisor = _make_supervisor(armed=False)
    resolver = AutoResolver(store=store, supervisor=supervisor)
    classification = await resolver.classify_kill_switch()
    assert classification.verdict == KillSwitchVerdict.NOT_ARMED
    assert classification.recommend_manual_disarm is False


@pytest.mark.asyncio
async def test_classify_kill_switch_benign_recommends_when_preconditions_pass():
    pool = _StubPool()
    store = _StubStore(pool)
    safety = _StubSafetyStore()
    supervisor = _make_supervisor(
        armed=True,
        armed_by="system:shutdown",
        armed_reason="Process shutdown signal",
    )
    notifier = AsyncMock()
    resolver = AutoResolver(
        store=store, supervisor=supervisor, safety_store=safety, notifier=notifier
    )
    classification = await resolver.classify_kill_switch(
        credentials_ok=True, price_feeds_flowing=True, active_loss_event=False
    )
    assert classification.verdict == KillSwitchVerdict.BENIGN
    assert classification.recommend_manual_disarm is True
    assert classification.blocking_reasons == ()
    # Telegram alert was sent, safety_events row written.
    notifier.send.assert_awaited_once()
    assert any(e["event_type"] == "recommend_disarm" for e in safety.events)


@pytest.mark.asyncio
async def test_classify_kill_switch_genuine_never_recommends():
    pool = _StubPool()
    store = _StubStore(pool)
    safety = _StubSafetyStore()
    supervisor = _make_supervisor(
        armed=True,
        armed_by="operator:risk",
        armed_reason="loss threshold breached",
    )
    notifier = AsyncMock()
    resolver = AutoResolver(
        store=store, supervisor=supervisor, safety_store=safety, notifier=notifier
    )
    classification = await resolver.classify_kill_switch(
        credentials_ok=True, price_feeds_flowing=True, active_loss_event=False
    )
    assert classification.verdict == KillSwitchVerdict.GENUINE
    assert classification.recommend_manual_disarm is False
    # No disarm proposal sent or audited.
    notifier.send.assert_not_called()
    assert safety.events == []


@pytest.mark.asyncio
async def test_classify_kill_switch_benign_blocked_by_active_loss_event():
    pool = _StubPool()
    store = _StubStore(pool)
    safety = _StubSafetyStore()
    supervisor = _make_supervisor(
        armed=True,
        armed_by="system:shutdown",
        armed_reason="drain",
    )
    notifier = AsyncMock()
    resolver = AutoResolver(
        store=store, supervisor=supervisor, safety_store=safety, notifier=notifier
    )
    classification = await resolver.classify_kill_switch(
        credentials_ok=True, price_feeds_flowing=True, active_loss_event=True
    )
    assert classification.verdict == KillSwitchVerdict.BENIGN
    assert classification.recommend_manual_disarm is False
    assert classification.blocking_reasons
    notifier.send.assert_not_called()


@pytest.mark.asyncio
async def test_classify_kill_switch_does_not_call_reset_kill():
    """The classifier must never invoke supervisor.reset_kill itself."""
    pool = _StubPool()
    store = _StubStore(pool)
    supervisor = _make_supervisor(
        armed=True,
        armed_by="system:shutdown",
        armed_reason="Process shutdown signal",
    )
    # Track whether reset_kill is touched. We set it to an AsyncMock so
    # the resolver would visibly fail this test if it ever called it.
    supervisor.reset_kill = AsyncMock()
    resolver = AutoResolver(store=store, supervisor=supervisor)
    await resolver.classify_kill_switch()
    supervisor.reset_kill.assert_not_called()


# ─── Component C: warm-restart eligibility ─────────────────────────────────


@pytest.mark.asyncio
async def test_warm_restart_off_by_default():
    pool = _StubPool(historical_arb_count=100)
    store = _StubStore(pool)
    cfg = AutoResolverConfig(warm_restart_enabled=False)
    resolver = AutoResolver(store=store, config=cfg)
    decision = await resolver.warm_restart_decision()
    assert decision.enabled is False
    assert decision.scans_required == cfg.cold_start_scans
    assert decision.opps_required == cfg.cold_start_opps


@pytest.mark.asyncio
async def test_warm_restart_engages_with_historical_trades():
    pool = _StubPool(historical_arb_count=42)
    store = _StubStore(pool)
    cfg = AutoResolverConfig(warm_restart_enabled=True)
    resolver = AutoResolver(store=store, config=cfg)
    decision = await resolver.warm_restart_decision()
    assert decision.enabled is True
    assert decision.historical_arb_count == 42
    assert decision.scans_required == cfg.warm_restart_reduced_scans
    assert decision.opps_required == cfg.warm_restart_reduced_opps


@pytest.mark.asyncio
async def test_warm_restart_cold_start_with_no_history():
    pool = _StubPool(historical_arb_count=0)
    store = _StubStore(pool)
    cfg = AutoResolverConfig(warm_restart_enabled=True)
    resolver = AutoResolver(store=store, config=cfg)
    decision = await resolver.warm_restart_decision()
    assert decision.enabled is False
    assert decision.scans_required == cfg.cold_start_scans
    assert "0 historical successful arb(s)" in decision.reason


# ─── Component D: stale-incident expirer ───────────────────────────────────


@pytest.mark.asyncio
async def test_expire_stale_warning_incident_after_24h():
    pool = _StubPool(
        open_incidents=[
            {
                "incident_id": "INC-OLD",
                "arb_id": None,
                "severity": "warning",
                "age_s": 25 * 3600.0,
            }
        ],
    )
    store = _StubStore(pool)
    resolver = AutoResolver(store=store)
    results = await resolver.expire_stale_incidents()
    assert any(r.outcome == "expired" for r in results)
    assert pool.expired_incidents and pool.expired_incidents[0]["incident_id"] == (
        "INC-OLD"
    )


@pytest.mark.asyncio
async def test_expire_skips_critical_under_48h():
    pool = _StubPool(
        open_incidents=[
            {
                "incident_id": "INC-CRIT-30H",
                "arb_id": None,
                "severity": "critical",
                "age_s": 30 * 3600.0,
            }
        ],
    )
    store = _StubStore(pool)
    resolver = AutoResolver(store=store)
    results = await resolver.expire_stale_incidents()
    # Critical incidents have a 48h threshold; 30h is too young.
    assert results[0].outcome == "skipped_too_young"
    assert pool.expired_incidents == []


@pytest.mark.asyncio
async def test_expire_skips_incidents_with_open_arb_legs():
    """The most important safety contract: open legs block expiry."""
    pool = _StubPool(
        open_incidents=[
            {
                "incident_id": "INC-RISKY",
                "arb_id": "ARB-OPEN",
                "severity": "warning",
                "age_s": 7 * 24 * 3600.0,  # very stale
            }
        ],
        open_legs_by_arb={"ARB-OPEN": 1},  # one non-terminal leg
    )
    store = _StubStore(pool)
    resolver = AutoResolver(store=store)
    results = await resolver.expire_stale_incidents()
    assert results[0].outcome == "skipped_active_position"
    assert pool.expired_incidents == []  # nothing actually expired


@pytest.mark.asyncio
async def test_expire_critical_after_48h_when_no_open_legs():
    pool = _StubPool(
        open_incidents=[
            {
                "incident_id": "INC-CRIT-OLD",
                "arb_id": "ARB-CLOSED",
                "severity": "critical",
                "age_s": 50 * 3600.0,
            }
        ],
        open_legs_by_arb={},  # all legs terminal
    )
    store = _StubStore(pool)
    resolver = AutoResolver(store=store)
    results = await resolver.expire_stale_incidents()
    assert results[0].outcome == "expired"
    assert pool.expired_incidents[0]["incident_id"] == "INC-CRIT-OLD"


# ─── run_once orchestrator ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_once_returns_aggregated_summary():
    pool = _StubPool(
        auto_unwind_by_arb={
            "ARB-WIN": {
                "incident_id": "INC-AU",
                "metadata": {"realized_pnl": -1.0},
            }
        },
        open_incidents=[
            {
                "incident_id": "INC-OLD",
                "arb_id": None,
                "severity": "warning",
                "age_s": 25 * 3600.0,
            }
        ],
    )
    now = 1_700_000_000.0
    store = _StubStore(
        pool,
        half_recorded=[
            {
                "arb_id": "ARB-WIN",
                "canonical_id": "X",
                "status": "failed",
                "created_at": _ts(seconds_ago=2 * 3600, now=now),
            }
        ],
    )
    supervisor = _make_supervisor(armed=False)
    resolver = AutoResolver(
        store=store, supervisor=supervisor, clock=lambda: now
    )
    summary = await resolver.run_once()
    assert summary["reconciled"][0]["outcome"] == ReconcileOutcome.APPLIED
    assert summary["kill_switch"]["verdict"] == KillSwitchVerdict.NOT_ARMED
    assert any(
        e["outcome"] == "expired" for e in summary["expired_incidents"]
    )
