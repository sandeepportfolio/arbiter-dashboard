---
phase: 03-safety-layer
plan: 06
subsystem: mapping
tags: [market-mapping, resolution-criteria, jsonb, websocket, safe-06, postgres-migration]
requires:
  - arbiter.config.settings.MarketMappingRecord
  - arbiter.config.settings.update_market_mapping
  - arbiter.mapping.market_map.MarketMapping
  - arbiter.api.ArbiterAPI (_broadcast_json from plan 03-01)
  - arbiter.sql.init.sql base schema
provides:
  - arbiter.config.settings.MarketMappingRecord.resolution_criteria
  - arbiter.config.settings.MarketMappingRecord.resolution_match_status
  - update_market_mapping(resolution_criteria=, resolution_match_status=) kwargs
  - arbiter.mapping.market_map.MarketMapping.resolution_criteria_json
  - arbiter.mapping.market_map.MarketMapping.resolution_match_status
  - GET /api/market-mappings fields: resolution_criteria, resolution_match_status
  - POST /api/market-mappings/{id} body keys: resolution_criteria, resolution_match_status
  - WebSocket event type: mapping_state
  - SQL migration: ALTER TABLE market_mappings ADD COLUMN IF NOT EXISTS resolution_criteria JSONB, resolution_match_status VARCHAR(40)
affects:
  - plan 03-07 (UI) — consumes state.mappingUpdates + /api/market-mappings response for side-by-side comparison panel
  - arbiter.main.run_system — migration runner now executes safety_events.sql AND init.sql idempotently
  - arbiter.web.dashboard.js — WS handler tolerance chain extended with mapping_state branch (state-only mutation)
tech-stack:
  added: []
  patterns:
    - "Optional dataclass fields with explicit to_dict() emission so API consumers always .get() a key (never KeyError)"
    - "asyncpg $N::jsonb parameterized binding for JSONB INSERT/UPDATE paths (T-3-06-E SQL-injection mitigation)"
    - "API-layer enum validation (criteria_match) ahead of persistence to isolate the trust boundary (T-3-06-B)"
    - "Idempotent ALTER TABLE migration inside SQL_INIT + init.sql so startup applies new columns without breaking existing deployments"
    - "WebSocket state-mutation tolerance branch pattern (mapping_state) ahead of renderer implementation in later plan"
key-files:
  created: []
  modified:
    - arbiter/config/settings.py
    - arbiter/mapping/market_map.py
    - arbiter/api.py
    - arbiter/sql/init.sql
    - arbiter/main.py
    - arbiter/web/dashboard.js
    - arbiter/test_config_loading.py
    - arbiter/test_api_integration.py
decisions:
  - "Top-level resolution_match_status is a mirror of resolution_criteria.criteria_match when the caller supplies criteria; explicit resolution_match_status kwarg wins when passed (clearer API + lets operator fast-path a status change without restating the whole criteria dict)"
  - "MarketMapping.resolution_criteria_json is stored as a JSON string on the dataclass (not a dict) so the dataclass stays easy to compare/hash and maps 1:1 to the JSONB column; .to_dict() parses it safely with try/except to mitigate T-3-06-F malformed-JSON DoS"
  - "Enum validation (criteria_match ∈ {identical, similar, divergent, pending_operator_review}) lives in the API handler, not the settings helper — the settings helper is purely a persistence hook, and the API is where the auth-gated trust boundary lives"
  - "WebSocket mapping_state event is emitted only when resolution_criteria or resolution_match_status is part of the request (not on every mapping action) so the event carries semantic meaning and dashboards don't thrash on confirm/disable_auto_trade-only flows"
  - "init.sql is now re-runnable — existing CREATE INDEX statements were upgraded to CREATE INDEX IF NOT EXISTS so the startup migration runner can safely re-execute the whole file. Deviation Rule 3 fix: without this, the startup block would silently log warnings on every restart."
metrics:
  duration: "~9min"
  completed: 2026-04-17
requirements-completed: [SAFE-06]
---

# Phase 03 Plan 06: Market-mapping resolution_criteria schema + mapping_state WS event Summary

**Market-mapping data layer extended with a structured resolution-criteria payload, a denormalized match-status column, an idempotent Postgres JSONB migration, an `/api/market-mappings` POST passthrough with criteria_match enum validation, and a `mapping_state` WebSocket event — all shipped without a renderer (plan 03-07 owns the side-by-side comparison UI).**

## Performance

- **Duration:** ~9 min
- **Started:** 2026-04-17T01:04:43Z
- **Completed:** 2026-04-17T01:13:29Z
- **Tasks:** 2 (Task 0 + Task 1)
- **Files modified:** 8

## Accomplishments

- `MarketMappingRecord` gained optional `resolution_criteria: Optional[Dict[str, Any]]` and `resolution_match_status: str` (default `"pending_operator_review"`). Existing `MARKET_SEEDS` continue to load without modification, and `.to_dict()` now always emits both keys so downstream consumers can `.get()` safely (Pitfall 6 mitigation).
- `update_market_mapping(..., resolution_criteria=None, resolution_match_status=None)` extended with two new kwargs. When `resolution_criteria` includes `criteria_match`, the top-level `resolution_match_status` mirrors it automatically unless the caller passes an explicit value.
- `arbiter.mapping.market_map.MarketMapping` gained `resolution_criteria_json` (JSON string) + `resolution_match_status` (VARCHAR-40) fields. `.to_dict()` parses the JSON safely (try/except → None on failure, T-3-06-F mitigation) and emits both keys. `MarketMappingStore.upsert` persists them via a `$20::jsonb` parameterized cast (T-3-06-E SQL-injection mitigation).
- **Idempotent SQL migration** added to `arbiter/sql/init.sql` (`ALTER TABLE market_mappings ADD COLUMN IF NOT EXISTS resolution_criteria JSONB, ... resolution_match_status VARCHAR(40) DEFAULT 'pending_operator_review'`) and mirrored in `arbiter/mapping/market_map.py::SQL_INIT`. `arbiter/main.py` startup migration runner now applies both `safety_events.sql` and `init.sql` (re-runnable — every statement uses `IF NOT EXISTS`).
- **`/api/market-mappings` GET** exposes `resolution_criteria` + `resolution_match_status` on every row (defaults applied when unset so dashboards never see missing keys).
- **`/api/market-mappings/{canonical_id}` POST** now accepts `resolution_criteria` and `resolution_match_status` alongside the existing `action` body. API layer validates the `criteria_match` enum (T-3-06-B) — unknown values return HTTP 400.
- **WebSocket `mapping_state` event** fires via `_broadcast_json` whenever a mapping update carries resolution-criteria data. Payload shape: `{canonical_id, resolution_criteria, resolution_match_status, status, updated_at}`.
- **`arbiter/web/dashboard.js`** extended with a state-only tolerance branch (`message.type === "mapping_state"`) — no render, mutates `state.mappingUpdates[canonical_id]`. Plan 03-07 wires the comparison renderer on top.
- **Threat mitigations implemented in code:** T-3-06-B (enum validation), T-3-06-E (parameterized JSONB), T-3-06-F (safe JSON parse). T-3-06-C (XSS on operator rule text) explicitly deferred to plan 03-07 as a forward constraint.

## Task Commits

1. **Task 0: Red tests for resolution_criteria schema + mapping_state WS event** — `ff5f757` (test)
2. **Task 1: Schema + dataclass + API endpoint + WS event + SQL migration + dashboard tolerance branch** — `6ab4037` (feat)

## Files Created/Modified

- `arbiter/config/settings.py` — `MarketMappingRecord` gains `resolution_criteria` + `resolution_match_status`; `.to_dict()` always emits both; `update_market_mapping` accepts the two new kwargs.
- `arbiter/mapping/market_map.py` — `MarketMapping` dataclass gains the two new fields; `SQL_INIT` appends idempotent `ALTER TABLE`; `MarketMappingStore.upsert` persists both columns via parameterized `::jsonb` binding; `_row_to_mapping` normalizes criteria JSONB → string and tolerates missing columns.
- `arbiter/api.py` — `handle_market_mappings` GET backfills both keys; `handle_market_mapping_action` POST accepts + validates `criteria_match` enum and broadcasts the `mapping_state` event.
- `arbiter/sql/init.sql` — append `ALTER TABLE market_mappings ADD COLUMN IF NOT EXISTS ...`; upgrade existing `CREATE INDEX` statements to `CREATE INDEX IF NOT EXISTS` (re-runnable).
- `arbiter/main.py` — startup migration runner iterates `{safety_events.sql, init.sql}` so schema deltas land on restart.
- `arbiter/web/dashboard.js` — new `else if (message.type === "mapping_state")` branch; state-only mutation; no renderer (03-07).
- `arbiter/test_config_loading.py` — 5 new tests covering optional-field behavior, accepted-field round-trip, update_market_mapping kwarg, explicit-status kwarg precedence, criteria_match passthrough.
- `arbiter/test_api_integration.py` — 4 new in-process `TestServer` tests covering GET response shape, POST body passthrough, 400 on invalid `criteria_match`, and WebSocket `mapping_state` emission within 2s of an update.

## Resolution-criteria Schema Contract

```python
{
  "kalshi": {
    "source": str,          # URL to Kalshi rules page
    "rule": str,            # Operator-entered resolution rule
    "settlement_date": str, # ISO date
  },
  "polymarket": {
    "source": str,
    "rule": str,
    "settlement_date": str,
  },
  "criteria_match": Literal[
    "identical",               # Operator has verified rules + dates match
    "similar",                 # Rules align but small divergences (different dates, same event)
    "divergent",               # Rules diverge enough that auto-trade is unsafe
    "pending_operator_review", # Default — not yet reviewed
  ],
  "operator_note": str,       # Free-text, surfaced in the comparison UI (03-07)
}
```

**Field shape note:** The criteria dict is stored as JSONB in Postgres; the API accepts it verbatim (modulo `criteria_match` enum validation). Operator-entered free text (`rule`, `operator_note`) is NOT rendered in this plan — plan 03-07 MUST escape it via `textContent` or an equivalent DOMPurify path to close threat T-3-06-C.

## Pass-through Flow

```
POST /api/market-mappings/{canonical_id}
  └─▶ require_auth(request)
  └─▶ validate criteria_match ∈ allowed enum (400 on miss)
  └─▶ update_market_mapping(..., resolution_criteria=..., resolution_match_status=...)
        └─▶ MARKET_MAP[canonical_id]["resolution_criteria"] = payload
        └─▶ MARKET_MAP[canonical_id]["resolution_match_status"] = mirror or explicit
  └─▶ _broadcast_json({type: "mapping_state", payload: {...}})
        └─▶ WS clients receive mapping_state
        └─▶ dashboard.js: state.mappingUpdates[canonical_id] = payload
        └─▶ (plan 03-07: side-by-side comparison renderer reads state.mappingUpdates)
```

## Forward Constraint for Plan 03-07 (T-3-06-C)

**The dashboard renderer MUST escape operator-entered rule text before injecting it into the DOM.** Options:
- Use `element.textContent = rule` (recommended — simplest, no innerHTML surface)
- Use DOMPurify + `element.innerHTML = DOMPurify.sanitize(rule)` if Markdown rendering is needed

The data layer is XSS-neutral today because `dashboard.js` only stores the payload on `state.mappingUpdates` without rendering. The first render call in plan 03-07 must carry the escape forward.

## Backward-compat Guarantee

Existing `MARKET_SEEDS` entries (no `resolution_criteria` specified) load identically — `MarketMappingRecord` defaults the two new fields to `None` and `"pending_operator_review"`, and every API consumer uses `.get()` with defaults. The one-liner smoke test confirms:

```bash
python -c "from arbiter.config.settings import MarketMappingRecord; \
  r = MarketMappingRecord(canonical_id='X', description='t', status='candidate', \
    kalshi='K', polymarket='P'); \
  assert r.resolution_criteria is None; \
  assert r.resolution_match_status == 'pending_operator_review'; \
  assert r.to_dict()['resolution_criteria'] is None; print('OK')"
```
Exits 0 after this plan.

## Decisions Made

- **Top-level `resolution_match_status` mirror:** API callers can mutate the criteria dict in full OR call out a status change in isolation via the explicit kwarg. This lets plan 03-07's UI offer a "mark as identical" button without requiring the operator to re-enter the full rules dict. Explicit kwarg wins over criteria-embedded `criteria_match`.
- **JSON-string storage on `MarketMapping` dataclass:** Keeps the dataclass shape stable (strings compare cleanly) and maps 1:1 to the JSONB column. `.to_dict()` parses the string with a try/except fallback to `None` so T-3-06-F malformed-JSON cases cannot crash the serializer.
- **Enum validation at the API boundary, not in `update_market_mapping`:** The settings helper is a persistence hook used by several callers; the API handler is the trust boundary where operator input arrives. Validation at the handler fails fast with HTTP 400; the helper trusts its callers.
- **WS event only when criteria data is present:** `mapping_state` fires only when the update touches resolution fields. Confirm/review/enable_auto_trade actions without criteria changes do NOT emit the event, keeping WS traffic semantic.
- **Re-runnable `init.sql`:** Upgrading `CREATE INDEX` → `CREATE INDEX IF NOT EXISTS` lets the startup runner safely re-execute init.sql. Without this, every restart logs noise (the ALTER block works regardless because it uses `IF NOT EXISTS` already, but re-running the file needs every statement to be idempotent).

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 3 - Blocking] Make `CREATE INDEX` statements in init.sql idempotent**
- **Found during:** Task 1 (SQL migration)
- **Issue:** Plan directs that `arbiter/main.py` re-run `init.sql` at startup to apply the new `ALTER TABLE`. But the existing `CREATE INDEX` statements in init.sql (lines 52-56) are not idempotent — every restart would raise `DuplicateTable`-style errors and the main.py try/except would log warnings indefinitely. This is cosmetic but noisy and could mask real migration failures.
- **Fix:** Upgraded `CREATE INDEX idx_trades_canonical` + 4 siblings to `CREATE INDEX IF NOT EXISTS idx_...`. Now `init.sql` is fully re-runnable.
- **Files modified:** `arbiter/sql/init.sql`
- **Verification:** Grep confirms 5 × `CREATE INDEX IF NOT EXISTS`; the SAFE-06 ALTER block (which was already idempotent per plan) is untouched.
- **Committed in:** `6ab4037` (Task 1 commit)

---

**Total deviations:** 1 auto-fixed (Rule 3 - blocking).
**Impact on plan:** No scope creep. The change was necessary to make the main.py `init.sql`-on-startup pattern (plan specification, step 5 of Task 1) work without generating constant warning noise.

## Issues Encountered

- **Pre-existing test `test_api_and_dashboard_contracts` fails on Windows** (dropped-socket timeout + Windows `add_signal_handler` NotImplementedError). Verified pre-existing by `git stash` of my changes and rerunning — same failure. Already documented in `.planning/phases/03-safety-layer/deferred-items.md` from plan 03-04. Out of scope per the SCOPE BOUNDARY rule.

## Tests Moved from SKIP/FAIL to PASS

| Test | File | Verifies |
|------|------|----------|
| `test_resolution_criteria_optional_when_missing` | `arbiter/test_config_loading.py` | Record default state (None / "pending_operator_review") serializes correctly |
| `test_resolution_criteria_accepted_when_present` | `arbiter/test_config_loading.py` | Full criteria dict round-trips through `.to_dict()` unchanged |
| `test_update_market_mapping_accepts_resolution_criteria` | `arbiter/test_config_loading.py` | `update_market_mapping(resolution_criteria=...)` kwarg persists + mirrors criteria_match |
| `test_update_market_mapping_rejects_invalid_criteria_match` | `arbiter/test_config_loading.py` | Helper accepts valid enum values (helper does not validate; API does) |
| `test_update_market_mapping_explicit_resolution_match_status` | `arbiter/test_config_loading.py` | Explicit status kwarg wins over criteria_match mirror |
| `test_market_mappings_returns_resolution_criteria` | `arbiter/test_api_integration.py` | GET /api/market-mappings exposes both keys on every row |
| `test_market_mapping_update_accepts_criteria` | `arbiter/test_api_integration.py` | POST body accepts criteria + returns persisted payload |
| `test_market_mapping_update_rejects_invalid_criteria_match` | `arbiter/test_api_integration.py` | 400 on unknown criteria_match (T-3-06-B) |
| `test_mapping_state_ws_event_fires_on_update` | `arbiter/test_api_integration.py` | WebSocket mapping_state event lands within 2s of POST |

## SAFE-06 Observable Truths — all met

- [x] MARKET_MAP schema accepts an optional `resolution_criteria` dict (verified by `test_resolution_criteria_accepted_when_present`).
- [x] Existing MARKET_MAP entries without resolution_criteria load without KeyError (verified by `test_resolution_criteria_optional_when_missing` + the backward-compat one-liner).
- [x] `MarketMapping` dataclass exposes `resolution_criteria_json` + `resolution_match_status`; `.to_dict()` serializes them under `resolution_criteria` + `resolution_match_status` keys (verified by grep counts and implementation inspection).
- [x] GET /api/market-mappings returns resolution_criteria on every mapping response (verified by `test_market_mappings_returns_resolution_criteria`).
- [x] POST /api/market-mappings/{id} accepts resolution_criteria and persists via `update_market_mapping(resolution_criteria=...)` (verified by `test_market_mapping_update_accepts_criteria`).
- [x] New WebSocket event `mapping_state` fires on resolution criteria / match status changes (verified by `test_mapping_state_ws_event_fires_on_update`).
- [x] `ALTER TABLE market_mappings ADD COLUMN IF NOT EXISTS resolution_criteria JSONB + resolution_match_status VARCHAR(40)` migration runs idempotently at startup (verified by inspection + `main.py` loop over `{safety_events.sql, init.sql}`).
- [x] Existing dashboard.js mapping render still works; new tolerance branch mutates state without throwing (JS syntax validated via `node --check`; vitest UI view-model test suite passes; `renderMappings` still referenced).

## Authentication Gates

**None encountered.** All new API tests use in-process `aiohttp.test_utils.TestServer` with a monkeypatched `UI_ALLOWED_USERS` fixture — no live Telegram bot, no live Postgres, no OAuth.

## Threat Flags

No new attack surface beyond the plan's `<threat_model>`. Mitigations applied in this plan:

| Threat | Component | Mitigation |
|--------|-----------|------------|
| T-3-06-A | Operator-injected malicious mapping | `require_auth(request)` guards POST; criteria is operator-provided data — trust boundary is the authenticated operator (accepted in threat register) |
| T-3-06-B | Arbitrary `criteria_match` string | API handler rejects any value outside `{identical, similar, divergent, pending_operator_review}` with 400 |
| T-3-06-C | XSS on operator rule text | **DEFERRED to plan 03-07** — this plan does not render the field; the forward constraint is documented in this SUMMARY + the plan 03-07 checklist |
| T-3-06-D | Info disclosure on WS fanout | Accepted (resolution rules are public market info; operator notes are low-risk for small-capital phase) |
| T-3-06-E | SQL injection via JSONB | All INSERT/UPDATE paths use `$N::jsonb` parameterized binding; no string-interpolation |
| T-3-06-F | Malformed JSON crashes `.to_dict()` | `json.loads` wrapped in try/except → None; API-layer input validation rejects non-dict payloads |

## Next Phase Readiness

- **Plan 03-07** can now build the side-by-side comparison UI directly against `state.mappingUpdates` (WS stream) + `/api/market-mappings` (bootstrap response). Both expose the same `resolution_criteria` / `resolution_match_status` shape.
- **Plan 03-07 security constraint (carry-forward):** MUST escape operator-entered rule text via `textContent` or DOMPurify before DOM injection (T-3-06-C).
- **Database operators:** `ALTER TABLE market_mappings` runs on next `arbiter.main` startup with Postgres configured. No manual migration needed.

## Self-Check: PASSED

- **Files modified (8):** all show `git diff` against the base commit and each is referenced in `key-files.modified`.
- **Commits (2):** `ff5f757` (test) + `6ab4037` (feat) both present in `git log --oneline`.
- **Test suite:** `pytest arbiter/test_config_loading.py arbiter/test_api_integration.py -k "resolution_criteria or mapping_state or market_mapping_update"` → 7 passed, 8 deselected, 0 failed.
- **Regression suite:** `pytest arbiter/safety/ arbiter/test_api_safety.py arbiter/test_api_auth.py arbiter/test_config_loading.py` → 39 passed, 1 skipped, 0 failed.
- **JS syntax:** `node --check arbiter/web/dashboard.js` → exit 0.
- **Vitest UI view-model:** 3/3 pass.
- **Backward-compat one-liner:** exits 0.
- **Grep counts:** all acceptance criteria satisfied (`resolution_criteria` ≥ 3 in settings.py, `resolution_match_status` ≥ 2 in settings.py, `resolution_criteria_json` ≥ 1 in market_map.py, `resolution_match_status` ≥ 1 in market_map.py, ALTER COLUMN × 2 in init.sql, `mapping_state` ≥ 1 in api.py, `message.type === "mapping_state"` = 1 in dashboard.js, `renderMappings` still present).

---
*Phase: 03-safety-layer*
*Plan: 06*
*Completed: 2026-04-17*
