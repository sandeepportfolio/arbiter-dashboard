# Polymarket US Pivot — Final Status

## Final Commit

```
de244b0e18f5f8abc5e83aa2c0258366e96e7792
test(rollback): smoke tests for POLYMARKET_VARIANT=us|legacy|disabled
```

## Test Counts

| Suite                        | Pass | Skip | Fail |
|------------------------------|------|------|------|
| Default (`pytest -q`)        |  495 |   87 |    0 |
| With slow (`--run-slow`)     |  496 |   86 |    0 |

Notes:
- Baseline (pre-pivot): 483 passed, 83 skipped
- 12 new tests from Task 19 (onboarding script unit tests in `scripts/setup/`)
- 4 new tests from Task 19.5 (rollback smoke tests in `arbiter/live/`) — these pass in isolation
  but are counted as "skipped" in the full suite because `arbiter/sandbox/conftest.py`'s
  `pytest_collection_modifyitems` skips all tests in paths containing "live" unless
  `--live` is passed. This is pre-existing behavior, not introduced by this work.
- The 1 slow test (`test_scale_1000`) moves from skipped→passed with `--run-slow`.

## TypeScript Check

```
npx tsc --noEmit
```

Exit 0, zero errors.

## Preflight Dry-run

```
POLYMARKET_VARIANT=disabled PREFLIGHT_ALLOW_LIVE=0 python -m arbiter.live.preflight
```

Runner completed without crashing. Expected failures (no creds, no DB, no deployed API):
- Check 2: Phase 4 scenarios missing (6/9 observed, expected in dev)
- Check 4: Kalshi credentials unset (expected)
- Check 7: DATABASE_URL unset (expected)
- Check 8: PHASE5_MAX_ORDER_USD unset (expected)
- Check 10: Telegram unset (expected)
- Checks 11-12: API unreachable (expected — service not running)
- Check 13: Polymarket migration ack missing (expected)
- Check 14: No identical-resolution market mapping (expected)
- Check 15: Runbook not acknowledged (expected)

Checks 5 and 16 (Polymarket US / 5a / 5b) correctly show "not applicable" for `disabled` variant.

## Git Log (0501d69..HEAD)

```
de244b0 test(rollback): smoke tests for POLYMARKET_VARIANT=us|legacy|disabled
d62bc5d feat(setup): Playwright onboarding script for Polymarket US dev portal
bf21f01 feat(ops): Prometheus metrics (+9) and Telegram heartbeat (15-min, auto-exec-gated)
fad9e9e feat(setup): check_polymarket_us.py with Ed25519 round-trip + secret-leak guard tests
fff359d feat(preflight): split Polymarket check into 5a credentials + 5b live balance
ec64c00 feat(mapping): 8-condition auto-promote gate (8 negative paths tested)
4311332 feat(mapping): auto-discovery pipeline (2 rps budget, candidate-only)
fa9fbb6 feat(mapping): LLM verifier (Haiku 4.5 with prompt cache, fail-safe to MAYBE)
a8c5109 feat(mapping): resolution-check Layer 1 + hand-labeled fixture corpus (20+20 pairs)
e980079 test(scanner): scale test at n=1000 pairs × 3 updates/sec (0.01ms p99)
fe09bb1 feat(scanner): matcher backpressure + debounce + emit throttle
2a555eb feat(scanner): MatchedPairStream — event-driven O(1) matcher
af69bdd test(adapter): Phase 5 hard-lock suite ported to Polymarket US adapter
304ab1c feat(adapter): PolymarketUSAdapter with ordered hard-lock gates
49e5778 feat(collectors): Polymarket US WebSocket multiplex (100 slugs/conn, reconnect, merged stream)
ff31f66 feat(collectors): Polymarket US REST client (signed, paginated, 429-retry)
38eeae6 docs(env): Polymarket US variant default in production template
57a2330 feat(config): POLYMARKET_VARIANT flag + PolymarketUSConfig class
428ece1 feat(fees): polymarket_us_order_fee with signed maker rebate
371685c fix(conftest): move pytest_plugins to root for pytest 8+ compat
da07281 feat(auth): Ed25519 signer for Polymarket US
93f707c docs(plan): v2 - per-task regression gate, hard-lock order test, rollback smoke (plan review round 1 fixes)
5c428ba docs(plan): 21-task implementation plan for Polymarket US pivot + scale
aaa3fdb docs(spec): Polymarket US pivot + scale-to-thousands design
```
