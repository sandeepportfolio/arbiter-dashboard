"""arbiter.recovery — verify-before-act recovery automation.

This package layers automation on top of the manual recovery surface added in
the 2026-05-22 audit (commits 4b4cb0b, c95c861). Every action in here is
gated by independent evidence — never by the in-memory state of the thing
being recovered — so the automation cannot silently undo the safety
invariants the audit established.

See ``auto_resolver.AutoResolver`` for the four mechanisms:
  A. Half-recorded arb reconciler — only marks the sentinel when an
     auto-unwind incident has already booked the venue-side closure.
  B. Kill-switch classifier — proposes manual disarm via Telegram for
     benign arm reasons; never disarms autonomously.
  C. Warm-restart eligibility — toggles the readiness gate down to a
     reduced sample size when there is historical evidence of successful
     live trading.
  D. Stale-incident expirer — closes non-critical incidents > 24h and
     critical incidents > 48h, but ONLY when no open position references
     the incident's arb.
"""
from __future__ import annotations

from .auto_resolver import (
    AutoResolver,
    AutoResolverConfig,
    KillSwitchClassification,
    KillSwitchVerdict,
    ReconcileOutcome,
    ReconcileResult,
    StaleIncidentExpiry,
    WarmRestartDecision,
)

__all__ = [
    "AutoResolver",
    "AutoResolverConfig",
    "KillSwitchClassification",
    "KillSwitchVerdict",
    "ReconcileOutcome",
    "ReconcileResult",
    "StaleIncidentExpiry",
    "WarmRestartDecision",
]
