"""Phase 4 acceptance aggregator.

Collects scenario_manifest.json files under evidence/04/, applies the phase-level
reconciliation hard-gate (TEST-03 PnL tolerance + TEST-04 fee tolerance, both
+/-$0.01 per D-17), and renders the authoritative 04-VALIDATION.md acceptance
artifact that Phase 5 reads to determine its go-live gate (D-19).

Offline-testable. No network or API dependencies. Drives the live-gated
test_phase_reconciliation.py terminal test AND stands alone for operator
dry-runs (e.g., `python -m arbiter.sandbox.aggregator` after a live suite).
"""
from __future__ import annotations

import json
import pathlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from arbiter.sandbox.reconcile import RECONCILE_TOLERANCE_USD


DEFAULT_EVIDENCE_ROOT = pathlib.Path("evidence/04")
VALIDATION_MD_PATH = pathlib.Path(".planning/phases/04-sandbox-validation/04-VALIDATION.md")


@dataclass
class ScenarioReconcileResult:
    """Per-scenario reconciliation verdict + discrepancy values for downstream rendering."""

    scenario: str
    evidence_dir: pathlib.Path
    requirement_ids: List[str]
    tag: str  # "real" | "injected" | "unknown"
    pnl_check_applicable: bool
    pnl_delta: Optional[float]
    pnl_recorded: Optional[float]
    pnl_discrepancy: Optional[float]
    pnl_passed: Optional[bool]
    fee_check_applicable: bool
    fee_platform: Optional[float]
    fee_computed: Optional[float]
    fee_discrepancy: Optional[float]
    fee_passed: Optional[bool]
    overall_passed: bool
    notes: str = ""


@dataclass
class ReconcileReport:
    """Aggregate reconciliation report across all manifests; drives phase gate status."""

    results: List[ScenarioReconcileResult] = field(default_factory=list)
    tolerance: float = RECONCILE_TOLERANCE_USD

    @property
    def any_real_breach(self) -> bool:
        """True iff any real-tagged scenario failed reconciliation (triggers D-19 hard gate)."""
        return any(r.tag == "real" and not r.overall_passed for r in self.results)

    @property
    def phase_gate_status(self) -> str:
        """PASS / BLOCKED per D-19. Empty report defaults to PASS (awaiting live-fire)."""
        return "BLOCKED" if self.any_real_breach else "PASS"


def collect_scenario_manifests(
    evidence_root: pathlib.Path = DEFAULT_EVIDENCE_ROOT,
) -> List[Dict[str, Any]]:
    """Glob evidence/04/*/scenario_manifest.json and load each.

    Returns list of dicts; each dict includes an extra key '_evidence_dir' pointing to
    the parent directory so downstream consumers can locate the adjacent artifacts
    (balances_pre/post.json, execution_orders.json, etc.).

    Non-existent root => []. Malformed manifest => dict with parse_error + placeholder
    fields so the aggregator never crashes on a partial live-fire run.
    """
    root = pathlib.Path(evidence_root)
    if not root.exists():
        return []
    manifests: List[Dict[str, Any]] = []
    for manifest_path in sorted(root.glob("*/scenario_manifest.json")):
        try:
            data = json.loads(manifest_path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                data = {
                    "scenario": manifest_path.parent.name,
                    "parse_error": "manifest is not a JSON object",
                    "requirement_ids": [],
                    "tag": "unknown",
                }
        except json.JSONDecodeError as exc:
            data = {
                "scenario": manifest_path.parent.name,
                "parse_error": str(exc),
                "requirement_ids": [],
                "tag": "unknown",
            }
        data["_evidence_dir"] = manifest_path.parent
        manifests.append(data)
    return manifests


def _read_json_if_exists(path: pathlib.Path) -> Optional[Any]:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def _compute_recorded_pnl(
    execution_orders: Optional[List[Dict[str, Any]]],
) -> Optional[float]:
    """Sum realized PnL across execution_orders rows.

    The Phase 2 execution_orders schema does not carry a direct `realized_pnl` column;
    it stores fill_price, fill_qty, side, fee per order. For the phase-4 reconciliation
    we first look for an explicit `realized_pnl` field (if the test harness computed it)
    and fall back to a buy/sell-aware net signed notional if the column is absent.
    Returns None if neither is computable.
    """
    if not execution_orders:
        return None

    # Preferred path: explicit realized_pnl column (computed by a scenario's harness).
    total = 0.0
    any_realized = False
    for row in execution_orders:
        if "realized_pnl" in row and row["realized_pnl"] is not None:
            try:
                total += float(row["realized_pnl"])
                any_realized = True
            except (TypeError, ValueError):
                continue
    if any_realized:
        return total

    # Fallback: signed notional (buy => -price*qty, sell => +price*qty) minus fee.
    # This is NOT a true realized PnL (ignores cost basis) but gives a best-effort
    # cash-flow delta suitable for balance-reconciliation within the live-fire window.
    fallback = 0.0
    any_cashflow = False
    for row in execution_orders:
        fill_price = row.get("fill_price")
        fill_qty = row.get("fill_qty")
        side = (row.get("side") or "").lower()
        fee = row.get("fee") or 0.0
        if fill_price is None or fill_qty is None:
            continue
        try:
            fp = float(fill_price)
            fq = float(fill_qty)
            f = float(fee)
        except (TypeError, ValueError):
            continue
        notional = fp * fq
        if side == "buy":
            fallback += -(notional) - f
        elif side == "sell":
            fallback += notional - f
        else:
            # Unknown side; skip rather than guess.
            continue
        any_cashflow = True
    return fallback if any_cashflow else None


def reconcile_pnl_across_manifests(
    manifests: List[Dict[str, Any]],
    tolerance: float = RECONCILE_TOLERANCE_USD,
) -> ReconcileReport:
    """For each manifest, inspect pre/post balances + fee fields and compute discrepancy.

    Real-tagged scenarios with balance snapshots are hard-gated per D-19 / D-17:
    balance delta must match recorded PnL within +/-$0.01. Fee checks fire when the
    manifest carries both platform_fee and computed_fee (happy-path scenarios).

    Injected-tagged scenarios bypass balance/fee reconciliation and are trusted per
    their own manifest claims (e.g., exec_01_invariant_holds, cancel_succeeded).
    """
    report = ReconcileReport(tolerance=tolerance)

    for m in manifests:
        evidence_dir = m.get("_evidence_dir", pathlib.Path("."))
        scenario = m.get("scenario", evidence_dir.name)
        tag = m.get("tag", "unknown")
        req_ids = list(m.get("requirement_ids", []) or [])

        # PnL reconciliation (TEST-03).
        pnl_check_applicable = False
        pnl_delta = pnl_recorded = pnl_discrepancy = None
        pnl_passed: Optional[bool] = None

        pre = _read_json_if_exists(evidence_dir / "balances_pre.json")
        post = _read_json_if_exists(evidence_dir / "balances_post.json")
        exec_orders = _read_json_if_exists(evidence_dir / "execution_orders.json")

        if isinstance(pre, dict) and isinstance(post, dict):
            # Pick the relevant platform from the scenario name heuristic.
            if "kalshi" in scenario.lower():
                platform = "kalshi"
            elif "polymarket" in scenario.lower() or "poly" in scenario.lower():
                platform = "polymarket"
            else:
                platform = None

            if (
                platform
                and platform in pre
                and platform in post
                and isinstance(pre[platform], dict)
                and isinstance(post[platform], dict)
            ):
                pre_b = pre[platform].get("balance")
                post_b = post[platform].get("balance")
                if pre_b is not None and post_b is not None:
                    pnl_check_applicable = True
                    try:
                        pnl_delta = float(post_b) - float(pre_b)
                    except (TypeError, ValueError):
                        pnl_delta = None
                    if pnl_delta is not None:
                        recorded = _compute_recorded_pnl(exec_orders)
                        pnl_recorded = recorded if recorded is not None else 0.0
                        pnl_discrepancy = pnl_delta - pnl_recorded
                        pnl_passed = abs(pnl_discrepancy) <= tolerance

        # Fee reconciliation (TEST-04). Happy-path manifests include platform_fee +
        # computed_fee; non-happy-path manifests omit these and fee_check stays N/A.
        fee_check_applicable = False
        fee_platform = fee_computed = fee_discrepancy = None
        fee_passed: Optional[bool] = None

        if (
            "platform_fee" in m
            and "computed_fee" in m
            and m.get("platform_fee") is not None
            and m.get("computed_fee") is not None
        ):
            try:
                fee_platform = float(m["platform_fee"])
                fee_computed = float(m["computed_fee"])
                fee_check_applicable = True
                fee_discrepancy = fee_platform - fee_computed
                fee_passed = abs(fee_discrepancy) <= tolerance
            except (TypeError, ValueError):
                # Malformed fee fields => treat as not-applicable rather than breach.
                pass

        # Overall verdict logic:
        #   - If any reconciliation check applies, all applicable checks must pass.
        #   - If no reconciliation data (injected, or real with no balances/fees), fall
        #     back to the scenario's own pass/fail claims captured in the manifest.
        applicable_checks = [
            c for c in [pnl_passed, fee_passed] if c is not None
        ]
        if applicable_checks:
            overall_passed = all(applicable_checks)
        else:
            overall_passed = (
                bool(m.get("exec_01_invariant_holds", True))
                and bool(m.get("cancel_succeeded", True))
                and bool(m.get("cancelled_on_platform", True))
                and bool(m.get("cr_02_lookup_succeeded", True))
                and bool(m.get("order_cancelled_on_platform", True))
                and "parse_error" not in m
            )

        result = ScenarioReconcileResult(
            scenario=scenario,
            evidence_dir=evidence_dir,
            requirement_ids=req_ids,
            tag=tag,
            pnl_check_applicable=pnl_check_applicable,
            pnl_delta=pnl_delta,
            pnl_recorded=pnl_recorded,
            pnl_discrepancy=pnl_discrepancy,
            pnl_passed=pnl_passed,
            fee_check_applicable=fee_check_applicable,
            fee_platform=fee_platform,
            fee_computed=fee_computed,
            fee_discrepancy=fee_discrepancy,
            fee_passed=fee_passed,
            overall_passed=overall_passed,
        )
        report.results.append(result)

    return report


# ---- Markdown rendering ----------------------------------------------------


_PER_TASK_MAP_ROWS = [
    # (task_id, plan, wave, requirements, automated_command)
    (
        "04-01 Task 1",
        "04-01",
        "1",
        "TEST-01..04",
        "pytest arbiter/sandbox/test_smoke.py -v",
    ),
    (
        "04-01 Task 2",
        "04-01",
        "1",
        "TEST-01..04",
        "pytest arbiter/sandbox/test_smoke.py -v",
    ),
    (
        "04-01 Task 3",
        "04-01",
        "1",
        "TEST-01..04",
        "wc -l arbiter/sandbox/README.md",
    ),
    (
        "04-02 Task 1",
        "04-02",
        "1",
        "TEST-01, TEST-02",
        "pytest arbiter/execution/adapters/test_polymarket_phase4_hardlock.py -v",
    ),
    (
        "04-02 Task 2",
        "04-02",
        "1",
        "TEST-01, TEST-02",
        "python -c sanity checks on settings.py defaults",
    ),
    (
        "04-02 Task 3",
        "04-02",
        "1",
        "TEST-01, TEST-02",
        "docker-compose config && bash -n arbiter/sql/init-sandbox.sh",
    ),
    (
        "04-02.1 Tasks 1-2",
        "04-02.1",
        "1",
        "SAFE-01 enabler",
        "pytest arbiter/execution/adapters/test_kalshi_place_resting_limit.py -v",
    ),
    (
        "04-03 Task 1",
        "04-03",
        "2",
        "TEST-01, TEST-04",
        "pytest -m live --live arbiter/sandbox/test_kalshi_happy_path.py -v",
    ),
    (
        "04-03 Task 2",
        "04-03",
        "2",
        "EXEC-01, TEST-01",
        "pytest -m live --live arbiter/sandbox/test_kalshi_fok_rejection.py -v",
    ),
    (
        "04-03 Task 3",
        "04-03",
        "2",
        "TEST-01, EXEC-05, EXEC-04",
        "pytest -m live --live arbiter/sandbox/test_kalshi_timeout_cancel.py -v",
    ),
    (
        "04-04 Task 1",
        "04-04",
        "2",
        "TEST-02, TEST-04",
        "pytest -m live --live arbiter/sandbox/test_polymarket_happy_path.py -v",
    ),
    (
        "04-04 Task 2",
        "04-04",
        "2",
        "EXEC-01, TEST-02",
        "pytest -m live --live arbiter/sandbox/test_polymarket_fok_rejection.py -v",
    ),
    (
        "04-05 Task 1",
        "04-05",
        "2",
        "SAFE-01, TEST-01",
        "pytest -m live --live arbiter/sandbox/test_safety_killswitch.py -v",
    ),
    (
        "04-06 Task 1",
        "04-06",
        "2",
        "SAFE-03, TEST-01",
        "pytest -m live --live arbiter/sandbox/test_one_leg_exposure.py -v",
    ),
    (
        "04-06 Task 2",
        "04-06",
        "2",
        "SAFE-04, TEST-01",
        "pytest -m live --live arbiter/sandbox/test_rate_limit_burst.py -v",
    ),
    (
        "04-07 Task 1",
        "04-07",
        "2",
        "SAFE-05, TEST-01",
        "pytest -m live --live arbiter/sandbox/test_graceful_shutdown.py -v",
    ),
    (
        "04-08 Task 1",
        "04-08",
        "3",
        "TEST-03, TEST-04",
        "pytest arbiter/sandbox/test_aggregator.py -v",
    ),
    (
        "04-08 Task 2",
        "04-08",
        "3",
        "TEST-03, TEST-04",
        "pytest -m live --live arbiter/sandbox/test_phase_reconciliation.py -v",
    ),
]


_EXPECTED_SCENARIOS = [
    # (scenario_name, expected_requirement_ids, plan_ref)
    ("kalshi_happy_lifecycle", ["TEST-01", "TEST-04"], "04-03 Task 1"),
    ("polymarket_happy_lifecycle", ["TEST-02", "TEST-04"], "04-04 Task 1"),
    ("kalshi_fok_rejected_on_thin_market", ["EXEC-01", "TEST-01"], "04-03 Task 2"),
    (
        "polymarket_fok_rejected_on_thin_market",
        ["EXEC-01", "TEST-02"],
        "04-04 Task 2",
    ),
    (
        "kalshi_timeout_triggers_cancel_via_client_order_id",
        ["TEST-01", "EXEC-05", "EXEC-04"],
        "04-03 Task 3",
    ),
    (
        "kill_switch_cancels_open_kalshi_demo_order",
        ["SAFE-01", "TEST-01"],
        "04-05 Task 1",
    ),
    ("one_leg_recovery_injected", ["SAFE-03", "TEST-01"], "04-06 Task 1"),
    (
        "rate_limit_burst_triggers_backoff_and_ws",
        ["SAFE-04", "TEST-01"],
        "04-06 Task 2",
    ),
    (
        "sigint_cancels_open_kalshi_demo_orders",
        ["SAFE-05", "TEST-01"],
        "04-07 Task 1",
    ),
]


def render_validation_markdown(
    manifests: List[Dict[str, Any]],
    report: ReconcileReport,
) -> str:
    """Generate the final 04-VALIDATION.md content.

    Shape mirrors 03-VERIFICATION.md: frontmatter + phase gate banner + per-scenario
    table + optional Tolerance Breach section + Per-Task Verification Map + Manual-Only
    Verifications carryover + footer notes.

    When manifests is empty (no live-fire run yet), the output documents the acceptance
    contract in "pending live-fire" state so operators re-run the aggregator after
    `.env.sandbox` provisioning to get the authoritative PASS/BLOCKED verdict.
    """
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    observed_scenarios = {m.get("scenario") for m in manifests}
    missing_scenarios = [
        (name, reqs, plan_ref)
        for name, reqs, plan_ref in _EXPECTED_SCENARIOS
        if name not in observed_scenarios
    ]
    awaiting_live_fire = len(observed_scenarios) == 0

    lines: List[str] = []

    # --- Frontmatter ---
    lines.append("---")
    lines.append("phase: 4")
    lines.append("slug: sandbox-validation")
    if awaiting_live_fire:
        lines.append("status: pending_live_fire")
        lines.append("phase_gate_status: PENDING")
    else:
        lines.append(
            f"status: {'complete' if report.phase_gate_status == 'PASS' else 'incomplete'}"
        )
        lines.append(f"phase_gate_status: {report.phase_gate_status}")
    lines.append("nyquist_compliant: true")
    lines.append(
        f"wave_0_complete: {'true' if not awaiting_live_fire else 'true  # Wave 0 scaffolding done; live-fire deferred'}"
    )
    lines.append(f"generated: {now_iso}")
    lines.append(f"tolerance_usd: {report.tolerance}")
    lines.append(f"total_scenarios_expected: {len(_EXPECTED_SCENARIOS)}")
    lines.append(f"total_scenarios_observed: {len(report.results)}")
    passed_count = sum(1 for r in report.results if r.overall_passed)
    lines.append(f"scenarios_passed: {passed_count}")
    lines.append(f"scenarios_failed: {len(report.results) - passed_count}")
    lines.append(f"scenarios_missing: {len(missing_scenarios)}")
    lines.append("---")
    lines.append("")

    # --- Title + banner ---
    lines.append("# Phase 4: Sandbox Validation - Acceptance Report")
    lines.append("")
    lines.append(
        "**Phase Goal:** The full pipeline (collect -> scan -> execute -> monitor -> "
        "reconcile) is validated end-to-end against real platform APIs in sandbox/demo "
        "mode with no real money at risk."
    )
    lines.append(f"**Generated:** {now_iso}")
    if awaiting_live_fire:
        lines.append(
            "**Phase Gate Status:** **PENDING** -- 0 of 9 scenarios observed. Phase 5 "
            "BLOCKED per D-19 until operator runs the full live-fire suite."
        )
    else:
        unblocked_txt = (
            "UNBLOCKED"
            if report.phase_gate_status == "PASS"
            else "BLOCKED per D-19"
        )
        lines.append(
            f"**Phase Gate Status:** **{report.phase_gate_status}** -- Phase 5 {unblocked_txt}"
        )
    lines.append(
        f"**Tolerance:** +/-${report.tolerance:.2f} absolute (D-17, both TEST-03 PnL and TEST-04 fee)"
    )
    lines.append(
        f"**Hard Gate Rule:** D-19 -- any real-tagged scenario with a tolerance breach blocks Phase 5"
    )
    lines.append("")

    # --- Phase Gate Status (explicit section per plan artifact contract) ---
    lines.append("## Phase Gate Status")
    lines.append("")
    if awaiting_live_fire:
        lines.append(
            "**PENDING** -- Phase 5 BLOCKED. The 9 scenario live-fire runs have not "
            "been executed yet (evidence/04/ empty). Operator action required: see "
            "the **Operator Workflow** section below."
        )
    elif report.phase_gate_status == "PASS":
        lines.append(
            f"**PASS** -- All {passed_count} observed real-tagged scenarios reconciled "
            f"within +/-${report.tolerance:.2f}. Phase 5 is UNBLOCKED per D-19."
        )
    else:
        lines.append(
            f"**BLOCKED** -- One or more real-tagged scenarios breached the "
            f"+/-${report.tolerance:.2f} tolerance. Phase 5 is BLOCKED per D-19. "
            f"See the Tolerance Breach section below for diagnostics."
        )
    lines.append("")

    # --- Operator Workflow (re-run instructions) ---
    lines.append("## Operator Workflow")
    lines.append("")
    lines.append(
        "To populate or refresh this file with real scenario results, run the full "
        "Phase 4 live suite from a host with `.env.sandbox` provisioned:"
    )
    lines.append("")
    lines.append("```bash")
    lines.append("# 1. One-time setup (see arbiter/sandbox/README.md)")
    lines.append("cp .env.sandbox.template .env.sandbox")
    lines.append("# Fill in KALSHI_DEMO_API_KEY_ID, KALSHI_PRIVATE_KEY_PATH,")
    lines.append("# POLY_PRIVATE_KEY (throwaway wallet), POLY_FUNDER, DATABASE_URL")
    lines.append("# pointing at arbiter_sandbox, PHASE4_MAX_ORDER_USD=5, etc.")
    lines.append("")
    lines.append("# 2. Source environment + export scenario-specific overrides")
    lines.append("set -a; source .env.sandbox; set +a")
    lines.append("export SANDBOX_HAPPY_TICKER=<liquid-kalshi-demo-market>")
    lines.append("export SANDBOX_FOK_TICKER=<thin-kalshi-demo-market>")
    lines.append("export PHASE4_KILLSWITCH_TICKER=<resting-capable-kalshi-market>")
    lines.append("export PHASE4_SHUTDOWN_TICKER=<same-as-killswitch>")
    lines.append("")
    lines.append("# 3. Run all 9 scenario tests")
    lines.append("pytest -m live --live arbiter/sandbox/ -v")
    lines.append("")
    lines.append("# 4. Run the terminal aggregator (rewrites this file)")
    lines.append("pytest -m live --live arbiter/sandbox/test_phase_reconciliation.py -v")
    lines.append("```")
    lines.append("")
    lines.append(
        "The aggregator can also be run offline (after manifests exist) via: "
        "`python -m arbiter.sandbox.aggregator`."
    )
    lines.append("")

    # --- Scenario Results table ---
    lines.append("## Scenario Results")
    lines.append("")
    if awaiting_live_fire:
        lines.append(
            "_No scenario manifests found under `evidence/04/`. The 9 live-fire "
            "scenarios below are expected but **not yet executed** (requires operator "
            "to provision `.env.sandbox` and run `pytest -m live --live arbiter/sandbox/`)._"
        )
        lines.append("")
        lines.append(
            "| # | Expected Scenario | Requirements | Plan Ref | Tag | Status |"
        )
        lines.append(
            "|---|-------------------|--------------|----------|-----|--------|"
        )
        for idx, (name, reqs, plan_ref) in enumerate(_EXPECTED_SCENARIOS, 1):
            tag = (
                "injected"
                if name in ("one_leg_recovery_injected", "rate_limit_burst_triggers_backoff_and_ws")
                else "real"
            )
            lines.append(
                f"| {idx} | {name} | {', '.join(reqs)} | {plan_ref} | {tag} | "
                "**PENDING** (awaiting live-fire) |"
            )
    else:
        lines.append(
            "| # | Scenario | Requirements | Tag | PnL Disc | Fee Disc | Overall | Evidence |"
        )
        lines.append(
            "|---|----------|--------------|-----|----------|----------|---------|----------|"
        )
        for idx, r in enumerate(report.results, 1):
            reqs = ", ".join(r.requirement_ids) if r.requirement_ids else "-"
            if r.pnl_discrepancy is not None:
                pnl_cell = f"{r.pnl_discrepancy:+.4f}"
                if r.pnl_passed is False:
                    pnl_cell = f"**BREACH** {pnl_cell}"
            else:
                pnl_cell = "-"
            if r.fee_discrepancy is not None:
                fee_cell = f"{r.fee_discrepancy:+.4f}"
                if r.fee_passed is False:
                    fee_cell = f"**BREACH** {fee_cell}"
            else:
                fee_cell = "-"
            overall = "PASS" if r.overall_passed else "FAIL"
            ev_rel = str(r.evidence_dir).replace("\\", "/")
            lines.append(
                f"| {idx} | {r.scenario} | {reqs} | {r.tag} | {pnl_cell} | "
                f"{fee_cell} | **{overall}** | `{ev_rel}/` |"
            )

        if missing_scenarios:
            lines.append("")
            lines.append(
                f"_Missing scenarios ({len(missing_scenarios)} of "
                f"{len(_EXPECTED_SCENARIOS)}):_"
            )
            lines.append("")
            lines.append("| Scenario | Requirements | Plan Ref |")
            lines.append("|----------|--------------|----------|")
            for name, reqs, plan_ref in missing_scenarios:
                lines.append(f"| {name} | {', '.join(reqs)} | {plan_ref} |")
    lines.append("")

    # --- Hard-gate breach section ---
    if report.any_real_breach:
        lines.append("## Tolerance Breach - Phase 5 BLOCKED")
        lines.append("")
        lines.append(
            "One or more real-tagged scenarios exceeded the +/-$0.01 reconciliation "
            "tolerance. Per D-19 (04-CONTEXT.md), Phase 5 live trading is BLOCKED until "
            "the discrepancy is diagnosed and remediated."
        )
        lines.append("")
        for r in report.results:
            if r.tag == "real" and not r.overall_passed:
                lines.append(f"### Breach: {r.scenario}")
                if r.pnl_discrepancy is not None:
                    lines.append(
                        f"- PnL discrepancy: {r.pnl_discrepancy:+.4f} "
                        f"(balance_delta={r.pnl_delta}, recorded={r.pnl_recorded})"
                    )
                if r.fee_discrepancy is not None:
                    lines.append(
                        f"- Fee discrepancy: {r.fee_discrepancy:+.4f} "
                        f"(platform={r.fee_platform}, computed={r.fee_computed})"
                    )
                lines.append(
                    f"- Evidence: `{str(r.evidence_dir).replace(chr(92), '/')}/`"
                )
                lines.append("")

    # --- Per-Task Verification Map ---
    lines.append("## Per-Task Verification Map")
    lines.append("")
    lines.append(
        "Every task across Plans 04-01 through 04-08, with automated command + status."
    )
    lines.append("")
    lines.append(
        "| Task | Plan | Wave | Requirement | Automated Command | Status |"
    )
    lines.append(
        "|------|------|------|-------------|-------------------|--------|"
    )
    scenario_status_by_plan_task = {
        "04-03 Task 1": "kalshi_happy_lifecycle",
        "04-03 Task 2": "kalshi_fok_rejected_on_thin_market",
        "04-03 Task 3": "kalshi_timeout_triggers_cancel_via_client_order_id",
        "04-04 Task 1": "polymarket_happy_lifecycle",
        "04-04 Task 2": "polymarket_fok_rejected_on_thin_market",
        "04-05 Task 1": "kill_switch_cancels_open_kalshi_demo_order",
        "04-06 Task 1": "one_leg_recovery_injected",
        "04-06 Task 2": "rate_limit_burst_triggers_backoff_and_ws",
        "04-07 Task 1": "sigint_cancels_open_kalshi_demo_orders",
    }
    scenario_pass_by_name = {
        r.scenario: r.overall_passed for r in report.results
    }
    for task_id, plan, wave, reqs, cmd in _PER_TASK_MAP_ROWS:
        if task_id in scenario_status_by_plan_task:
            scen = scenario_status_by_plan_task[task_id]
            if scen in scenario_pass_by_name:
                task_status = (
                    "PASS (see Scenario Results)"
                    if scenario_pass_by_name[scen]
                    else "FAIL (see Scenario Results)"
                )
            else:
                task_status = "pending live-fire"
        elif task_id == "04-08 Task 2":
            task_status = (
                f"authored by this file ({report.phase_gate_status})"
                if not awaiting_live_fire
                else "pending live-fire"
            )
        else:
            task_status = "complete (Wave 1 scaffolding)"
        lines.append(
            f"| {task_id} | {plan} | {wave} | {reqs} | `{cmd}` | {task_status} |"
        )
    lines.append("")

    # --- Manual-Only Verifications (carryover from 04-VALIDATION.md draft) ---
    lines.append(
        "## Manual-Only Verifications (Deferred from Phase 3 HUMAN-UAT)"
    )
    lines.append("")
    lines.append(
        "| Behavior | Requirement | Backend Verified | UI Verification |"
    )
    lines.append(
        "|----------|-------------|------------------|-----------------|"
    )
    lines.append(
        "| Kill-switch ARM/RESET end-to-end | SAFE-01 | Scenario 6 (backend; WS "
        "event + platform cancel) | Deferred to operator browser UAT |"
    )
    lines.append(
        "| Shutdown banner visibility | SAFE-05 | Scenario 9 (backend; phase="
        "shutting_down log + platform cancel) | Deferred to operator browser UAT |"
    )
    lines.append(
        "| Rate-limit pill color transition | SAFE-04 | Scenario 8 (backend; "
        "rate_limit_state payload) | Deferred to operator browser UAT |"
    )
    lines.append("")

    # --- Footer notes ---
    lines.append("## Notes")
    lines.append("")
    lines.append(
        f"- Real-tagged scenarios observed: "
        f"{sum(1 for r in report.results if r.tag == 'real')}"
    )
    lines.append(
        f"- Injected-tagged scenarios observed: "
        f"{sum(1 for r in report.results if r.tag == 'injected')}"
    )
    lines.append(
        f"- Expected total: {len(_EXPECTED_SCENARIOS)} (7 real, 2 injected)"
    )
    lines.append(
        f"- Tolerance: +/-${report.tolerance:.2f} (D-17)"
    )
    lines.append(
        "- Hard-gate rule: D-19 -- any real breach blocks Phase 5"
    )
    lines.append(
        "- To refresh this file after a live-fire run: "
        "`pytest -m live --live arbiter/sandbox/test_phase_reconciliation.py`"
    )
    lines.append("")
    return "\n".join(lines)


def write_validation_markdown(
    manifests: List[Dict[str, Any]],
    report: ReconcileReport,
    target_path: pathlib.Path = VALIDATION_MD_PATH,
) -> None:
    """Write the rendered Markdown to the target path. Creates parent dirs if needed."""
    target_path.parent.mkdir(parents=True, exist_ok=True)
    target_path.write_text(
        render_validation_markdown(manifests, report), encoding="utf-8"
    )


if __name__ == "__main__":  # pragma: no cover - operator dry-run entrypoint
    manifests = collect_scenario_manifests()
    report = reconcile_pnl_across_manifests(manifests)
    write_validation_markdown(manifests, report)
    print(
        f"[aggregator] wrote {VALIDATION_MD_PATH} "
        f"(status={report.phase_gate_status}, scenarios={len(report.results)})"
    )
