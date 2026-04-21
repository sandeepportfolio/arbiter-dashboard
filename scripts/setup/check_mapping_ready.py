"""check_mapping_ready.py — verifies at least one MARKET_MAP entry is
auto-trade-ready.

Preflight #7 (`mapping_has_allow_auto_trade`) is blocking. This script prints
every mapping's status so the operator can pick which one to enable:

    canonical_id                 status      allow_auto_trade  resolution_match_status
    DEM_HOUSE_2026               candidate   False             pending_operator_review
    DEM_SENATE_2026              confirmed   False             pending_operator_review
    GOP_SENATE_2026              confirmed   False             pending_operator_review
    ...

If ≥1 entry has allow_auto_trade=True AND status=confirmed AND
resolution_match_status=identical, exits 0. Else exits 1 with a prompt to
curate one via /ops dashboard.
"""
from __future__ import annotations

import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
except (AttributeError, ValueError):
    pass

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from arbiter.config.settings import MARKET_MAP  # type: ignore


def main() -> int:
    header = f"{'canonical_id':<30} {'status':<12} {'allow_auto':<11} {'match_status':<25}"
    print(header)
    print("-" * len(header))

    ready_count = 0
    for cid, mapping in MARKET_MAP.items():
        status = mapping.get("status", "?")
        allow = bool(mapping.get("allow_auto_trade", False))
        match = mapping.get("resolution_match_status", "pending_operator_review")
        marker = "✓" if (allow and status == "confirmed" and match == "identical") else " "
        print(f"{marker} {cid:<28} {status:<12} {str(allow):<11} {match:<25}")
        if allow and status == "confirmed" and match == "identical":
            ready_count += 1

    if ready_count == 0:
        print()
        print("FAIL — no MARKET_MAP entry is auto-trade-ready.")
        print()
        print("Requirements for a pair to auto-trade:")
        print("  1. status = 'confirmed'                  (click Confirm in /ops → Mappings)")
        print("  2. resolution_match_status = 'identical' (operator judgment — identical on both venues)")
        print("  3. allow_auto_trade = True               (click Enable auto-trade in /ops)")
        print()
        print("Without this, AutoExecutor's gate G4 (allow_auto_trade) blocks every opportunity.")
        return 1

    print()
    print(f"PASS — {ready_count} mapping(s) are auto-trade-ready.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
