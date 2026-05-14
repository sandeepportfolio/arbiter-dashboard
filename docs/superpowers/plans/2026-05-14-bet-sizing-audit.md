# Bet Sizing Audit & Fix Implementation Plan

**Goal:** Make bet sizes balance-proportional, edge-aware, and liquidity-adaptive while remaining backward-compatible with the live `.env.production` settings (`MAX_POSITION_USD=10`, `PHASE5_MAX_ORDER_USD=10`).

**Architecture:** Three additive caps composed via `min(...)`:
1. Existing fixed USD caps (`MAX_POSITION_USD`, `PHASE5_MAX_ORDER_USD`) — unchanged.
2. New fraction-of-balance cap (`MAX_BALANCE_FRACTION_PER_TRADE`) — never bet more than X% of the smaller platform balance per trade.
3. New per-platform reserve floor (`MIN_PLATFORM_RESERVE_USD`) — always hold back at least $Y on each platform for unwind/fees.
4. Optional edge-based scaling (`EDGE_SCALING_ENABLED`) — at the minimum gate edge, size at `EDGE_SCALING_MIN_FRACTION` of cap; at `EDGE_SCALING_REF_CENTS`, size at 100% of cap.

Plus: change `AutoExecutor._pre_flight_checks` so insufficient depth reduces qty (halve-and-retry) instead of skipping the trade.

**Tech Stack:** Python 3.12, pytest, dataclass config, `os.getenv` for new env vars.

**Backward compatibility:**
- Defaults preserve current behaviour: `MAX_BALANCE_FRACTION_PER_TRADE=1.0` (no fraction cap), `MIN_PLATFORM_RESERVE_USD=0`, `EDGE_SCALING_ENABLED=false`, `LIQUIDITY_ADAPTIVE_SIZING=true` (this is the only behavioural change at default).
- Production `.env.production.template` gets the *recommended* tuned values (5% fraction, $20 reserve, edge scaling on, adaptive sizing on) so the new caps actually activate in live.
- All new env vars are documented at point of read; existing env vars unchanged.

---

## Task 1: Scanner — fraction-of-balance + min-reserve caps

**Files:**
- Modify: `arbiter/scanner/arbitrage.py:585-622` (`_compute_position_size`)
- Modify: `arbiter/scanner/arbitrage.py:220-240` (`ArbitrageScanner.__init__` — read new env vars)
- Test: `arbiter/scanner/test_arbitrage_scanner.py`

Compose final qty as:
```
qty_max_position    = MAX_POSITION_USD / cost_per_pair         (existing)
qty_fraction        = (fraction × min(bal_yes, bal_no)) / cost  (new)
qty_reserve         = ((min(bal_yes, bal_no) - reserve_floor)
                       × (1 - RESERVE_FRACTION)) / cost          (new)
qty_liquidity       = min(yes_volume, no_volume)               (existing)
qty_legacy_reserve  = (bal × 0.9) / leg_price                  (existing 10% reserve)

suggested_qty = max(0, min(all caps))
```

Edge scaling (when enabled):
```
scalar = clamp(
    min_fraction + (net_edge_cents - min_gate) / (ref_edge - min_gate) × (1 - min_fraction),
    min_fraction, 1.0
)
qty_fraction × = scalar    # only the fraction cap scales
```

## Task 2: AutoExecutor — adaptive depth-shortage handling

**Files:**
- Modify: `arbiter/execution/auto_executor.py:520-557` (depth check loop)
- Modify: `arbiter/execution/auto_executor.py:43-58` (`AutoExecutorConfig` — add adaptive flag)
- Modify: `arbiter/execution/auto_executor.py:690-712` (`make_auto_executor_from_env` — read flag)
- Test: `arbiter/execution/test_auto_executor.py`

When `check_depth(market_id, side, required_qty)` returns `sufficient=False` AND adaptive sizing is on, halve `try_qty` until `sufficient=True` and `try_qty >= 1`. Replace `opp.suggested_qty` with the smallest reduced qty across both sides. Skip only if both halve to zero.

When adaptive sizing is OFF, preserve existing skip behaviour (gate semantics unchanged for legacy callers).

## Task 3: Env templates + settings hook

**Files:**
- Modify: `arbiter/config/settings.py:590-606` (`ScannerConfig` — env-loaded fields)
- Modify: `.env.production.template:104-122` (document recommended values)
- Modify: `.env.template`, `.env.sandbox.template` (mirror defaults)

New ScannerConfig fields:
```python
max_balance_fraction_per_trade: float  # env: MAX_BALANCE_FRACTION_PER_TRADE, default 1.0
min_platform_reserve_usd:       float  # env: MIN_PLATFORM_RESERVE_USD, default 0.0
edge_scaling_enabled:           bool   # env: EDGE_SCALING_ENABLED, default false
edge_scaling_min_fraction:      float  # env: EDGE_SCALING_MIN_FRACTION, default 0.5
edge_scaling_ref_cents:         float  # env: EDGE_SCALING_REF_CENTS, default 10.0
```

## Task 4: Tests

**Files:**
- New tests in `arbiter/scanner/test_arbitrage_scanner.py`:
  - `test_position_size_capped_by_balance_fraction`
  - `test_position_size_reserves_min_platform_usd`
  - `test_position_size_edge_scaling_increases_size_with_edge`
  - `test_position_size_backward_compatible_with_defaults`
- New tests in `arbiter/execution/test_auto_executor.py`:
  - `test_preflight_reduces_qty_on_partial_depth`
  - `test_preflight_skips_when_no_depth_for_qty_1`
  - `test_preflight_skip_preserved_when_adaptive_disabled`

## Task 5: Run full suite, fix regressions, commit
