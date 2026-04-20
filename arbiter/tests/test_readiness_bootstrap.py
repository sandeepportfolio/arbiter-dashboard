"""Unit tests for B-1 Q6 PHASE5_BOOTSTRAP_TRADES readiness override.

The bootstrap-mode override lets the FIRST N live trades bypass the
``_check_profitability`` ``collecting_evidence`` block. Without it, the
profitability gate stays blocking with zero completed executions -- a
chicken-and-egg condition where the very first live trade can never clear
readiness.

Contract:
- Unset env var = existing behaviour unchanged (Phase 4 + dev unaffected).
- Set to an int in ``[1, 5]`` AND ``completed_executions < N`` -> return
  ``ReadinessCheck(status="pass", blocking=False, summary="Phase 5
  bootstrap: <remaining> trade(s) remaining ...")``
- Once ``completed_executions >= N``, env var has no effect (fall through
  to existing branches).
- Out-of-range values (``0``, ``1000``) and unparseable values fall through
  to existing logic (treat as absent). Preflight check #8 is the second
  belt that catches invalid values before startup.
- Bootstrap short-circuits BEFORE ``validated_profitable`` and ``blocked``
  branches. This is intentional: bootstrap is the ONLY escape hatch; if
  the operator has set it they have accepted the override.

Tests follow the root-conftest async dispatch style for async methods
but ``_check_profitability`` is synchronous so test functions here are
plain ``def``.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from arbiter.readiness import OperationalReadiness, ReadinessCheck


def _make_snapshot(*, verdict: str, completed: int):
    """Return a MagicMock with the ProfitabilitySnapshot surface the check reads."""
    snap = MagicMock()
    snap.verdict = verdict
    snap.progress = 0.0
    snap.total_realized_pnl = 0.0
    snap.completed_executions = completed
    return snap


def _make_profitability(snapshot):
    pv = MagicMock()
    pv.get_snapshot = MagicMock(return_value=snapshot)
    return pv


def _make_readiness(profitability):
    """Minimal OperationalReadiness with only the profitability surface wired."""
    cfg = SimpleNamespace(
        scanner=SimpleNamespace(dry_run=False),
        kalshi=SimpleNamespace(api_key_id="", private_key_path=""),
        polymarket=SimpleNamespace(private_key=""),
        alerts=SimpleNamespace(telegram_bot_token="", telegram_chat_id=""),
    )
    return OperationalReadiness(
        config=cfg,
        engine=None,
        monitor=None,
        profitability=profitability,
        collectors={},
        reconciler=None,
    )


# ─── 1: Unset env var — existing behaviour unchanged ────────────────────────


def test_unset_env_preserves_collecting_evidence_warning(monkeypatch):
    monkeypatch.delenv("PHASE5_BOOTSTRAP_TRADES", raising=False)
    snap = _make_snapshot(verdict="collecting_evidence", completed=0)
    readiness = _make_readiness(_make_profitability(snap))

    check = readiness._check_profitability()

    assert isinstance(check, ReadinessCheck)
    assert check.status == "warning"
    assert check.blocking is True
    assert "collecting evidence" in check.summary.lower()


# ─── 2: PHASE5_BOOTSTRAP_TRADES=1, zero completed -> bypass ─────────────────


def test_bootstrap_set_to_1_with_zero_executions_passes_not_blocking(monkeypatch):
    monkeypatch.setenv("PHASE5_BOOTSTRAP_TRADES", "1")
    snap = _make_snapshot(verdict="collecting_evidence", completed=0)
    readiness = _make_readiness(_make_profitability(snap))

    check = readiness._check_profitability()

    assert check.status == "pass"
    assert check.blocking is False
    assert "1 trade(s) remaining" in check.summary
    assert check.details["bootstrap_limit"] == 1
    assert check.details["bootstrap_remaining"] == 1
    assert check.details["completed_executions"] == 0


# ─── 3: PHASE5_BOOTSTRAP_TRADES=1, one completed -> fall through ────────────


def test_bootstrap_exhausted_falls_through_to_existing_logic(monkeypatch):
    """After completing the bootstrap limit, the existing logic re-engages.

    With verdict=collecting_evidence + completed=1 and limit=1, bootstrap
    does NOT fire (remaining would be 0); fall through to the existing
    warning+blocking branch.
    """
    monkeypatch.setenv("PHASE5_BOOTSTRAP_TRADES", "1")
    snap = _make_snapshot(verdict="collecting_evidence", completed=1)
    readiness = _make_readiness(_make_profitability(snap))

    check = readiness._check_profitability()

    assert check.status == "warning"
    assert check.blocking is True


# ─── 4: PHASE5_BOOTSTRAP_TRADES=3, one completed -> still bootstrapping ─────


def test_bootstrap_set_to_3_with_one_completed_passes_not_blocking(monkeypatch):
    monkeypatch.setenv("PHASE5_BOOTSTRAP_TRADES", "3")
    snap = _make_snapshot(verdict="collecting_evidence", completed=1)
    readiness = _make_readiness(_make_profitability(snap))

    check = readiness._check_profitability()

    assert check.status == "pass"
    assert check.blocking is False
    assert "2 trade(s) remaining" in check.summary
    assert check.details["bootstrap_remaining"] == 2


# ─── 5: PHASE5_BOOTSTRAP_TRADES=0 (invalid, below range) -> fall through ────


def test_bootstrap_below_range_falls_through(monkeypatch):
    monkeypatch.setenv("PHASE5_BOOTSTRAP_TRADES", "0")
    snap = _make_snapshot(verdict="collecting_evidence", completed=0)
    readiness = _make_readiness(_make_profitability(snap))

    check = readiness._check_profitability()

    assert check.status == "warning"
    assert check.blocking is True


# ─── 6: PHASE5_BOOTSTRAP_TRADES=1000 (invalid, above range) -> fall through ─


def test_bootstrap_above_range_falls_through(monkeypatch):
    monkeypatch.setenv("PHASE5_BOOTSTRAP_TRADES", "1000")
    snap = _make_snapshot(verdict="collecting_evidence", completed=0)
    readiness = _make_readiness(_make_profitability(snap))

    check = readiness._check_profitability()

    assert check.status == "warning"
    assert check.blocking is True


# ─── 7: PHASE5_BOOTSTRAP_TRADES="not-a-number" -> fall through ──────────────


def test_bootstrap_unparseable_falls_through(monkeypatch):
    monkeypatch.setenv("PHASE5_BOOTSTRAP_TRADES", "banana")
    snap = _make_snapshot(verdict="collecting_evidence", completed=0)
    readiness = _make_readiness(_make_profitability(snap))

    check = readiness._check_profitability()

    assert check.status == "warning"
    assert check.blocking is True


# ─── 8: Bootstrap short-circuits BEFORE validated_profitable branch ─────────


def test_bootstrap_short_circuits_even_over_validated_profitable(monkeypatch):
    """With bootstrap active and completed < limit, the bootstrap summary
    wins over the validated_profitable branch.

    Both outcomes happen to be non-blocking passes, but the summary must be
    the bootstrap variant so operators see the bootstrap state in the
    dashboard.
    """
    monkeypatch.setenv("PHASE5_BOOTSTRAP_TRADES", "1")
    snap = _make_snapshot(verdict="validated_profitable", completed=0)
    readiness = _make_readiness(_make_profitability(snap))

    check = readiness._check_profitability()

    assert check.status == "pass"
    assert check.blocking is False
    # Bootstrap summary, not the validated_profitable summary.
    assert "bootstrap" in check.summary.lower()
    assert "validated" not in check.summary.lower()


# ─── 9: Bootstrap overrides blocked verdict (documented escape hatch) ───────


def test_bootstrap_wins_over_blocked_verdict(monkeypatch):
    """Bootstrap is the ONLY escape hatch; operator setting it = accepting the
    override. Documented in 05-RESEARCH.md Open Question #6.
    """
    monkeypatch.setenv("PHASE5_BOOTSTRAP_TRADES", "1")
    snap = _make_snapshot(verdict="blocked", completed=0)
    readiness = _make_readiness(_make_profitability(snap))

    check = readiness._check_profitability()

    assert check.status == "pass"
    assert check.blocking is False
    assert "bootstrap" in check.summary.lower()
