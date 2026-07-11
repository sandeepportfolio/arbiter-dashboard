"""Cross-venue mapping coherence checks (2026-07-10).

Live failure these guard: DEM_SENATE_2026 / GOP_SENATE_2026 had their
ForecastEx conids PARTY-SWAPPED (DEM mapping held SENM_1126_Republican_YES/NO;
GOP held SENM_1126_Democratic_YES/NO). Every "arb" bought two legs of the SAME
directional bet; the phantom 12.5c edge persisted for days because it wasn't
real. Two independent detectors:

1. Cross-venue YES divergence: two venues quoting the SAME outcome must agree
   within a tolerance. Swapped-party DEM: kalshi yes 0.44 vs FX "yes" 0.61
   (divergence 0.17). GOP: kalshi yes ~0.56 vs FX "yes" 0.47 (0.09).
2. Party-token conflict: a canonical named DEM_* must not map to an FX
   contract whose local_symbol names the other party.
"""
from __future__ import annotations

import pytest

from arbiter.mapping.coherence import (
    DEFAULT_MAX_YES_DIVERGENCE,
    max_yes_divergence,
    party_conflict,
    shared_fx_conid_conflict,
)


# ── Shared ForecastEx conid detection (2026-07-11 latent party-swap) ───────
#
# Live failure this guards: two confirmed+auto_trade canonicals
# (POL_US_SENATE ...DEM and ...REP) both pointed at the SAME ForecastEx conid
# 745923952 — at most one can be the correct hedge leg; the other is
# party-swapped. It escaped the live-quote coherence check because the FX leg
# was deduped-dark, so this DB-metadata check must catch it independent of quotes.


def test_shared_yes_conid_is_a_conflict():
    counts = {"745923952": 2, "111": 1}
    c = shared_fx_conid_conflict("745923952", "", counts)
    assert c is not None
    assert "745923952" in c and "2" in c


def test_shared_no_conid_is_a_conflict():
    counts = {"999": 3}
    assert shared_fx_conid_conflict("111", "999", counts) is not None


def test_unique_conids_are_clean():
    counts = {"111": 1, "222": 1}
    assert shared_fx_conid_conflict("111", "222", counts) is None


def test_empty_or_zero_conid_is_not_a_conflict():
    counts = {"0": 98, "": 30}
    assert shared_fx_conid_conflict("", "", counts) is None
    assert shared_fx_conid_conflict("0", "0", counts) is None


def test_missing_from_counts_is_clean():
    assert shared_fx_conid_conflict("abc", "", {}) is None


# ── Cross-venue YES divergence ─────────────────────────────────────────────


def test_swapped_dem_senate_mapping_is_incoherent():
    div, pair = max_yes_divergence({"kalshi": 0.44, "forecastex": 0.61})
    assert div == pytest.approx(0.17)
    assert set(pair) == {"kalshi", "forecastex"}
    assert div > DEFAULT_MAX_YES_DIVERGENCE


def test_swapped_gop_senate_mapping_is_incoherent():
    div, _ = max_yes_divergence({"kalshi": 0.56, "forecastex": 0.47})
    assert div == pytest.approx(0.09)
    assert div > DEFAULT_MAX_YES_DIVERGENCE


def test_correct_mapping_with_real_arb_edge_is_coherent():
    """A genuine cross-venue arb divergence of a few cents must NOT flag."""
    div, _ = max_yes_divergence({"kalshi": 0.44, "forecastex": 0.47})
    assert div == pytest.approx(0.03)
    assert div <= DEFAULT_MAX_YES_DIVERGENCE


def test_divergence_ignores_dead_quotes():
    """Venues without a live yes price (0.0) cannot vote."""
    div, pair = max_yes_divergence({"kalshi": 0.44, "forecastex": 0.0})
    assert div == 0.0
    assert pair is None


def test_divergence_across_three_venues_takes_max():
    div, pair = max_yes_divergence(
        {"kalshi": 0.44, "polymarket": 0.46, "forecastex": 0.61}
    )
    assert div == pytest.approx(0.17)
    assert set(pair) == {"kalshi", "forecastex"}


def test_single_venue_is_trivially_coherent():
    div, pair = max_yes_divergence({"kalshi": 0.44})
    assert div == 0.0
    assert pair is None


# ── Party-token conflict ───────────────────────────────────────────────────


def test_dem_canonical_mapped_to_republican_contract_conflicts():
    conflict = party_conflict(
        "DEM_SENATE_2026 democrats senate 2026", "SENM_1126_Republican_YES"
    )
    assert conflict is not None
    assert "democratic" in conflict.lower() and "republican" in conflict.lower()


def test_gop_canonical_mapped_to_democratic_contract_conflicts():
    conflict = party_conflict(
        "GOP_SENATE_2026 republicans senate", "SENM_1126_Democratic_YES"
    )
    assert conflict is not None


def test_matching_party_is_clean():
    assert party_conflict(
        "DEM_SENATE_2026 democrats senate", "SENM_1126_Democratic_YES"
    ) is None
    assert party_conflict(
        "GOP_SENATE_2026 gop senate", "SENM_1126_Republican_NO"
    ) is None


def test_non_party_markets_never_conflict():
    assert party_conflict("FX_CPIY_202611_0p2 cpi yoy", "CPIY_NOV26_0.2_YES") is None
    assert party_conflict("GAME_MLB_20260710_ATL", "") is None


def test_missing_symbol_with_party_canonical_returns_none():
    """Symbol unavailable -> no verdict here; the PROMOTION gate is what
    fails closed on missing data (it refuses to promote without a symbol
    when the canonical carries party tokens)."""
    assert party_conflict("DEM_SENATE_2026", "") is None
