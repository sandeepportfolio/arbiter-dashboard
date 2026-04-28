"""
ARBITER — Trade Execution Math Auditor (Shadow Calculator)
Independent verification of arbitrage profit calculations.

Runs as a parallel shadow calculator that re-derives every value from raw
prices and flags any discrepancy > 0.1 cent against the scanner/execution
engine's numbers.

Audit checks per opportunity:
  1. yes_price + no_price + total_fees < $1.00
  2. gross_edge = 1.0 - yes_price - no_price
  3. total_fees = fee_a + fee_b (re-computed from platform fee models)
  4. net_edge = gross_edge - total_fees
  5. net_edge_cents = net_edge * 100
  6. suggested_qty respects position limits (max_position_usd)
  7. max_profit_usd = net_edge * suggested_qty

Discrepancy thresholds:
  - Price/fee/edge discrepancy > 0.001 (0.1 cent) → FLAG
  - Quantity mismatch → FLAG
  - Failed fundamental constraint (yes+no+fees >= 1.0) → CRITICAL
"""
import logging
import math
import time
from dataclasses import dataclass, field
from typing import List, Optional

logger = logging.getLogger("arbiter.audit")


# ─── Independent Fee Models (duplicated on purpose — shadow must not import scanner) ─

def _kalshi_fee(price: float, quantity: int = 1) -> float:
    """Kalshi: quadratic order fee rounded up to the nearest cent."""
    quantity = max(int(quantity), 1)
    raw_fee = 0.07 * quantity * price * (1.0 - price)
    return math.ceil((raw_fee * 100.0) - 1e-9) / 100.0 / quantity


def _polymarket_fee(
    price: float,
    category: str = "politics",
    quantity: int = 1,
    fee_rate: Optional[float] = None,
) -> float:
    """Polymarket: fee rate × price × (1-price), amortized per contract."""
    rates = {
        "crypto": 0.072,
        "sports": 0.03,
        "finance": 0.04,
        "politics": 0.04,
        "economics": 0.05,
        "culture": 0.05,
        "weather": 0.05,
        "tech": 0.04,
        "mentions": 0.04,
        "geopolitics": 0.0,
        "default": 0.05,
    }
    rate = rates.get(category, rates["default"])
    if fee_rate is not None:
        rate = max(float(fee_rate), 0.0)
    quantity = max(int(quantity), 1)
    return (rate * price * (1.0 - price) * quantity) / quantity


# ─── Audit Result ─────────────────────────────────────────────────────────

@dataclass
class AuditFlag:
    """A single discrepancy found by the auditor."""
    field: str
    expected: float
    actual: float
    discrepancy: float
    severity: str  # "info", "warning", "critical"
    message: str


@dataclass
class AuditResult:
    """Complete audit result for one opportunity."""
    canonical_id: str
    yes_platform: str
    no_platform: str
    timestamp: float
    passed: bool
    flags: List[AuditFlag] = field(default_factory=list)
    # Shadow-computed values
    shadow_gross_edge: float = 0.0
    shadow_total_fees: float = 0.0
    shadow_net_edge: float = 0.0
    shadow_net_edge_cents: float = 0.0
    shadow_suggested_qty: int = 0
    shadow_max_profit: float = 0.0

    def to_dict(self) -> dict:
        return {
            "canonical_id": self.canonical_id,
            "yes_platform": self.yes_platform,
            "no_platform": self.no_platform,
            "passed": self.passed,
            "flag_count": len(self.flags),
            "flags": [
                {
                    "field": f.field,
                    "expected": round(f.expected, 6),
                    "actual": round(f.actual, 6),
                    "discrepancy": round(f.discrepancy, 6),
                    "severity": f.severity,
                    "message": f.message,
                }
                for f in self.flags
            ],
            "shadow": {
                "gross_edge": round(self.shadow_gross_edge, 6),
                "total_fees": round(self.shadow_total_fees, 6),
                "net_edge": round(self.shadow_net_edge, 6),
                "net_edge_cents": round(self.shadow_net_edge_cents, 4),
                "suggested_qty": self.shadow_suggested_qty,
                "max_profit_usd": round(self.shadow_max_profit, 4),
            },
            "timestamp": self.timestamp,
        }


# ─── Math Auditor ─────────────────────────────────────────────────────────

# Threshold: flag discrepancies above 0.1 cent (0.001 in dollar terms)
DISCREPANCY_THRESHOLD = 0.001
# Critical: profit discrepancy above 0.5%
CRITICAL_PROFIT_THRESHOLD = 0.005


class MathAuditor:
    """
    Shadow calculator that independently verifies every arb opportunity.

    Does NOT import from scanner or execution — all fee models and position
    sizing logic are re-implemented here to catch bugs in the primary code.
    """

    def __init__(self, max_position_usd: float = 100.0):
        self.max_position_usd = max_position_usd
        self._audit_count = 0
        self._flag_count = 0
        self._critical_count = 0
        self._results: List[AuditResult] = []

    def audit_opportunity(self, opp_dict: dict) -> AuditResult:
        """
        Audit a single opportunity. Accepts a dict with the opportunity fields
        (from ArbitrageOpportunity.to_dict() or equivalent).

        Returns an AuditResult with pass/fail and any flags.
        """
        self._audit_count += 1
        flags: List[AuditFlag] = []

        canonical_id = opp_dict.get("canonical_id", "unknown")
        yes_platform = opp_dict.get("yes_platform", "")
        no_platform = opp_dict.get("no_platform", "")
        yes_price = opp_dict.get("yes_price", 0.0)
        no_price = opp_dict.get("no_price", 0.0)
        reported_gross = opp_dict.get("gross_edge", 0.0)
        reported_total_fees = opp_dict.get("total_fees", 0.0)
        reported_net_edge = opp_dict.get("net_edge", 0.0)
        reported_net_cents = opp_dict.get("net_edge_cents", 0.0)
        reported_qty = opp_dict.get("suggested_qty", 0)
        reported_max_profit = opp_dict.get("max_profit_usd", 0.0)
        reported_liquidity = opp_dict.get("min_available_liquidity", 0.0)
        yes_fee_rate = opp_dict.get("yes_fee_rate")
        no_fee_rate = opp_dict.get("no_fee_rate")

        # ─── Check 1: Gross edge ──────────────────────────────────────
        shadow_gross = 1.0 - yes_price - no_price
        diff = abs(shadow_gross - reported_gross)
        if diff > DISCREPANCY_THRESHOLD:
            flags.append(AuditFlag(
                field="gross_edge",
                expected=shadow_gross,
                actual=reported_gross,
                discrepancy=diff,
                severity="warning",
                message=f"Gross edge mismatch: shadow={shadow_gross:.6f} vs reported={reported_gross:.6f}",
            ))

        # ─── Check 2: Position sizing ─────────────────────────────────
        shadow_qty = self._compute_position_size(
            yes_platform,
            no_platform,
            yes_price,
            no_price,
            min_available_liquidity=reported_liquidity,
        )
        # Allow qty mismatches when the reported qty has been clamped down
        # to fit within a per-order cap (e.g. engine clamps scanner's $100
        # suggested_qty to fit $10 MAX_POSITION_USD). The key safety check
        # is that reported_qty doesn't EXCEED the auditor's shadow_qty,
        # which would mean the engine is trying to trade MORE than allowed.
        if reported_qty > shadow_qty:
            flags.append(AuditFlag(
                field="suggested_qty",
                expected=float(shadow_qty),
                actual=float(reported_qty),
                discrepancy=abs(shadow_qty - reported_qty),
                severity="warning",
                message=f"Quantity exceeds cap: reported={reported_qty} > shadow_max={shadow_qty}",
            ))

        qty_for_fee = max(reported_qty or shadow_qty, 1)

        # ─── Check 3: Fee computation ─────────────────────────────────
        shadow_fee_a = self._compute_fee(yes_platform, yes_price, "yes", qty_for_fee, fee_rate=yes_fee_rate)
        shadow_fee_b = self._compute_fee(no_platform, no_price, "no", qty_for_fee, fee_rate=no_fee_rate)
        shadow_total_fees = shadow_fee_a + shadow_fee_b

        diff = abs(shadow_total_fees - reported_total_fees)
        if diff > DISCREPANCY_THRESHOLD:
            flags.append(AuditFlag(
                field="total_fees",
                expected=shadow_total_fees,
                actual=reported_total_fees,
                discrepancy=diff,
                severity="warning",
                message=(
                    f"Fee mismatch: shadow={shadow_total_fees:.6f} "
                    f"(a={shadow_fee_a:.6f} + b={shadow_fee_b:.6f}) "
                    f"vs reported={reported_total_fees:.6f}"
                ),
            ))

        # ─── Check 4: Net edge ────────────────────────────────────────
        shadow_net = shadow_gross - shadow_total_fees
        diff = abs(shadow_net - reported_net_edge)
        if diff > DISCREPANCY_THRESHOLD:
            severity = "critical" if diff > CRITICAL_PROFIT_THRESHOLD else "warning"
            flags.append(AuditFlag(
                field="net_edge",
                expected=shadow_net,
                actual=reported_net_edge,
                discrepancy=diff,
                severity=severity,
                message=f"Net edge mismatch: shadow={shadow_net:.6f} vs reported={reported_net_edge:.6f}",
            ))

        # ─── Check 5: Net edge in cents ───────────────────────────────
        shadow_cents = shadow_net * 100
        diff = abs(shadow_cents - reported_net_cents)
        if diff > 0.1:  # 0.1 cent threshold
            flags.append(AuditFlag(
                field="net_edge_cents",
                expected=shadow_cents,
                actual=reported_net_cents,
                discrepancy=diff,
                severity="warning",
                message=f"Cents mismatch: shadow={shadow_cents:.4f}¢ vs reported={reported_net_cents:.4f}¢",
            ))

        # ─── Check 6: Fundamental constraint ──────────────────────────
        total_cost = yes_price + no_price + shadow_total_fees
        if total_cost >= 1.0 and shadow_net > 0:
            flags.append(AuditFlag(
                field="fundamental_constraint",
                expected=0.0,
                actual=total_cost,
                discrepancy=total_cost - 1.0,
                severity="critical",
                message=(
                    f"VIOLATION: yes({yes_price:.4f}) + no({no_price:.4f}) + "
                    f"fees({shadow_total_fees:.4f}) = {total_cost:.4f} >= $1.00 "
                    f"but net_edge reported positive"
                ),
            ))

        # ─── Check 7: Max profit ──────────────────────────────────────
        shadow_max_profit = shadow_net * shadow_qty
        # Compare using reported qty for consistency check
        reported_expected_profit = reported_net_edge * reported_qty
        diff = abs(reported_max_profit - reported_expected_profit)
        if diff > DISCREPANCY_THRESHOLD:
            flags.append(AuditFlag(
                field="max_profit_usd",
                expected=reported_expected_profit,
                actual=reported_max_profit,
                discrepancy=diff,
                severity="warning",
                message=(
                    f"Max profit internal inconsistency: "
                    f"net_edge({reported_net_edge:.6f}) × qty({reported_qty}) = "
                    f"{reported_expected_profit:.4f} but reported {reported_max_profit:.4f}"
                ),
            ))

        # ─── Check 8: Negative edge passed as positive ────────────────
        if shadow_net <= 0 and reported_net_edge > 0:
            flags.append(AuditFlag(
                field="sign_check",
                expected=shadow_net,
                actual=reported_net_edge,
                discrepancy=abs(shadow_net - reported_net_edge),
                severity="critical",
                message=(
                    f"CRITICAL: Shadow computes negative/zero edge ({shadow_net:.6f}) "
                    f"but scanner reports positive ({reported_net_edge:.6f})"
                ),
            ))

        # ─── Build result ─────────────────────────────────────────────
        has_critical = any(f.severity == "critical" for f in flags)
        passed = len(flags) == 0

        result = AuditResult(
            canonical_id=canonical_id,
            yes_platform=yes_platform,
            no_platform=no_platform,
            timestamp=time.time(),
            passed=passed,
            flags=flags,
            shadow_gross_edge=shadow_gross,
            shadow_total_fees=shadow_total_fees,
            shadow_net_edge=shadow_net,
            shadow_net_edge_cents=shadow_cents,
            shadow_suggested_qty=shadow_qty,
            shadow_max_profit=shadow_max_profit,
        )

        self._flag_count += len(flags)
        if has_critical:
            self._critical_count += 1

        # Log results
        if has_critical:
            logger.critical(
                f"AUDIT CRITICAL [{canonical_id}] {yes_platform}↔{no_platform}: "
                f"{len(flags)} flags — "
                + "; ".join(f.message for f in flags if f.severity == "critical")
            )
        elif flags:
            logger.warning(
                f"AUDIT FLAG [{canonical_id}] {yes_platform}↔{no_platform}: "
                f"{len(flags)} discrepancies — "
                + "; ".join(f.message for f in flags)
            )
        else:
            logger.debug(
                f"AUDIT PASS [{canonical_id}] {yes_platform}↔{no_platform}: "
                f"net_edge={shadow_cents:.2f}¢ qty={shadow_qty}"
            )

        self._results.append(result)
        return result

    def audit_execution(self, execution_dict: dict) -> AuditResult:
        """
        Audit a completed execution — verifies fill prices and realized PnL
        against the opportunity's expected values.
        """
        opp = execution_dict.get("opportunity", {})
        result = self.audit_opportunity(opp)

        # Additional execution-specific checks
        leg_yes = execution_dict.get("leg_yes", {})
        leg_no = execution_dict.get("leg_no", {})
        reported_pnl = execution_dict.get("realized_pnl", 0.0)

        # Check fill price slippage
        if leg_yes.get("fill_price", 0) > 0:
            yes_slip = abs(leg_yes["fill_price"] - opp.get("yes_price", 0))
            if yes_slip > DISCREPANCY_THRESHOLD:
                result.flags.append(AuditFlag(
                    field="yes_fill_slippage",
                    expected=opp.get("yes_price", 0),
                    actual=leg_yes["fill_price"],
                    discrepancy=yes_slip,
                    severity="warning",
                    message=f"YES fill slippage: expected {opp.get('yes_price', 0):.4f}, got {leg_yes['fill_price']:.4f}",
                ))

        if leg_no.get("fill_price", 0) > 0:
            no_slip = abs(leg_no["fill_price"] - opp.get("no_price", 0))
            if no_slip > DISCREPANCY_THRESHOLD:
                result.flags.append(AuditFlag(
                    field="no_fill_slippage",
                    expected=opp.get("no_price", 0),
                    actual=leg_no["fill_price"],
                    discrepancy=no_slip,
                    severity="warning",
                    message=f"NO fill slippage: expected {opp.get('no_price', 0):.4f}, got {leg_no['fill_price']:.4f}",
                ))

        # Check both legs filled
        yes_status = leg_yes.get("status", "")
        no_status = leg_no.get("status", "")
        if yes_status in ("filled", "simulated") and no_status not in ("filled", "simulated"):
            result.flags.append(AuditFlag(
                field="leg_mismatch",
                expected=1.0,
                actual=0.0,
                discrepancy=1.0,
                severity="critical",
                message=f"ONE-LEG RISK: YES={yes_status} but NO={no_status} — unhedged exposure!",
            ))
        elif no_status in ("filled", "simulated") and yes_status not in ("filled", "simulated"):
            result.flags.append(AuditFlag(
                field="leg_mismatch",
                expected=1.0,
                actual=0.0,
                discrepancy=1.0,
                severity="critical",
                message=f"ONE-LEG RISK: NO={no_status} but YES={yes_status} — unhedged exposure!",
            ))

        result.passed = len(result.flags) == 0
        return result

    def _compute_fee(
        self,
        platform: str,
        price: float,
        side: str,
        quantity: int,
        fee_rate: Optional[float] = None,
    ) -> float:
        """Shadow fee computation — independent of scanner code."""
        if platform == "kalshi":
            return _kalshi_fee(price, quantity)
        elif platform == "polymarket":
            return _polymarket_fee(price, category="politics", quantity=quantity, fee_rate=fee_rate)
        return 0.0

    def _compute_position_size(
        self,
        platform_a: str,
        platform_b: str,
        yes_price: float,
        no_price: float,
        min_available_liquidity: float = 0.0,
    ) -> int:
        """Shadow position sizing — independent of scanner code."""
        cost_per_pair = yes_price + no_price
        if cost_per_pair <= 0:
            return 0

        max_by_capital = int(self.max_position_usd / cost_per_pair)
        if min_available_liquidity > 0:
            max_by_capital = min(max_by_capital, int(max(min_available_liquidity, 1.0)))

        return max(1, max_by_capital)

    @property
    def stats(self) -> dict:
        return {
            "audits_run": self._audit_count,
            "total_flags": self._flag_count,
            "critical_flags": self._critical_count,
            "pass_rate": (
                round((self._audit_count - self._critical_count) / self._audit_count, 4)
                if self._audit_count > 0 else 1.0
            ),
            "recent_results": [r.to_dict() for r in self._results[-10:]],
        }
