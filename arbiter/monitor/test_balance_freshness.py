"""Tests for the BalanceMonitor freshness + error tracking.

Bug being defended: the dashboard previously displayed the cached 30-second
background balance with no age and no failure surface — operators couldn't tell
when a balance was stale or why. Past incident: Polymarket /account/balances
errored for 15 minutes and the displayed number was unchanged the whole time;
operators traded against a phantom balance until reconciliation flagged it.
"""
from __future__ import annotations

import asyncio
import time

import pytest

from arbiter.config.settings import AlertConfig
from arbiter.monitor.balance import BalanceMonitor


class _StaticCollector:
    """Collector that returns a fixed balance and reports its source."""

    def __init__(self, balance, source="test-source"):
        self._balance = balance
        self.balance_source = source
        self.calls = 0

    async def fetch_balance(self):
        self.calls += 1
        return self._balance


class _RaisingCollector:
    balance_source = "raises"

    def __init__(self, message="boom"):
        self._message = message

    async def fetch_balance(self):
        raise RuntimeError(self._message)


class _NoneCollector:
    balance_source = "none"

    async def fetch_balance(self):
        return None


def _make_monitor(collectors):
    cfg = AlertConfig()
    cfg.cooldown = 0.0
    monitor = BalanceMonitor(cfg, collectors)
    return monitor


def test_snapshot_carries_source_from_collector():
    async def runner():
        monitor = _make_monitor({"kalshi": _StaticCollector(450.0, source="kalshi:portfolio")})
        await monitor.check_balances()
        snap = monitor.current_balances["kalshi"]
        return snap

    snap = asyncio.run(runner())
    assert snap.balance == 450.0
    assert snap.source == "kalshi:portfolio"


def test_last_errors_records_exception_message():
    async def runner():
        monitor = _make_monitor({"polymarket": _RaisingCollector("clob-503")})
        await monitor.check_balances()
        return monitor.last_errors

    errors = asyncio.run(runner())
    assert "polymarket" in errors
    assert "clob-503" in errors["polymarket"]["message"]
    assert errors["polymarket"]["timestamp"] > 0


def test_last_errors_records_when_fetch_returns_none():
    async def runner():
        monitor = _make_monitor({"polymarket": _NoneCollector()})
        await monitor.check_balances()
        return monitor.last_errors

    errors = asyncio.run(runner())
    assert "polymarket" in errors
    assert "unavailable" in errors["polymarket"]["message"]


def test_successful_fetch_clears_prior_error():
    """After an error, a subsequent successful fetch should clear the error
    so the dashboard doesn't keep flagging a recovered platform as broken.
    """
    async def runner():
        collector = _StaticCollector(100.0)
        monitor = _make_monitor({"kalshi": collector})
        # Seed an error manually as if a previous tick failed
        monitor._last_errors["kalshi"] = {"message": "earlier failure", "timestamp": 1.0}
        await monitor.check_balances()
        return monitor.last_errors

    errors = asyncio.run(runner())
    assert "kalshi" not in errors


class _ToggleCollector:
    """Collector that returns a good balance, then fails on demand.

    Models the ForecastEx failure mode: a healthy fetch primes a known-good
    value, then the IBKR gateway SSO expires and subsequent fetches raise
    (circuit open) — the monitor must degrade to the last-known-good value,
    not collapse the platform to null.
    """

    balance_source = "forecastex:ibkr-gateway"

    def __init__(self, balance):
        self._balance = balance
        self.mode = "ok"  # "ok" | "raise" | "none"

    async def fetch_balance(self):
        if self.mode == "raise":
            raise RuntimeError("Circuit [forecastex-rest] is OPEN — request rejected")
        if self.mode == "none":
            return None
        return self._balance


def test_failed_fetch_degrades_to_last_known_good_on_exception():
    """After one good read, a raising fetch must re-serve the last-known-good
    balance flagged stale, with the error reason and the ORIGINAL timestamp —
    never a bare null that drops the platform from current_balances.
    """
    async def runner():
        coll = _ToggleCollector(298.95)
        monitor = _make_monitor({"forecastex": coll})
        await monitor.check_balances()
        good = monitor.current_balances["forecastex"]
        good_ts = good.timestamp
        # Gateway dies — circuit opens.
        coll.mode = "raise"
        await monitor.check_balances()
        return monitor.current_balances, monitor.last_errors, good_ts

    balances, errors, good_ts = asyncio.run(runner())
    assert "forecastex" in balances, "platform must NOT vanish to null"
    snap = balances["forecastex"]
    assert snap.balance == 298.95
    assert snap.stale is True
    assert snap.timestamp == good_ts, "stale value keeps original known-good ts"
    assert "OPEN" in snap.error
    assert snap.source == "forecastex:ibkr-gateway"
    # The error map still carries the live failure reason.
    assert "forecastex" in errors
    assert "OPEN" in errors["forecastex"]["message"]


def test_failed_fetch_degrades_to_last_known_good_on_none():
    """A None return (no creds / swallowed error) also degrades to last-good."""
    async def runner():
        coll = _ToggleCollector(123.0)
        monitor = _make_monitor({"forecastex": coll})
        await monitor.check_balances()
        coll.mode = "none"
        await monitor.check_balances()
        return monitor.current_balances

    balances = asyncio.run(runner())
    assert balances["forecastex"].balance == 123.0
    assert balances["forecastex"].stale is True
    assert "unavailable" in balances["forecastex"].error


def test_no_known_good_means_platform_absent_not_phantom():
    """If a platform NEVER produced a good read this process, a failure must
    leave it absent from current_balances (the error map carries the reason) —
    we must not fabricate a $0 phantom balance out of nothing.
    """
    async def runner():
        monitor = _make_monitor({"forecastex": _RaisingCollector("never-good")})
        await monitor.check_balances()
        return monitor.current_balances, monitor.last_errors

    balances, errors = asyncio.run(runner())
    assert "forecastex" not in balances
    assert "forecastex" in errors
    assert "never-good" in errors["forecastex"]["message"]


def test_recovery_clears_stale_flag():
    """When the gateway comes back, the next good read must overwrite the stale
    last-known-good snapshot with a fresh, non-stale one and clear the error.
    """
    async def runner():
        coll = _ToggleCollector(298.95)
        monitor = _make_monitor({"forecastex": coll})
        await monitor.check_balances()          # good
        coll.mode = "raise"
        await monitor.check_balances()          # degrade → stale
        assert monitor.current_balances["forecastex"].stale is True
        coll.mode = "ok"
        coll._balance = 305.10                  # gateway back, new number
        await monitor.check_balances()          # recover
        return monitor.current_balances, monitor.last_errors

    balances, errors = asyncio.run(runner())
    snap = balances["forecastex"]
    assert snap.balance == 305.10
    assert snap.stale is False
    assert snap.error == ""
    assert "forecastex" not in errors


def test_refresh_balances_serialises_concurrent_callers():
    """force_refresh from multiple concurrent /api/balances requests should
    coalesce — the lock holds while one in-flight refresh completes, and the
    subsequent callers re-fetch fresh state.

    We can't assert single-fetch semantics without a sleep, but we can assert
    that all callers observe the latest balance value rather than racing on
    half-mutated state.
    """
    async def runner():
        collector = _StaticCollector(200.0)
        monitor = _make_monitor({"kalshi": collector})
        # Three concurrent refreshes — each acquires the lock in turn.
        await asyncio.gather(
            monitor.refresh_balances(),
            monitor.refresh_balances(),
            monitor.refresh_balances(),
        )
        return collector.calls, monitor.current_balances["kalshi"].balance

    calls, balance = asyncio.run(runner())
    # The lock serialises but doesn't deduplicate — three callers, three calls.
    # The important guarantee is no torn state, not a single fetch.
    assert calls == 3
    assert balance == 200.0
