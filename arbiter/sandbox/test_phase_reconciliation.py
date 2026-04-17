"""Phase 4 terminal reconciliation test.

Aggregates all scenario_manifest.json files written by Plans 04-03 through 04-07,
applies the phase-level reconciliation hard-gate (TEST-03 PnL + TEST-04 fee, both
+/-$0.01 per D-17), writes the authoritative 04-VALIDATION.md acceptance artifact,
and enforces the D-19 hard gate via `pytest.fail` on any real-tagged breach.

This is the LAST test in the Phase 4 live suite. Operator workflow:

    set -a; source .env.sandbox; set +a
    pytest -m live --live arbiter/sandbox/                  # runs all 9 scenarios
    pytest -m live --live arbiter/sandbox/test_phase_reconciliation.py  # aggregator

The second invocation (or running the full sandbox dir, which orders alphabetically)
reads the per-scenario evidence captured by the first and produces the final Phase 4
verdict in `.planning/phases/04-sandbox-validation/04-VALIDATION.md`.

Plan: 04-08 Task 2. Requirements: TEST-01, TEST-02, TEST-03, TEST-04. Phase gate: D-19.
"""
from __future__ import annotations

import pytest

from arbiter.sandbox.aggregator import (
    VALIDATION_MD_PATH,
    collect_scenario_manifests,
    reconcile_pnl_across_manifests,
    write_validation_markdown,
)


@pytest.mark.live
async def test_phase_reconciliation_and_validation_report():
    """Collect manifests, reconcile, write 04-VALIDATION.md, D-19 hard-gate on breach."""
    manifests = collect_scenario_manifests()
    assert manifests, (
        "No scenario manifests found under evidence/04/. This terminal test requires "
        "that Plans 04-03 through 04-07 have been run live (pytest -m live --live) "
        "before this test. Expected >=9 manifests (one per scenario). Source "
        ".env.sandbox and run the full live suite, then re-run this test."
    )

    report = reconcile_pnl_across_manifests(manifests)

    # ALWAYS write the VALIDATION.md so operator has the full report even on failure.
    write_validation_markdown(manifests, report, target_path=VALIDATION_MD_PATH)

    # D-19 HARD GATE: any real-tagged scenario with a tolerance breach fails the test.
    if report.any_real_breach:
        breach_lines = []
        for r in report.results:
            if r.tag == "real" and not r.overall_passed:
                breach_lines.append(
                    f"- {r.scenario}: "
                    f"pnl_discrepancy={r.pnl_discrepancy} "
                    f"fee_discrepancy={r.fee_discrepancy} "
                    f"evidence={r.evidence_dir}"
                )
        pytest.fail(
            "D-19 HARD GATE: Phase 4 has reconciliation breach on real-tagged "
            "scenarios. Phase 5 BLOCKED.\n"
            + "\n".join(breach_lines)
            + f"\nSee: {VALIDATION_MD_PATH}"
        )

    # Scenario-count sanity (warning-only — operator may run a subset for debugging).
    expected_scenarios = {
        "kalshi_happy_lifecycle",
        "polymarket_happy_lifecycle",
        "kalshi_fok_rejected_on_thin_market",
        "polymarket_fok_rejected_on_thin_market",
        "kalshi_timeout_triggers_cancel_via_client_order_id",
        "kill_switch_cancels_open_kalshi_demo_order",
        "one_leg_recovery_injected",
        "rate_limit_burst_triggers_backoff_and_ws",
        "sigint_cancels_open_kalshi_demo_orders",
    }
    observed = {m.get("scenario") for m in manifests}
    missing = expected_scenarios - observed
    if missing:
        import warnings
        warnings.warn(
            f"Phase 4 missing scenario coverage (ran a subset): {sorted(missing)}"
        )

    # All real-tagged scenarios within tolerance and report written.
    assert report.phase_gate_status == "PASS", (
        f"Unexpected phase_gate_status: {report.phase_gate_status}. "
        f"See {VALIDATION_MD_PATH} for detail."
    )
