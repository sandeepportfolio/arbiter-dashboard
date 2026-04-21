"""Unit tests for MARKET_MAP audit log (Phase 6 Plan 06-05).

Tests verify:
    - toggle allow_auto_trade → audit entry written with old/new values + actor
    - note flows through into audit entry
    - unchanged field does NOT write a spurious entry
    - audit log is capped at 50 entries (FIFO)
    - missing actor defaults to "system"
"""
from __future__ import annotations

import time

import pytest

from arbiter.config.settings import MARKET_MAP, update_market_mapping


@pytest.fixture
def test_canonical():
    # Pick a known-existing mapping and snapshot its state so we can restore.
    canonical_id = next(iter(MARKET_MAP.keys()))
    original = dict(MARKET_MAP[canonical_id])
    yield canonical_id
    # Restore (with audit_log reset)
    MARKET_MAP[canonical_id] = {**original, "audit_log": []}


def test_toggle_allow_auto_trade_writes_audit_entry(test_canonical):
    MARKET_MAP[test_canonical]["allow_auto_trade"] = False
    MARKET_MAP[test_canonical]["audit_log"] = []
    before = time.time()

    mapping = update_market_mapping(
        test_canonical,
        allow_auto_trade=True,
        note="Enable for live test",
        actor="operator@example.com",
    )

    audit = mapping["audit_log"]
    assert len(audit) == 1
    entry = audit[0]
    assert entry["field"] == "allow_auto_trade"
    assert entry["old"] is False
    assert entry["new"] is True
    assert entry["actor"] == "operator@example.com"
    assert entry["note"] == "Enable for live test"
    assert entry["ts"] >= before


def test_unchanged_field_does_not_write_audit_entry(test_canonical):
    MARKET_MAP[test_canonical]["allow_auto_trade"] = True
    MARKET_MAP[test_canonical]["audit_log"] = []

    mapping = update_market_mapping(
        test_canonical,
        allow_auto_trade=True,  # same value
        actor="operator@example.com",
    )
    assert mapping["audit_log"] == []


def test_multiple_changes_write_multiple_entries(test_canonical):
    MARKET_MAP[test_canonical]["allow_auto_trade"] = False
    MARKET_MAP[test_canonical]["status"] = "review"
    MARKET_MAP[test_canonical]["audit_log"] = []

    mapping = update_market_mapping(
        test_canonical,
        status="confirmed",
        allow_auto_trade=True,
        actor="ops@example.com",
    )
    audit = mapping["audit_log"]
    fields = sorted(e["field"] for e in audit)
    assert fields == ["allow_auto_trade", "status"]


def test_missing_actor_defaults_to_system(test_canonical):
    MARKET_MAP[test_canonical]["allow_auto_trade"] = False
    MARKET_MAP[test_canonical]["audit_log"] = []

    mapping = update_market_mapping(
        test_canonical,
        allow_auto_trade=True,
    )
    assert mapping["audit_log"][0]["actor"] == "system"


def test_audit_log_capped_at_50_entries(test_canonical):
    # Seed 60 entries and make 1 more change; should see 50 post-cap.
    MARKET_MAP[test_canonical]["audit_log"] = [
        {"ts": float(i), "actor": "seed", "field": "status", "old": "a", "new": "b", "note": None}
        for i in range(60)
    ]
    MARKET_MAP[test_canonical]["allow_auto_trade"] = False

    mapping = update_market_mapping(
        test_canonical,
        allow_auto_trade=True,
        actor="operator",
    )
    assert len(mapping["audit_log"]) == 50
    # Most recent entry is the one we just made
    assert mapping["audit_log"][-1]["field"] == "allow_auto_trade"
    assert mapping["audit_log"][-1]["actor"] == "operator"


def test_unknown_canonical_returns_none():
    assert update_market_mapping("DOES_NOT_EXIST", allow_auto_trade=True) is None
