---
phase: 06-production-automation
plan: 03
status: complete
key_files:
  created:
    - arbiter/notifiers/__init__.py
    - arbiter/notifiers/telegram.py
    - arbiter/notifiers/test_telegram.py
  modified:
    - arbiter/monitor/balance.py
    - arbiter/safety/supervisor.py
tests_added: 10
tests_passing: 10
---

# Plan 06-03 — Telegram Alerting Pipeline — SUMMARY

## What was built

Enhanced `TelegramNotifier` (already existed in `arbiter/monitor/balance.py`) with:
  - **Retry**: 3 attempts with exponential backoff (0.5s → 1s → 2s) on aiohttp transient errors + 5xx/429 responses.
  - **Dedup**: sliding-window dedup (default 60s) keyed by caller-supplied `dedup_key`. Bounded-memory compaction at 256 entries.
  - **Fail-fast on 4xx** (except 429): bad token, missing chat — give up immediately, don't retry.
  - **Backwards-compatible**: old `send(msg)` signature still works.

New namespace `arbiter/notifiers/` with:
  - `__init__.py` re-exports TelegramNotifier from the monitor module.
  - `telegram.py` adds CLI dry-test entry point: `python -m arbiter.notifiers.telegram` sends a test message, exit 0 on success.
  - `test_telegram.py` with 10 unit tests covering every branch.

Wired semantic `dedup_key` into `SafetySupervisor` alert calls:
  - `trip_kill`: `dedup_key=f"kill_armed:{by}"` — only first-arm-per-actor fires Telegram
  - `reset_kill`: `dedup_key=f"kill_reset:{by}"`
  - `one_leg_exposure`: `dedup_key=f"one_leg:{canonical_id}"` — same incident doesn't spam

## Tests

```
arbiter/notifiers/test_telegram.py
  test_disabled_returns_false_no_network  PASSED
  test_200_success                        PASSED
  test_5xx_retry_succeeds_on_third        PASSED
  test_5xx_retries_exhausted              PASSED
  test_4xx_fails_fast_no_retry            PASSED
  test_429_triggers_retry                 PASSED
  test_dedup_within_window_skips_second   PASSED
  test_dedup_different_keys_always_send   PASSED
  test_dedup_window_zero_disables_dedup   PASSED
  test_transient_exception_is_retried     PASSED
10 passed in 0.29s

Regression: arbiter/safety + arbiter/monitor + arbiter/notifiers
23 passed, 1 skipped — no regressions from dedup_key threading.
```

## Operator dry-test

```bash
set -a; source .env.production; set +a
python -m arbiter.notifiers.telegram
# expect: "Telegram dry-test OK — message delivered."
# and a Telegram message: "🧪 Arbiter Telegram dry-test ..."
```

Exit codes: 0 = OK, 1 = disabled or send failed, 2 = exception.

## Self-Check: PASSED
- 10/10 new tests green
- 23/24 existing safety+monitor+notifier tests green (1 skip is pre-existing)
- SafetySupervisor dedup_key calls use unambiguous scoped keys
- Disabled-mode is a true no-op (no network, no exceptions)
- CLI entry point works via `python -m arbiter.notifiers.telegram`

## Deferred
- Queue + async-dispatch: current `send()` is synchronous (awaits). For very high event frequency, a bounded queue + worker task would reduce callsite latency. Not needed today — SafetySupervisor already wraps all notifier.send in try/except so latency on bad Telegram doesn't block safety logic.
