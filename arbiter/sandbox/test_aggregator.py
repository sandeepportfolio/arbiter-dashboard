"""Unit tests for the Phase 4 aggregator library (arbiter.sandbox.aggregator).

These tests are OFFLINE — they synthesize scenario_manifest.json directories under pytest's
`tmp_path` and verify the aggregator's collection, reconciliation, and markdown-rendering
logic against known fixtures. No live API dependency; run on any host.

Plan: 04-08 Task 1 (TDD).
"""
from __future__ import annotations

import json
import pathlib

from arbiter.sandbox.aggregator import (
    DEFAULT_EVIDENCE_ROOT,
    ReconcileReport,
    ScenarioReconcileResult,
    collect_scenario_manifests,
    reconcile_pnl_across_manifests,
    render_validation_markdown,
    write_validation_markdown,
)


def _make_fake_evidence_dir(
    tmp_path: pathlib.Path,
    scenario: str,
    manifest: dict,
    balances_pre=None,
    balances_post=None,
    execution_orders=None,
) -> pathlib.Path:
    """Build a synthetic evidence/04/<scenario>_<ts>/ directory with canonical artifacts."""
    d = tmp_path / scenario
    d.mkdir()
    (d / "scenario_manifest.json").write_text(
        json.dumps(dict(manifest)), encoding="utf-8"
    )
    if balances_pre is not None:
        (d / "balances_pre.json").write_text(
            json.dumps(balances_pre), encoding="utf-8"
        )
    if balances_post is not None:
        (d / "balances_post.json").write_text(
            json.dumps(balances_post), encoding="utf-8"
        )
    if execution_orders is not None:
        (d / "execution_orders.json").write_text(
            json.dumps(execution_orders), encoding="utf-8"
        )
    return d


async def test_collect_empty_when_root_missing(tmp_path):
    """Non-existent evidence root returns empty list (no crash)."""
    manifests = collect_scenario_manifests(tmp_path / "does_not_exist")
    assert manifests == []


async def test_collect_reads_scenario_manifests(tmp_path):
    """Globs scenario_manifest.json files and injects _evidence_dir."""
    _make_fake_evidence_dir(
        tmp_path,
        "scenA",
        {"scenario": "scenA", "tag": "real", "requirement_ids": ["TEST-01"]},
    )
    _make_fake_evidence_dir(
        tmp_path,
        "scenB",
        {"scenario": "scenB", "tag": "injected", "requirement_ids": ["SAFE-03"]},
    )
    manifests = collect_scenario_manifests(tmp_path)
    assert len(manifests) == 2
    scenarios = {m["scenario"] for m in manifests}
    assert scenarios == {"scenA", "scenB"}
    for m in manifests:
        assert "_evidence_dir" in m


async def test_reconcile_pnl_passes_when_within_tolerance(tmp_path):
    """PnL delta ~= recorded, within $0.01, overall_passed=True, phase_gate=PASS."""
    _make_fake_evidence_dir(
        tmp_path,
        "kalshi_happy_lifecycle_20260417T120000Z",
        manifest={
            "scenario": "kalshi_happy_lifecycle",
            "tag": "real",
            "requirement_ids": ["TEST-01", "TEST-03", "TEST-04"],
            "platform_fee": 0.10,
            "computed_fee": 0.10,
        },
        balances_pre={"kalshi": {"balance": 100.00, "timestamp": 0}},
        balances_post={"kalshi": {"balance": 97.50, "timestamp": 0}},
        execution_orders=[{"realized_pnl": -2.50}],
    )
    manifests = collect_scenario_manifests(tmp_path)
    report = reconcile_pnl_across_manifests(manifests)
    assert len(report.results) == 1
    r = report.results[0]
    assert r.pnl_passed is True
    assert r.fee_passed is True
    assert r.overall_passed is True
    assert report.any_real_breach is False
    assert report.phase_gate_status == "PASS"


async def test_reconcile_pnl_hard_gates_on_breach(tmp_path):
    """Real scenario with delta exceeding tolerance triggers phase_gate_status=BLOCKED."""
    _make_fake_evidence_dir(
        tmp_path,
        "kalshi_happy_lifecycle_20260417T120000Z",
        manifest={
            "scenario": "kalshi_happy_lifecycle",
            "tag": "real",
            "requirement_ids": ["TEST-01", "TEST-03"],
        },
        balances_pre={"kalshi": {"balance": 100.00, "timestamp": 0}},
        balances_post={"kalshi": {"balance": 95.00, "timestamp": 0}},
        execution_orders=[{"realized_pnl": -2.50}],  # recorded -2.50 vs actual -5.00 => discrepancy -2.50
    )
    manifests = collect_scenario_manifests(tmp_path)
    report = reconcile_pnl_across_manifests(manifests)
    r = report.results[0]
    assert r.pnl_passed is False
    assert r.overall_passed is False
    assert report.any_real_breach is True
    assert report.phase_gate_status == "BLOCKED"


async def test_reconcile_fee_hard_gates_on_breach(tmp_path):
    """Real scenario with fee discrepancy > tolerance triggers BLOCKED (TEST-04 gate)."""
    _make_fake_evidence_dir(
        tmp_path,
        "kalshi_happy_lifecycle_x",
        manifest={
            "scenario": "kalshi_happy_lifecycle",
            "tag": "real",
            "requirement_ids": ["TEST-01", "TEST-04"],
            "platform_fee": 0.25,
            "computed_fee": 0.10,  # 15 cent discrepancy — well beyond $0.01 tolerance
        },
    )
    manifests = collect_scenario_manifests(tmp_path)
    report = reconcile_pnl_across_manifests(manifests)
    r = report.results[0]
    assert r.fee_passed is False
    assert r.overall_passed is False
    assert report.phase_gate_status == "BLOCKED"


async def test_injected_scenario_passes_without_balance_data(tmp_path):
    """Injected scenarios have no balance/fee data; they pass on manifest claims."""
    _make_fake_evidence_dir(
        tmp_path,
        "one_leg_recovery_injected",
        manifest={
            "scenario": "one_leg_recovery_injected",
            "tag": "injected",
            "requirement_ids": ["SAFE-03", "TEST-01"],
            "telegram_sent": True,
            "ws_event_type": "one_leg_exposure",
        },
    )
    manifests = collect_scenario_manifests(tmp_path)
    report = reconcile_pnl_across_manifests(manifests)
    r = report.results[0]
    assert r.tag == "injected"
    # Injected scenarios should not trip the hard gate when no check is applicable.
    assert r.overall_passed is True
    assert report.any_real_breach is False
    assert report.phase_gate_status == "PASS"


async def test_collect_with_malformed_manifest(tmp_path):
    """Malformed scenario_manifest.json yields a dict with parse_error + placeholder fields."""
    d = tmp_path / "broken_scenario"
    d.mkdir()
    (d / "scenario_manifest.json").write_text("{{ not valid json", encoding="utf-8")
    manifests = collect_scenario_manifests(tmp_path)
    assert len(manifests) == 1
    assert "parse_error" in manifests[0]
    assert manifests[0]["tag"] == "unknown"


async def test_render_markdown_contains_required_sections(tmp_path):
    """Rendered markdown contains frontmatter + scenario table + phase gate + per-task map."""
    _make_fake_evidence_dir(
        tmp_path,
        "scenA",
        manifest={"scenario": "scenA", "tag": "real", "requirement_ids": ["TEST-01"]},
    )
    manifests = collect_scenario_manifests(tmp_path)
    report = reconcile_pnl_across_manifests(manifests)
    md = render_validation_markdown(manifests, report)
    assert "phase_gate_status:" in md
    assert "# Phase 4: Sandbox Validation" in md
    assert "## Scenario Results" in md
    assert "## Per-Task Verification Map" in md
    assert "## Manual-Only Verifications" in md
    assert "scenA" in md


async def test_render_markdown_marks_breach_section_when_blocked(tmp_path):
    """Breach scenario renders the Tolerance Breach + Phase 5 BLOCKED section."""
    _make_fake_evidence_dir(
        tmp_path,
        "kalshi_happy_lifecycle_x",
        manifest={
            "scenario": "kalshi_happy_lifecycle",
            "tag": "real",
            "requirement_ids": ["TEST-01"],
        },
        balances_pre={"kalshi": {"balance": 100.00, "timestamp": 0}},
        balances_post={"kalshi": {"balance": 95.00, "timestamp": 0}},
        execution_orders=[{"realized_pnl": -2.50}],
    )
    manifests = collect_scenario_manifests(tmp_path)
    report = reconcile_pnl_across_manifests(manifests)
    md = render_validation_markdown(manifests, report)
    assert "Phase 5 BLOCKED" in md
    assert "Tolerance Breach" in md


async def test_write_validation_markdown_creates_file(tmp_path):
    """write_validation_markdown writes the rendered content to target_path."""
    _make_fake_evidence_dir(
        tmp_path,
        "scenA",
        manifest={"scenario": "scenA", "tag": "real", "requirement_ids": ["TEST-01"]},
    )
    manifests = collect_scenario_manifests(tmp_path)
    report = reconcile_pnl_across_manifests(manifests)
    target = tmp_path / "out.md"
    write_validation_markdown(manifests, report, target_path=target)
    assert target.exists()
    content = target.read_text(encoding="utf-8")
    assert "phase_gate_status:" in content
    assert "scenA" in content


async def test_default_evidence_root_is_evidence_04():
    """Library constant points at the canonical evidence root per 04-01 / 04-03..07 contract."""
    assert str(DEFAULT_EVIDENCE_ROOT).replace("\\", "/") == "evidence/04"


async def test_reconcile_report_phase_gate_status_pass_when_empty():
    """Empty results => no real breach => phase_gate_status defaults to PASS (awaiting live-fire)."""
    report = ReconcileReport()
    assert report.any_real_breach is False
    assert report.phase_gate_status == "PASS"


async def test_scenario_result_dataclass_shape():
    """ScenarioReconcileResult is a dataclass with the expected attribute set for downstream use."""
    r = ScenarioReconcileResult(
        scenario="x",
        evidence_dir=pathlib.Path("."),
        requirement_ids=[],
        tag="real",
        pnl_check_applicable=False,
        pnl_delta=None,
        pnl_recorded=None,
        pnl_discrepancy=None,
        pnl_passed=None,
        fee_check_applicable=False,
        fee_platform=None,
        fee_computed=None,
        fee_discrepancy=None,
        fee_passed=None,
        overall_passed=True,
    )
    assert r.scenario == "x"
    assert r.tag == "real"
