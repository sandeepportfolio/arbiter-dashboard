"""
Tests for resolution_check.py — Layer 1 structured-field equivalence gate.

TDD: tests written before implementation.
"""
from __future__ import annotations

import json
import os
import pathlib
import pytest

from arbiter.mapping.resolution_check import (
    MarketFacts,
    ResolutionMatch,
    check_resolution_equivalence,
)

FIXTURES_DIR = pathlib.Path(__file__).parent / "fixtures"


# ─── Fixture corpus tests ─────────────────────────────────────────────────────

def _load_pairs(filename: str):
    path = FIXTURES_DIR / filename
    with open(path) as f:
        return json.load(f)


def _facts_from_dict(d: dict) -> MarketFacts:
    return MarketFacts(
        question=d["question"],
        resolution_date=d.get("resolution_date"),
        resolution_source=d.get("resolution_source"),
        tie_break_rule=d.get("tie_break_rule"),
        category=d.get("category"),
        outcome_set=tuple(d.get("outcome_set", ["Yes", "No"])),
    )


@pytest.mark.parametrize("pair", _load_pairs("known_equivalent_pairs.json"))
def test_equivalent_pairs_return_identical(pair):
    a = _facts_from_dict(pair["a"])
    b = _facts_from_dict(pair["b"])
    result = check_resolution_equivalence(a, b)
    assert result == ResolutionMatch.IDENTICAL, (
        f"Expected IDENTICAL for pair '{pair.get('label', '')}', got {result}"
    )


@pytest.mark.parametrize("pair", _load_pairs("known_divergent_pairs.json"))
def test_divergent_pairs_return_divergent(pair):
    a = _facts_from_dict(pair["a"])
    b = _facts_from_dict(pair["b"])
    result = check_resolution_equivalence(a, b)
    assert result == ResolutionMatch.DIVERGENT, (
        f"Expected DIVERGENT for pair '{pair.get('label', '')}', got {result}"
    )


# ─── Unit tests for each divergence type ─────────────────────────────────────

def _base_facts(**kwargs) -> MarketFacts:
    defaults = dict(
        question="Will the Fed cut rates in May 2026?",
        resolution_date="2026-05-31",
        resolution_source="Federal Reserve",
        tie_break_rule=None,
        category="economics",
        outcome_set=("Yes", "No"),
    )
    defaults.update(kwargs)
    return MarketFacts(**defaults)


def test_identical_facts_return_identical():
    a = _base_facts()
    b = _base_facts()
    assert check_resolution_equivalence(a, b) == ResolutionMatch.IDENTICAL


def test_date_within_24h_returns_identical():
    a = _base_facts(resolution_date="2026-05-31")
    b = _base_facts(resolution_date="2026-06-01")  # exactly 1 day later
    assert check_resolution_equivalence(a, b) == ResolutionMatch.IDENTICAL


def test_date_over_24h_returns_divergent():
    """Resolution dates more than 24h apart → DIVERGENT."""
    a = _base_facts(resolution_date="2026-05-31")
    b = _base_facts(resolution_date="2026-07-31")  # 2 months apart
    assert check_resolution_equivalence(a, b) == ResolutionMatch.DIVERGENT


def test_equivalent_source_returns_identical():
    """AP vs 'Associated Press' is in the allow-list → IDENTICAL."""
    a = _base_facts(resolution_source="AP")
    b = _base_facts(resolution_source="Associated Press")
    assert check_resolution_equivalence(a, b) == ResolutionMatch.IDENTICAL


def test_different_source_returns_divergent():
    """AP vs NY Times is not in the allow-list → DIVERGENT."""
    a = _base_facts(resolution_source="AP")
    b = _base_facts(resolution_source="New York Times")
    assert check_resolution_equivalence(a, b) == ResolutionMatch.DIVERGENT


def test_tie_break_rule_mismatch_returns_divergent():
    """Different tie-break rules → DIVERGENT."""
    a = _base_facts(tie_break_rule="most recent official data")
    b = _base_facts(tie_break_rule="no trade / void")
    assert check_resolution_equivalence(a, b) == ResolutionMatch.DIVERGENT


def test_tie_break_both_none_returns_identical():
    """Both None tie-break rules → no mismatch."""
    a = _base_facts(tie_break_rule=None)
    b = _base_facts(tie_break_rule=None)
    assert check_resolution_equivalence(a, b) == ResolutionMatch.IDENTICAL


def test_category_mismatch_returns_divergent():
    """Different categories → DIVERGENT."""
    a = _base_facts(category="economics")
    b = _base_facts(category="politics")
    assert check_resolution_equivalence(a, b) == ResolutionMatch.DIVERGENT


def test_category_both_none_returns_identical():
    """Both None categories → no mismatch."""
    a = _base_facts(category=None)
    b = _base_facts(category=None)
    assert check_resolution_equivalence(a, b) == ResolutionMatch.IDENTICAL


def test_missing_resolution_date_returns_pending():
    """Either side has None resolution_date → PENDING."""
    a = _base_facts(resolution_date=None)
    b = _base_facts()
    assert check_resolution_equivalence(a, b) == ResolutionMatch.PENDING


def test_missing_resolution_source_returns_pending():
    """Either side has None resolution_source → PENDING."""
    a = _base_facts()
    b = _base_facts(resolution_source=None)
    assert check_resolution_equivalence(a, b) == ResolutionMatch.PENDING


def test_both_resolution_date_none_returns_pending():
    """Both sides have None resolution_date → PENDING."""
    a = _base_facts(resolution_date=None)
    b = _base_facts(resolution_date=None)
    assert check_resolution_equivalence(a, b) == ResolutionMatch.PENDING


def test_both_resolution_source_none_returns_pending():
    """Both sides have None resolution_source → PENDING."""
    a = _base_facts(resolution_source=None)
    b = _base_facts(resolution_source=None)
    assert check_resolution_equivalence(a, b) == ResolutionMatch.PENDING


def test_outcome_set_mismatch_returns_divergent():
    """Different outcome sets → DIVERGENT."""
    a = _base_facts(outcome_set=("Yes", "No"))
    b = _base_facts(outcome_set=("Democrats", "Republicans", "Other"))
    assert check_resolution_equivalence(a, b) == ResolutionMatch.DIVERGENT


def test_outcome_set_identical_returns_identical():
    a = _base_facts(outcome_set=("Yes", "No"))
    b = _base_facts(outcome_set=("Yes", "No"))
    assert check_resolution_equivalence(a, b) == ResolutionMatch.IDENTICAL


def test_outcome_set_case_insensitive_identical():
    """Outcome sets should match case-insensitively."""
    a = _base_facts(outcome_set=("yes", "no"))
    b = _base_facts(outcome_set=("Yes", "No"))
    assert check_resolution_equivalence(a, b) == ResolutionMatch.IDENTICAL
