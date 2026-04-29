"""Tests for the centralized team-alias normalization."""
from __future__ import annotations

from arbiter.mapping.team_aliases import (
    KNOWN_NON_MATCHES,
    detect_polarity,
    norm_team,
    same_team,
    try_split_teams,
)


# ─── norm_team ─────────────────────────────────────────────────────────────────

def test_norm_team_passes_through_unknown():
    assert norm_team("ZZZ") == "zzz"
    assert norm_team("") == ""


def test_norm_team_canonicalizes_aliases():
    # Barcelona has both BAR and FCB on different platforms
    assert norm_team("BAR") == "fcb"
    assert norm_team("FCB") == "fcb"
    # Austin FC: ATX (Kalshi) vs AUS (Polymarket)
    assert norm_team("ATX") == "aus"
    assert norm_team("AUS") == "aus"


# ─── same_team ─────────────────────────────────────────────────────────────────

def test_same_team_handles_aliases():
    assert same_team("BAR", "FCB")
    assert same_team("ATX", "AUS")
    # Common 3-letter abbreviations agree
    assert same_team("HOU", "hou")


def test_same_team_blocks_known_traps():
    # Montreal Canadiens vs Inter Miami CF — DIFFERENT teams
    assert not same_team("MTL", "MIM")
    assert not same_team("mim", "mtl")


def test_same_team_rejects_empty():
    assert not same_team("", "HOU")
    assert not same_team("BAL", "")


# ─── try_split_teams ───────────────────────────────────────────────────────────

def test_split_resolves_simple_pair():
    assert try_split_teams("HOUBAL", "hou", "bal")
    assert try_split_teams("HOUBAL", "bal", "hou")


def test_split_works_through_aliases():
    # Kalshi sticks ATX in the ticker stem, Polymarket uses AUS in the slug
    assert try_split_teams("ATXSTL", "aus", "stl")


def test_split_rejects_unrelated():
    assert not try_split_teams("HOUBAL", "lal", "lac")


# ─── detect_polarity ───────────────────────────────────────────────────────────

def test_polarity_same_for_explicit_match():
    # Kalshi YES = HOU; polymarket explicit suffix = HOU
    out = detect_polarity(
        kalshi_side="HOU",
        poly_side_suffix="hou",
        poly_team1="hou",
        poly_team2="bal",
    )
    assert out == "same"


def test_polarity_flipped_for_binary_team2():
    # No explicit suffix → poly YES = team1; if Kalshi side = team2 it's flipped
    out = detect_polarity(
        kalshi_side="BAL",
        poly_side_suffix=None,
        poly_team1="hou",
        poly_team2="bal",
    )
    assert out == "flipped"


def test_polarity_same_for_binary_team1():
    out = detect_polarity(
        kalshi_side="HOU",
        poly_side_suffix=None,
        poly_team1="hou",
        poly_team2="bal",
    )
    assert out == "same"


def test_polarity_flipped_for_explicit_opposite():
    # 3-way slug with explicit "bal" side; Kalshi picked HOU instead → flipped
    out = detect_polarity(
        kalshi_side="HOU",
        poly_side_suffix="bal",
        poly_team1="hou",
        poly_team2="bal",
    )
    assert out == "flipped"


def test_polarity_unrelated_for_tie_vs_team():
    out = detect_polarity(
        kalshi_side="tie",
        poly_side_suffix="hou",
        poly_team1="hou",
        poly_team2="bal",
    )
    assert out == "unrelated"


def test_polarity_same_for_both_tie():
    out = detect_polarity(
        kalshi_side="draw",
        poly_side_suffix="tie",
        poly_team1="hou",
        poly_team2="bal",
    )
    assert out == "same"


def test_polarity_unrelated_when_third_team():
    # KOR vs USA fixture; Kalshi side = JPN (not in fixture) → unrelated
    out = detect_polarity(
        kalshi_side="JPN",
        poly_side_suffix=None,
        poly_team1="kor",
        poly_team2="usa",
    )
    assert out == "unrelated"


def test_known_non_matches_includes_mtl_mim():
    assert ("mtl", "mim") in KNOWN_NON_MATCHES
    assert ("mim", "mtl") in KNOWN_NON_MATCHES
