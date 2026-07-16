"""Regression tests for the 2026-07-16 deep-fix audit pass.

Each test pins one specific fix so a future refactor that silently reverts the
behavior fails loudly. Covers the pure-function fixes here; the stateful ones
(STRAND-* TTL skip, deposit-drift incident, burst-guard bypass) live in their
own module test files.

  - Fix #6: coherence.shared_market_owner_conflict — duplicate-canonical guard
  - Fix #7: polymarket_us._signature_path — query string stripped before signing
"""
from arbiter.mapping import coherence
from arbiter.collectors.polymarket_us import _signature_path


# ─── Fix #6: duplicate-canonical (shared market owner) guard ────────────────

def test_shared_market_owner_conflict_none_when_unowned():
    # No other confirmed mapping owns this market → no conflict.
    assert coherence.shared_market_owner_conflict(
        "CONTROLH-2026-R", "some-slug", {}
    ) is None


def test_shared_market_owner_conflict_flags_shared_kalshi_market():
    # Another confirmed mapping already owns the Kalshi market (the exact
    # 2026-07-15 CONTROLH-2026-R duplicate-canonical class).
    conflict = coherence.shared_market_owner_conflict(
        "CONTROLH-2026-R", "", {"kalshi:CONTROLH-2026-R": 1}
    )
    assert conflict is not None
    assert "CONTROLH-2026-R" in conflict
    assert "Kalshi market" in conflict


def test_shared_market_owner_conflict_flags_shared_polymarket_slug():
    conflict = coherence.shared_market_owner_conflict(
        "", "dem-house-2026", {"polymarket:dem-house-2026": 2}
    )
    assert conflict is not None
    assert "dem-house-2026" in conflict
    assert "Polymarket slug" in conflict


def test_shared_market_owner_conflict_ignores_zero_count():
    # A key present with count 0 is not an owner.
    assert coherence.shared_market_owner_conflict(
        "KX-A", "slug-a", {"kalshi:KX-A": 0, "polymarket:slug-a": 0}
    ) is None


def test_shared_market_owner_conflict_ignores_empty_identifiers():
    # Empty legs (e.g. a K↔FX mapping with no Polymarket slug) never conflict,
    # even if the counts dict happens to carry an empty-string key.
    assert coherence.shared_market_owner_conflict(
        "", "", {"kalshi:": 5, "polymarket:": 9}
    ) is None


def test_shared_market_owner_conflict_kalshi_takes_precedence_but_both_checked():
    # When both legs are owned, a conflict is still returned (kalshi first).
    conflict = coherence.shared_market_owner_conflict(
        "KX-B", "slug-b",
        {"kalshi:KX-B": 1, "polymarket:slug-b": 1},
    )
    assert conflict is not None


# ─── Fix #7: Polymarket-US signed-GET query-string stripping ────────────────

def test_signature_path_strips_query_string():
    # The signature must cover the path WITHOUT the query string — a signed GET
    # 401s when the querystring is included in the signature payload.
    assert _signature_path(
        "/v1/portfolio/activities?limit=50&cursor=abc123"
    ) == "/v1/portfolio/activities"


def test_signature_path_adds_v1_prefix_when_missing():
    assert _signature_path("/portfolio/positions") == "/v1/portfolio/positions"


def test_signature_path_no_double_v1_prefix():
    assert _signature_path("/v1/portfolio/positions") == "/v1/portfolio/positions"


def test_signature_path_strips_query_and_normalizes_prefix_together():
    assert _signature_path(
        "/portfolio/activities?limit=5"
    ) == "/v1/portfolio/activities"


def test_signature_path_no_query_is_unchanged():
    assert _signature_path("/v1/portfolio/positions") == "/v1/portfolio/positions"
