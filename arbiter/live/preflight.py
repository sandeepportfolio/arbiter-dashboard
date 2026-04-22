"""Phase 5 Go-Live Preflight — 15-item operator checklist (05-RESEARCH.md).

Runnable two ways:

* CLI: ``python -m arbiter.live.preflight`` — prints a pass/fail table and
  exits 0 if all blocking checks pass, 1 otherwise.
* Pytest: imported from ``arbiter.live.test_preflight`` as unit-testable
  individual ``_check_*`` functions and the ``run_preflight`` coroutine.

Every check returns a ``PreflightItem`` dataclass so the same function can
be asserted in tests AND rendered to an operator-facing table.

Task 16 adds two new checks that replace check 5 when POLYMARKET_VARIANT=us:
  5a — credentials CI-safe (no network)
  5b — live balance check (only runs when PREFLIGHT_ALLOW_LIVE=1)
"""
from __future__ import annotations

import asyncio
import base64
import os
import pathlib
import re
import sys
from dataclasses import dataclass, field
from typing import Any, Callable, Coroutine, List, Optional


# ─── Data models ─────────────────────────────────────────────────────────────


@dataclass
class PreflightItem:
    """Single checklist row — matches 05-RESEARCH.md Go-Live Preflight Checklist."""
    key: str
    label: str
    passed: bool
    blocking: bool
    detail: str = ""

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "label": self.label,
            "passed": self.passed,
            "blocking": self.blocking,
            "detail": self.detail,
        }


@dataclass
class PreflightReport:
    """Aggregate result for all 15 checks."""
    items: List[PreflightItem] = field(default_factory=list)

    @property
    def blocking_failures(self) -> List[PreflightItem]:
        return [i for i in self.items if i.blocking and not i.passed]

    @property
    def passed(self) -> bool:
        return len(self.blocking_failures) == 0

    def to_table(self) -> str:
        """Render the 15-item checklist as an ASCII table."""
        rows = []
        rows.append("| #  | Check                                        | Status | Blocking | Detail")
        rows.append("|----|----------------------------------------------|--------|----------|--------")
        for idx, item in enumerate(self.items, start=1):
            status = "PASS" if item.passed else "FAIL"
            blocking = "YES" if item.blocking else "no "
            label = (item.label[:44]).ljust(44)
            rows.append(
                f"| {idx:2d} | {label} | {status:6s} | {blocking:8s} | {item.detail}"
            )
        rows.append("")
        if self.passed:
            rows.append("OVERALL: PASS — all blocking checks green. Safe to proceed with DRY_RUN=false.")
        else:
            rows.append(
                f"OVERALL: FAIL — {len(self.blocking_failures)} blocking failure(s). "
                "Do NOT set DRY_RUN=false."
            )
        return "\n".join(rows)


# ─── Individual checks (15 total) ────────────────────────────────────────────


def _phase_validation_path() -> pathlib.Path:
    return pathlib.Path(".planning/phases/04-sandbox-validation/04-VALIDATION.md")


def _phase_review_path() -> pathlib.Path:
    return pathlib.Path(".planning/phases/04-sandbox-validation/04-REVIEW.md")


def _read_frontmatter(path: pathlib.Path) -> Optional[dict]:
    """Parse minimal YAML-style ``key: value`` lines from a markdown frontmatter block."""
    if not path.exists():
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except Exception:
        return None
    if not text.startswith("---"):
        return None
    # Split after opening --- and up to next ---
    lines = text.splitlines()
    if len(lines) < 2:
        return None
    out: dict[str, str] = {}
    in_block = False
    for raw in lines:
        line = raw.rstrip()
        if line.startswith("---"):
            if in_block:
                break
            in_block = True
            continue
        if not in_block:
            continue
        m = re.match(r"^(\w[\w_]*)\s*:\s*(.*)$", line)
        if m:
            out[m.group(1)] = m.group(2).strip()
    return out


def _check_01_phase4_gate_passed() -> PreflightItem:
    """Check 1: Phase 4 D-19 gate PASS."""
    fm = _read_frontmatter(_phase_validation_path())
    if fm is None:
        return PreflightItem(
            key="phase4_gate",
            label="Phase 4 D-19 gate PASSED",
            passed=False,
            blocking=True,
            detail="04-VALIDATION.md missing or unreadable",
        )
    status = (fm.get("phase_gate_status") or "").strip().strip('"').strip("'")
    passed = status.upper() == "PASS"
    return PreflightItem(
        key="phase4_gate",
        label="Phase 4 D-19 gate PASSED",
        passed=passed,
        blocking=True,
        detail=f"phase_gate_status={status!r}",
    )


def _check_02_phase4_scenarios_observed() -> PreflightItem:
    """Check 2: all 9 Phase 4 scenarios observed."""
    fm = _read_frontmatter(_phase_validation_path())
    if fm is None:
        return PreflightItem(
            key="phase4_scenarios",
            label="Phase 4 all 9 scenarios observed",
            passed=False,
            blocking=True,
            detail="04-VALIDATION.md missing",
        )
    try:
        observed = int(fm.get("total_scenarios_observed", "0"))
    except ValueError:
        observed = 0
    try:
        missing = int(fm.get("scenarios_missing", "99"))
    except ValueError:
        missing = 99
    passed = observed >= 9 and missing == 0
    return PreflightItem(
        key="phase4_scenarios",
        label="Phase 4 all 9 scenarios observed",
        passed=passed,
        blocking=True,
        detail=f"observed={observed}, missing={missing}",
    )


def _check_03_phase4_review() -> PreflightItem:
    """Check 3: Phase 4 review warnings resolved or advisory."""
    path = _phase_review_path()
    if not path.exists():
        return PreflightItem(
            key="phase4_review",
            label="Phase 4 code-review warnings resolved/advisory",
            passed=True,
            blocking=True,
            detail="04-REVIEW.md not present — treat as no open items (manual attest)",
        )
    try:
        text = path.read_text(encoding="utf-8").lower()
    except Exception as exc:
        return PreflightItem(
            key="phase4_review",
            label="Phase 4 code-review warnings resolved/advisory",
            passed=False,
            blocking=True,
            detail=f"read failed: {exc}",
        )
    # Heuristic: a flagged "blocking" status anywhere fails the check.
    # Operators can override by editing 04-REVIEW.md.
    has_open_blocking = "status: blocking" in text or "status: open" in text
    return PreflightItem(
        key="phase4_review",
        label="Phase 4 code-review warnings resolved/advisory",
        passed=not has_open_blocking,
        blocking=True,
        detail="no open blocking items detected" if not has_open_blocking
               else "open blocking items in 04-REVIEW.md",
    )


def _check_04_kalshi_production_creds() -> PreflightItem:
    """Check 4: Production Kalshi API key issued + loaded."""
    key_id = os.getenv("KALSHI_API_KEY_ID", "")
    key_path = os.getenv("KALSHI_PRIVATE_KEY_PATH", "")
    issues: List[str] = []
    if not key_id:
        issues.append("KALSHI_API_KEY_ID unset")
    if not key_path:
        issues.append("KALSHI_PRIVATE_KEY_PATH unset")
    if key_path and "demo" in key_path.lower():
        issues.append("key path contains 'demo'")
    if key_path and not pathlib.Path(key_path).exists():
        issues.append(f"{key_path} missing on disk")
    passed = not issues
    return PreflightItem(
        key="kalshi_creds",
        label="Kalshi production credentials loaded",
        passed=passed,
        blocking=True,
        detail="OK" if passed else "; ".join(issues),
    )


def _check_05_polymarket_funded() -> PreflightItem:
    """Check 5: Polymarket wallet credentials present (on-chain balance is manual)."""
    pk = os.getenv("POLY_PRIVATE_KEY", "")
    funder = os.getenv("POLY_FUNDER", "")
    issues: List[str] = []
    if not pk:
        issues.append("POLY_PRIVATE_KEY unset")
    if not funder:
        issues.append("POLY_FUNDER unset")
    passed = not issues
    return PreflightItem(
        key="polymarket_funded",
        label="Polymarket wallet credentials present",
        passed=passed,
        blocking=True,
        detail="OK (on-chain balance check is manual)"
               if passed else "; ".join(issues),
    )


def _check_05a_polymarket_us_credentials() -> PreflightItem:
    """Check 5a: Polymarket US API key ID present + secret parseable as >=32-byte Ed25519 seed.

    When POLYMARKET_VARIANT=us: validates credentials without any network call.
    When POLYMARKET_VARIANT=legacy: delegates to the legacy _check_05_polymarket_funded().
    When POLYMARKET_VARIANT=disabled (or unset): both 5a/5b are not applicable.
    """
    variant = os.getenv("POLYMARKET_VARIANT", "legacy").lower()

    if variant == "disabled":
        return PreflightItem(
            key="polymarket_us_creds",
            label="Polymarket US credentials (5a)",
            passed=True,
            blocking=False,
            detail="not applicable (POLYMARKET_VARIANT=disabled)",
        )

    if variant != "us":
        # Legacy path — delegate
        legacy = _check_05_polymarket_funded()
        return PreflightItem(
            key="polymarket_us_creds",
            label="Polymarket US credentials (5a) [legacy variant]",
            passed=legacy.passed,
            blocking=legacy.blocking,
            detail=legacy.detail,
        )

    # US variant — credential check
    key_id = os.getenv("POLYMARKET_US_API_KEY_ID", "")
    secret_b64 = os.getenv("POLYMARKET_US_API_SECRET", "")
    issues: List[str] = []

    if not key_id:
        issues.append("POLYMARKET_US_API_KEY_ID unset")

    if not secret_b64:
        issues.append("POLYMARKET_US_API_SECRET unset")
    else:
        # Validate it decodes to >=32 bytes (Ed25519 seed requirement)
        try:
            raw = base64.b64decode(secret_b64)
            if len(raw) < 32:
                issues.append(
                    f"POLYMARKET_US_API_SECRET decodes to only {len(raw)} bytes; need >=32"
                )
        except Exception:
            issues.append("POLYMARKET_US_API_SECRET is not valid base64")

    passed = not issues
    return PreflightItem(
        key="polymarket_us_creds",
        label="Polymarket US credentials (5a)",
        passed=passed,
        blocking=True,
        detail="key_id set; secret is valid >=32-byte Ed25519 seed"
               if passed else "; ".join(issues),
    )


def _polymarket_us_balances_endpoint(base_url: str) -> tuple[str, str, str]:
    """Support both api base styles used in the repo for live balance checks."""
    base = base_url.rstrip("/")
    if base.endswith("/v1"):
        return base, "/account/balances", "/v1/account/balances"
    return base, "/v1/account/balances", "/v1/account/balances"


async def _check_05b_polymarket_us_balance() -> PreflightItem:
    """Check 5b: Live signed GET /v1/account/balances; assert currentBalance >= $20.

    Only runs when PREFLIGHT_ALLOW_LIVE=1. Otherwise returns a SKIPPED result
    (not a blocking failure) so CI never contacts the live API.

    When POLYMARKET_VARIANT=legacy: delegates to the legacy _check_05_polymarket_funded().
    When POLYMARKET_VARIANT=disabled: not applicable.
    """
    variant = os.getenv("POLYMARKET_VARIANT", "legacy").lower()

    if variant == "disabled":
        return PreflightItem(
            key="polymarket_us_balance",
            label="Polymarket US live balance >=20 (5b)",
            passed=True,
            blocking=False,
            detail="not applicable (POLYMARKET_VARIANT=disabled)",
        )

    if variant != "us":
        # Legacy path — delegate (marks as non-blocking advisory)
        legacy = _check_05_polymarket_funded()
        return PreflightItem(
            key="polymarket_us_balance",
            label="Polymarket US live balance >=20 (5b) [legacy variant]",
            passed=legacy.passed,
            blocking=False,
            detail=legacy.detail,
        )

    # Guard: 5b NEVER runs without PREFLIGHT_ALLOW_LIVE=1
    allow_live = os.getenv("PREFLIGHT_ALLOW_LIVE", "").strip() == "1"
    if not allow_live:
        return PreflightItem(
            key="polymarket_us_balance",
            label="Polymarket US live balance >=20 (5b)",
            passed=True,
            blocking=False,
            detail="SKIPPED (live check, set PREFLIGHT_ALLOW_LIVE=1 to enable)",
        )

    # Credentials must be present for the live call
    key_id = os.getenv("POLYMARKET_US_API_KEY_ID", "")
    secret_b64 = os.getenv("POLYMARKET_US_API_SECRET", "")
    if not key_id or not secret_b64:
        return PreflightItem(
            key="polymarket_us_balance",
            label="Polymarket US live balance >=20 (5b)",
            passed=False,
            blocking=True,
            detail="credentials missing — run 5a first",
        )

    # Live signed request
    import aiohttp
    try:
        from arbiter.auth.ed25519_signer import Ed25519Signer, SignatureError
        from arbiter.collectors.polymarket_us import extract_current_balance
    except Exception as exc:
        return PreflightItem(
            key="polymarket_us_balance",
            label="Polymarket US live balance >=20 (5b)",
            passed=False,
            blocking=True,
            detail=f"Ed25519Signer import failed: {exc}",
        )

    try:
        signer = Ed25519Signer(key_id=key_id, secret_b64=secret_b64)
    except Exception as exc:
        return PreflightItem(
            key="polymarket_us_balance",
            label="Polymarket US live balance >=20 (5b)",
            passed=False,
            blocking=True,
            detail=f"Ed25519Signer init failed: {exc}",
        )

    base_url = os.getenv("POLYMARKET_US_API_URL", "https://api.polymarket.us").rstrip("/")
    base_url, request_path, signature_path = _polymarket_us_balances_endpoint(base_url)
    headers = signer.headers("GET", signature_path)

    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
            async with session.get(f"{base_url}{request_path}", headers=headers) as resp:
                status = resp.status
                if status == 401:
                    return PreflightItem(
                        key="polymarket_us_balance",
                        label="Polymarket US live balance >=20 (5b)",
                        passed=False,
                        blocking=True,
                        detail="HTTP 401 — check POLYMARKET_US_API_KEY_ID + POLYMARKET_US_API_SECRET match",
                    )
                if status != 200:
                    return PreflightItem(
                        key="polymarket_us_balance",
                        label="Polymarket US live balance >=20 (5b)",
                        passed=False,
                        blocking=True,
                        detail=f"unexpected HTTP {status}",
                    )
                body = await resp.json()
    except Exception as exc:
        return PreflightItem(
            key="polymarket_us_balance",
            label="Polymarket US live balance >=20 (5b)",
            passed=False,
            blocking=False,
            detail=f"network error: {exc.__class__.__name__}",
        )

    # Parse currentBalance from either the current flat payload or the nested
    # balances[] shape returned by the current docs.
    try:
        current_balance = extract_current_balance(body)
    except (TypeError, ValueError):
        return PreflightItem(
            key="polymarket_us_balance",
            label="Polymarket US live balance >=20 (5b)",
            passed=False,
            blocking=True,
            detail=f"could not parse currentBalance from response: {body!r:.200}",
        )

    passed = current_balance >= 20.0
    return PreflightItem(
        key="polymarket_us_balance",
        label="Polymarket US live balance >=20 (5b)",
        passed=passed,
        blocking=True,
        detail=f"currentBalance=${current_balance:.2f}"
               if passed
               else f"currentBalance=${current_balance:.2f} < $20.00 minimum",
    )


def _check_06_kalshi_funded() -> PreflightItem:
    """Check 6: Kalshi account funded — proxy check via credentials presence.

    Actual balance check requires an authenticated Kalshi API call, which is
    not safe to automate here (it would make a network call from preflight).
    Operator must manually verify funding via the Kalshi web UI.
    """
    key_id = os.getenv("KALSHI_API_KEY_ID", "")
    passed = bool(key_id)
    return PreflightItem(
        key="kalshi_funded",
        label="Kalshi account funded (manual — UI check)",
        passed=passed,
        blocking=True,
        detail="credentials present; operator verifies balance in UI"
               if passed else "KALSHI_API_KEY_ID unset — cannot even self-check",
    )


def _check_07_database_url_live() -> PreflightItem:
    """Check 7: DATABASE_URL points at arbiter_live (not sandbox, not dev)."""
    url = os.getenv("DATABASE_URL", "")
    if not url:
        return PreflightItem(
            key="database_url",
            label="DATABASE_URL points at arbiter_live",
            passed=False,
            blocking=True,
            detail="DATABASE_URL unset",
        )
    has_live = "arbiter_live" in url
    has_sandbox = "arbiter_sandbox" in url
    has_dev = "arbiter_dev" in url
    passed = has_live and not has_sandbox and not has_dev
    if passed:
        detail = "arbiter_live detected"
    else:
        bits = []
        if not has_live:
            bits.append("missing 'arbiter_live'")
        if has_sandbox:
            bits.append("contains 'arbiter_sandbox'")
        if has_dev:
            bits.append("contains 'arbiter_dev'")
        detail = "; ".join(bits)
    return PreflightItem(
        key="database_url",
        label="DATABASE_URL points at arbiter_live",
        passed=passed,
        blocking=True,
        detail=detail,
    )


def _check_08_phase5_max_order_usd() -> PreflightItem:
    """Check 8: PHASE5_MAX_ORDER_USD set to a value in (0, 10]."""
    raw = os.getenv("PHASE5_MAX_ORDER_USD", "")
    if not raw:
        return PreflightItem(
            key="phase5_cap",
            label="PHASE5_MAX_ORDER_USD set to <=$10",
            passed=False,
            blocking=True,
            detail="unset",
        )
    try:
        cap = float(raw)
    except ValueError:
        return PreflightItem(
            key="phase5_cap",
            label="PHASE5_MAX_ORDER_USD set to <=$10",
            passed=False,
            blocking=True,
            detail=f"unparseable: {raw!r}",
        )
    passed = 0 < cap <= 10.0
    return PreflightItem(
        key="phase5_cap",
        label="PHASE5_MAX_ORDER_USD set to <=$10",
        passed=passed,
        blocking=True,
        detail=f"cap=${cap:.2f}" if passed
               else f"cap=${cap:.2f} outside (0, 10]",
    )


def _check_09_phase4_polarity() -> PreflightItem:
    """Check 9 (W-2 polarity fix): PHASE4_MAX_ORDER_USD absence is EXPECTED in prod.

    Block ONLY when both caps are set AND PHASE4 < PHASE5 (the unsafe inversion
    where Phase 4's tighter cap would reject below Phase 5's belt).
    """
    phase4_raw = os.getenv("PHASE4_MAX_ORDER_USD", "")
    phase5_raw = os.getenv("PHASE5_MAX_ORDER_USD", "")

    if not phase4_raw:
        return PreflightItem(
            key="phase4_polarity",
            label="PHASE4_MAX_ORDER_USD polarity sane",
            passed=True,
            blocking=False,
            detail="PHASE4_MAX_ORDER_USD unset (expected in production)",
        )

    # Both set — parse and compare.
    try:
        phase4_cap = float(phase4_raw)
    except ValueError:
        return PreflightItem(
            key="phase4_polarity",
            label="PHASE4_MAX_ORDER_USD polarity sane",
            passed=False,
            blocking=True,
            detail=f"PHASE4_MAX_ORDER_USD unparseable: {phase4_raw!r}",
        )

    if not phase5_raw:
        # PHASE4 set without PHASE5 — warning only (Phase 5 belt missing is
        # covered by check #8; don't double-block here).
        return PreflightItem(
            key="phase4_polarity",
            label="PHASE4_MAX_ORDER_USD polarity sane",
            passed=True,
            blocking=False,
            detail=f"PHASE4 set (${phase4_cap:.2f}) but PHASE5 unset — check #8 owns this",
        )

    try:
        phase5_cap = float(phase5_raw)
    except ValueError:
        return PreflightItem(
            key="phase4_polarity",
            label="PHASE4_MAX_ORDER_USD polarity sane",
            passed=False,
            blocking=True,
            detail=f"PHASE5_MAX_ORDER_USD unparseable: {phase5_raw!r}",
        )

    if phase4_cap < phase5_cap:
        return PreflightItem(
            key="phase4_polarity",
            label="PHASE4_MAX_ORDER_USD polarity sane",
            passed=False,
            blocking=True,
            detail=(
                f"UNSAFE INVERSION: PHASE4=${phase4_cap:.2f} < "
                f"PHASE5=${phase5_cap:.2f}; Phase 4 would reject below Phase 5 belt"
            ),
        )

    return PreflightItem(
        key="phase4_polarity",
        label="PHASE4_MAX_ORDER_USD polarity sane",
        passed=True,
        blocking=False,
        detail=(
            f"PHASE4=${phase4_cap:.2f} >= PHASE5=${phase5_cap:.2f} "
            "(Phase 5 wins by being stricter)"
        ),
    )


def _check_10_telegram_configured() -> PreflightItem:
    """Check 10: Telegram alerting configured (dry-test is manual)."""
    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat = os.getenv("TELEGRAM_CHAT_ID", "")
    issues: List[str] = []
    if not token:
        issues.append("TELEGRAM_BOT_TOKEN unset")
    if not chat:
        issues.append("TELEGRAM_CHAT_ID unset")
    passed = not issues
    return PreflightItem(
        key="telegram",
        label="Telegram alerting configured",
        passed=passed,
        blocking=True,
        detail="config present; operator verifies delivery manually"
               if passed else "; ".join(issues),
    )


async def _check_11_dashboard_kill_switch(dashboard_url: Optional[str] = None) -> PreflightItem:
    """Check 11: Dashboard kill-switch endpoint reachable.

    Non-blocking when the process isn't running (marked manual).
    """
    import aiohttp
    url = dashboard_url or os.getenv("DASHBOARD_URL", "http://localhost:8080")
    endpoint = f"{url.rstrip('/')}/api/kill-switch"
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=2.0)) as session:
            async with session.get(endpoint) as resp:
                passed = resp.status in (200, 405)  # 405 = POST-only is also fine
                return PreflightItem(
                    key="dashboard_kill_switch",
                    label="Dashboard kill-switch endpoint reachable",
                    passed=passed,
                    blocking=True,
                    detail=f"GET {endpoint} -> HTTP {resp.status}",
                )
    except Exception as exc:
        return PreflightItem(
            key="dashboard_kill_switch",
            label="Dashboard kill-switch endpoint reachable",
            passed=False,
            blocking=False,  # MANUAL — process may not be running yet
            detail=f"unreachable ({exc.__class__.__name__}) — start arbiter.main and re-run",
        )


async def _check_12_readiness_endpoint(dashboard_url: Optional[str] = None) -> PreflightItem:
    """Check 12: /api/readiness reports ready_for_live_trading=true.

    Non-blocking when the process isn't running (manual).
    """
    import aiohttp
    url = dashboard_url or os.getenv("DASHBOARD_URL", "http://localhost:8080")
    endpoint = f"{url.rstrip('/')}/api/readiness"
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=2.0)) as session:
            async with session.get(endpoint) as resp:
                if resp.status != 200:
                    return PreflightItem(
                        key="readiness",
                        label="Readiness endpoint reports ready_for_live_trading",
                        passed=False,
                        blocking=True,
                        detail=f"HTTP {resp.status}",
                    )
                body = await resp.json()
                ready = bool(body.get("ready_for_live_trading"))
                return PreflightItem(
                    key="readiness",
                    label="Readiness endpoint reports ready_for_live_trading",
                    passed=ready,
                    blocking=True,
                    detail=f"ready_for_live_trading={ready}",
                )
    except Exception as exc:
        return PreflightItem(
            key="readiness",
            label="Readiness endpoint reports ready_for_live_trading",
            passed=False,
            blocking=False,  # MANUAL
            detail=f"unreachable ({exc.__class__.__name__}) — start arbiter.main and re-run",
        )


def _check_13_polymarket_migration() -> PreflightItem:
    """Check 13: Polymarket April 2026 migration compatibility — manual."""
    try:
        import py_clob_client  # type: ignore
        version = getattr(py_clob_client, "__version__", "unknown")
    except Exception:
        return PreflightItem(
            key="polymarket_migration",
            label="Polymarket April 2026 migration compatibility",
            passed=False,
            blocking=True,
            detail="py_clob_client import failed",
        )
    # We can detect the library is installed but not verify migration state
    # programmatically. Mark as manual; operator attests.
    acked = os.getenv("POLYMARKET_MIGRATION_ACK", "").upper() == "ACKNOWLEDGED"
    return PreflightItem(
        key="polymarket_migration",
        label="Polymarket April 2026 migration compatibility",
        passed=acked,
        blocking=True,
        detail=(
            f"py_clob_client={version}; "
            + ("operator ACKNOWLEDGED via POLYMARKET_MIGRATION_ACK"
               if acked
               else "set POLYMARKET_MIGRATION_ACK=ACKNOWLEDGED after manual verification")
        ),
    )


def _check_14_identical_mapping_present() -> PreflightItem:
    """Check 14: At least one MARKET_MAP entry has resolution_match_status=identical."""
    try:
        from arbiter.config.settings import iter_confirmed_market_mappings
    except Exception as exc:
        return PreflightItem(
            key="identical_mapping",
            label="MARKET_MAP has identical-resolution mapping",
            passed=False,
            blocking=True,
            detail=f"config import failed: {exc}",
        )
    count = 0
    for canonical_id, mapping in iter_confirmed_market_mappings(require_auto_trade=True):
        if isinstance(mapping, dict):
            status = mapping.get("resolution_match_status", "") or ""
        else:
            status = getattr(mapping, "resolution_match_status", "") or ""
        if str(status).lower() == "identical":
            count += 1
    passed = count >= 1
    return PreflightItem(
        key="identical_mapping",
        label="MARKET_MAP has identical-resolution mapping",
        passed=passed,
        blocking=True,
        detail=f"{count} mapping(s) with resolution_match_status=identical"
               if passed else "no identical-resolution mapping found — first trade requires one",
    )


def _check_15_operator_runbook_ack() -> PreflightItem:
    """Check 15: Operator acknowledged reading the runbook.

    Set OPERATOR_RUNBOOK_ACK=ACKNOWLEDGED in env to pass.
    """
    ack = os.getenv("OPERATOR_RUNBOOK_ACK", "").upper()
    passed = ack == "ACKNOWLEDGED"
    return PreflightItem(
        key="operator_runbook_ack",
        label="Operator read & acknowledged runbook",
        passed=passed,
        blocking=True,
        detail="OPERATOR_RUNBOOK_ACK=ACKNOWLEDGED"
               if passed
               else "set OPERATOR_RUNBOOK_ACK=ACKNOWLEDGED after reading arbiter/live/README.md",
    )


# ─── Orchestrator + CLI ──────────────────────────────────────────────────────


async def run_preflight(dashboard_url: Optional[str] = None) -> PreflightReport:
    """Run all 16 checks. Sync checks are called directly; async ones awaited.

    Task 16: adds 5a (credentials) + 5b (live balance) to the original 15 checks.
    5a replaces the legacy check-5 position; 5b is appended as an additional async
    check giving 16 total items.

    ``dashboard_url`` defaults to ``os.getenv("DASHBOARD_URL", "http://localhost:8080")``.
    """
    sync_checks: List[Callable[[], PreflightItem]] = [
        _check_01_phase4_gate_passed,
        _check_02_phase4_scenarios_observed,
        _check_03_phase4_review,
        _check_04_kalshi_production_creds,
        _check_05a_polymarket_us_credentials,   # replaces _check_05_polymarket_funded
        _check_06_kalshi_funded,
        _check_07_database_url_live,
        _check_08_phase5_max_order_usd,
        _check_09_phase4_polarity,
        _check_10_telegram_configured,
        _check_13_polymarket_migration,
        _check_14_identical_mapping_present,
        _check_15_operator_runbook_ack,
    ]
    items: List[PreflightItem] = [check() for check in sync_checks]

    # Async checks (dashboard endpoints + live balance) — run concurrently.
    async_checks: List[Coroutine[Any, Any, PreflightItem]] = [
        _check_11_dashboard_kill_switch(dashboard_url),
        _check_12_readiness_endpoint(dashboard_url),
        _check_05b_polymarket_us_balance(),
    ]
    async_items = await asyncio.gather(*async_checks, return_exceptions=False)
    # Splice the async results back into positions 11 and 12 (1-indexed).
    items.insert(10, async_items[0])
    items.insert(11, async_items[1])
    # 5b appended at the end (16th item)
    items.append(async_items[2])

    return PreflightReport(items=items)


def main() -> int:
    """CLI entry point. Exits 0 on overall pass, 1 on any blocking failure."""
    try:
        report = asyncio.run(run_preflight())
    except Exception as exc:  # pragma: no cover — top-level safety net
        sys.stderr.write(f"Preflight crashed: {exc!r}\n")
        return 1
    print(report.to_table())
    return 0 if report.passed else 1


if __name__ == "__main__":
    sys.exit(main())
