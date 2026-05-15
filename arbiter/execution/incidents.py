"""Helpers for interpreting execution incidents consistently across gates."""
from __future__ import annotations

from typing import Any


def effective_critical_incident_count(incidents: list[Any]) -> int:
    """Count unresolved critical incidents, expanding summary incidents.

    Startup recovery emits a single runtime summary for many persisted
    half-recorded arbs. Gating must remain fail-closed, but operator-facing
    counts should reflect the underlying blockers, not just the summary row.
    """
    count = 0
    for incident in incidents:
        if str(getattr(incident, "severity", "")).lower() != "critical":
            continue
        metadata = getattr(incident, "metadata", {}) or {}
        if (
            isinstance(metadata, dict)
            and str(metadata.get("event_type", "")).lower() == "half_recorded_arb_summary"
        ):
            try:
                count += max(1, int(metadata.get("count") or 1))
            except (TypeError, ValueError):
                count += 1
            continue
        count += 1
    return count
