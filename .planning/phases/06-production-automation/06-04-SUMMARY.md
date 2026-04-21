---
phase: 06-production-automation
plan: 04
status: complete
key_files:
  created:
    - arbiter/test_api_metrics.py
  modified:
    - arbiter/api.py
    - arbiter/main.py
tests_added: 8
tests_passing: 8
---

# Plan 06-04 — /api/metrics + /api/readiness — SUMMARY

## What was built

`GET /api/metrics` returns Prometheus text-exposition format (version 0.0.4).
The readiness endpoint already returns structured go/no-go shape; no changes needed there beyond confirming it covers the preflight surface.

### Metrics exposed
| Metric | Type | Labels | Source |
|---|---|---|---|
| `arbiter_build_info` | gauge | release, env | env vars |
| `arbiter_scanner_scans_total` | counter | — | scanner.stats.scan_count |
| `arbiter_scanner_active_opportunities` | gauge | — | scanner.stats |
| `arbiter_scanner_best_edge_cents` | gauge | — | scanner.stats |
| `arbiter_scanner_last_scan_ms` | gauge | — | scanner.stats |
| `arbiter_executions_total` | counter | status={live,simulated,manual} | engine.stats |
| `arbiter_incidents_total` | counter | — | engine.stats |
| `arbiter_recoveries_total` | counter | — | engine.stats |
| `arbiter_aborts_total` | counter | — | engine.stats |
| `arbiter_pnl_total` | gauge | — | engine.stats |
| `arbiter_kill_switch_armed` | gauge | — | SafetySupervisor.is_armed |
| `arbiter_circuit_state` | gauge | platform={kalshi,polymarket} | collectors[p].circuit |
| `arbiter_rate_limiter_tokens` | gauge | platform | collectors[p].rate_limiter |
| `arbiter_rate_limiter_penalty_seconds` | gauge | platform | collectors[p].rate_limiter |
| `arbiter_auto_executor_considered` | counter | — | AutoExecutor.stats (Plan 06-01) |
| `arbiter_auto_executor_executed` | counter | — | AutoExecutor.stats |
| `arbiter_auto_executor_skipped` | counter | reason={disabled,armed,requires_manual,not_allowed,duplicate,over_cap,bootstrap_full} | AutoExecutor.stats |

## Scrape config

```yaml
- job_name: arbiter
  static_configs:
    - targets: ['arbiter-api-prod:8080']
  metrics_path: /api/metrics
  scheme: http
  scrape_interval: 15s
```

## Tests (8/8 green, 0.46s)

```
test_metrics_response_is_prometheus_text          PASSED
test_metrics_includes_scanner_stats               PASSED
test_metrics_includes_execution_counters          PASSED
test_metrics_includes_kill_switch_state           PASSED
test_metrics_includes_per_platform_circuit_and_limiter  PASSED
test_metrics_auto_executor_stats_when_attached    PASSED
test_metrics_auto_executor_absent_is_safe         PASSED
test_metrics_circuit_open_maps_to_2               PASSED
```

## Self-Check: PASSED
- 8/8 new tests green
- AutoExecutor attachment via `api.auto_executor = auto_executor` wired in arbiter.main
- Graceful when AutoExecutor absent (api-only mode): metric lines simply omitted
- Existing /api/readiness endpoint already returns structured blocking_reasons + checks; deferred UI polish to Plan 06-06

## Deferred
- /api/readiness additional fields for preflight 15-item parity (existing structure already covers blocking path; further enrichment left to Plan 06-06 which reuses this for the operator runbook).
- Grafana dashboard JSON — export left to the operator once they pick a Grafana flavor.
