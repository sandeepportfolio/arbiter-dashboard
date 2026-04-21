"""
Resolution-check Layer 1 — structured-field equivalence gate.

Returns ResolutionMatch based on comparing structured MarketFacts from two
platforms. IDENTICAL means the markets resolve to the same real-world event.
DIVERGENT means a confirmed mismatch. PENDING means not enough data.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

# ─── Source allow-list (canonically equivalent names) ─────────────────────────

# Each inner set is treated as a group of equivalent resolution sources.
# Two sources match if they belong to the same group.
_SOURCE_EQUIV_GROUPS: list[frozenset[str]] = [
    frozenset({"AP", "Associated Press"}),
    frozenset({"Federal Reserve", "Fed"}),
    frozenset({"Bureau of Labor Statistics", "BLS"}),
    frozenset({"Bureau of Economic Analysis", "BEA"}),
    frozenset({"European Central Bank", "ECB"}),
]

# Build a lookup: source_lower → group_id
_SOURCE_GROUP: dict[str, int] = {}
for _gid, _group in enumerate(_SOURCE_EQUIV_GROUPS):
    for _src in _group:
        _SOURCE_GROUP[_src.lower()] = _gid


def _sources_equivalent(a: str, b: str) -> bool:
    """Return True if a and b are in the same allow-list group, or are equal."""
    if a.lower() == b.lower():
        return True
    gid_a = _SOURCE_GROUP.get(a.lower())
    gid_b = _SOURCE_GROUP.get(b.lower())
    if gid_a is not None and gid_a == gid_b:
        return True
    return False


# ─── Date tolerance ────────────────────────────────────────────────────────────

_MAX_DATE_DELTA_HOURS = 24


def _parse_date(s: str) -> datetime:
    """Parse an ISO date string (YYYY-MM-DD or full ISO datetime) to UTC datetime."""
    try:
        # Try full ISO datetime first (with timezone)
        return datetime.fromisoformat(s).replace(tzinfo=timezone.utc) if s.endswith("Z") or "+" in s else datetime.fromisoformat(s).replace(tzinfo=timezone.utc)
    except ValueError:
        pass
    # Fallback: date-only string YYYY-MM-DD
    dt = datetime.strptime(s, "%Y-%m-%d")
    return dt.replace(tzinfo=timezone.utc)


def _dates_within_tolerance(a: str, b: str) -> bool:
    """Return True if dates are within _MAX_DATE_DELTA_HOURS hours of each other."""
    try:
        da = _parse_date(a)
        db = _parse_date(b)
        delta = abs((da - db).total_seconds())
        return delta <= _MAX_DATE_DELTA_HOURS * 3600
    except (ValueError, TypeError):
        return False


# ─── Public types ─────────────────────────────────────────────────────────────

class ResolutionMatch(Enum):
    IDENTICAL = "identical"
    DIVERGENT = "divergent"
    PENDING = "pending"


@dataclass
class MarketFacts:
    """Structured resolution facts extracted from a single market."""
    question: str
    resolution_date: Optional[str]       # ISO date (YYYY-MM-DD) or None
    resolution_source: Optional[str]     # Who determines the outcome
    tie_break_rule: Optional[str]        # Tie-break procedure or None
    category: Optional[str]             # Market category or None
    outcome_set: tuple[str, ...]         # e.g. ("Yes", "No")


# ─── Core function ────────────────────────────────────────────────────────────

def check_resolution_equivalence(a: MarketFacts, b: MarketFacts) -> ResolutionMatch:
    """Layer 1 structured-field equivalence check.

    Returns:
        IDENTICAL  — every comparable field matches (within allow-lists / tolerances).
        DIVERGENT  — at least one field has an unallow-listed mismatch.
        PENDING    — either side is missing resolution_date or resolution_source,
                     so we cannot determine equivalence.

    Rules applied in order (first PENDING/DIVERGENT check wins):
    1. PENDING if either resolution_date is None.
    2. PENDING if either resolution_source is None.
    3. DIVERGENT if resolution_dates differ by more than 24h.
    4. DIVERGENT if resolution_sources are not equivalent (allow-list checked).
    5. DIVERGENT if tie_break_rule values are both non-None and unequal.
    6. DIVERGENT if categories are both non-None and unequal.
    7. DIVERGENT if outcome_sets differ (case-insensitive comparison).
    8. Otherwise IDENTICAL.
    """
    # ── Step 1 & 2: PENDING checks ──
    if a.resolution_date is None or b.resolution_date is None:
        return ResolutionMatch.PENDING
    if a.resolution_source is None or b.resolution_source is None:
        return ResolutionMatch.PENDING

    # ── Step 3: Date tolerance ──
    if not _dates_within_tolerance(a.resolution_date, b.resolution_date):
        return ResolutionMatch.DIVERGENT

    # ── Step 4: Source equivalence ──
    if not _sources_equivalent(a.resolution_source, b.resolution_source):
        return ResolutionMatch.DIVERGENT

    # ── Step 5: Tie-break rule ──
    # Only compare if both sides supply a rule. One side None → skip check.
    if a.tie_break_rule is not None and b.tie_break_rule is not None:
        if a.tie_break_rule.strip().lower() != b.tie_break_rule.strip().lower():
            return ResolutionMatch.DIVERGENT

    # ── Step 6: Category ──
    if a.category is not None and b.category is not None:
        if a.category.strip().lower() != b.category.strip().lower():
            return ResolutionMatch.DIVERGENT

    # ── Step 7: Outcome set (case-insensitive) ──
    a_outcomes = frozenset(o.lower() for o in a.outcome_set)
    b_outcomes = frozenset(o.lower() for o in b.outcome_set)
    if a_outcomes != b_outcomes:
        return ResolutionMatch.DIVERGENT

    return ResolutionMatch.IDENTICAL
