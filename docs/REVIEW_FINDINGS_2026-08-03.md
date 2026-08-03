# Code-Review Findings — 2026-08-03 adversarial sweep (32 confirmed)

Fixed same-day: batch-verdict index misalignment (verifier), quarantine-release
party-identity gate, MitigationEngine cap+audit bypass (CRITICAL), P&L unwind
double-count, phantom-sweep unconfigured-venue coverage, cap flicker debounce.

## Remaining open findings (verified real — address in priority order)

1. **[major]** Verified against current code. scripts/llm_verifier_service.py:445 uses plain single-threaded HTTPServer bound to 0.0.0.0 with no auth; VerifyHandler sets no timeout attribute (socketserver default None), so a connection that never sends a complete request blocks rfile.readline() (or rfile.read(content_length) at line 339 for a short body) indefinitely, wedging /verify, /verify_batch, and /health 

2. **[minor]** Confirmed in current code: scripts/llm_verifier_service.py lines 120-124 build the persistent cache key as `v2|{roster}|` + '|'.join(sorted([a,b])) with no hashing/escaping, so pairs ('a|b','c') and ('a','b|c') collide on `v2|roster|a|b|c`. Both /verify (lines 236-238) and /verify_batch (lines 276-278) consult _PERSISTENT before the collision-safe frozenset in-memory cache, so a verdict cached und

3. **[major]** Confirmed at HEAD (8c34a05). update_status (arbiter/mapping/market_map.py:685-703) is still get() -> mutate -> upsert(); upsert (lines 606-683) rewrites every column (allow_auto_trade, forecastex conids, notes) from the moments-earlier read. The codebase itself documents this exact hazard in set_forecastex_conid's docstring (lines 578-584) and wrote disable_auto_trade/mark_forecastex_unavailable a

4. **[major]** Confirmed in current code: scripts/llm_verifier_service.py:166 uses _ANSWER_RE.findall(text)[0] with r"\b(YES|NO|MAYBE)\b" (IGNORECASE), returning the first YES/NO/MAYBE token anywhere in the reviewer's stdout rather than the verdict line. No commit in 6d565f0..HEAD fixed it (only 4ca6889 touched the file, parse unchanged). The narrated-response scenario is realistic: the prompt contradicts itself

5. **[major]** CONFIRMED in current HEAD. scripts/llm_verifier_service.py:445 still binds ("0.0.0.0", args.port) and VerifyHandler.do_POST (line 332) performs zero authentication — it checks only self.path before parsing JSON and invoking the Claude CLI. No commit in 6d565f0..HEAD changed this (only 4ca6889 touches the file). Live state corroborates: lsof shows PID 12880 listening on TCP *:8079, the deployed cop

6. **[minor]** Confirmed in current code. Eligibility (auto_validator.py:657-659) selects held rows via review_note+notes — a deliberate contract from 52b2c82, mirrored at all four promotion gates — but the release write (780-792) strips [no-auto-promote] only from review_note, and store.update_status (market_map.py:685-703) has no notes parameter and never modifies notes. If the marker lives in notes, the relea

7. **[major]** VERIFIED in current HEAD — every link of the claimed chain is present and nothing later in the range mitigates it.  1. Raw interpolation confirmed. `/Users/rentamac/Documents/arbiter/scripts/llm_verifier_service.py:245-252` builds `f"Q1 (Kalshi): {kalshi_q}\nQ2 (Polymarket): {poly_q}\n\nDo these two markets resolve..."` and line 300 concatenates it directly after `_SYSTEM_PROMPT`; the batch path (

8. **[major]** Verified against current HEAD (392223e is the last commit touching stranded_reconciler.py; no later fix). Line 681 builds covered={'kalshi','polymarket'}-failed_platforms unconditionally, and failed_platforms only records fetches that RAISED (lines 401-409). _fetch_kalshi_positions (846-847) and _fetch_polymarket_us_positions (932-935) return [] silently when credentials/config are absent, so an u

9. **[major]** Confirmed against current HEAD (8c34a05; realized_pnl_split was introduced there and never fixed). store.py:726-733 computes SUM(realized_pnl + unwind_pnl) in both buckets, but for every engine-recorded unwind the persisted realized_pnl ALREADY includes unwind_pnl: engine.py:2344-2346 adds the unwind amount to both execution.unwind_pnl and execution.realized_pnl, and record_arb (store.py:818-856, 

10. **[major]** Verified against current HEAD of arbiter/recovery/stranded_reconciler.py. (1) The prune loop (lines 439-450) deletes a tracked key and resets all three cap structures (_cum_close_submitted, _max_observed_qty, _cap_incidents_emitted) whenever the key is absent from `observed` and the platform's fetch did not raise — there is no absence debounce. A transiently-omitted position (200 response missing 

11. **[critical]** Verified against current HEAD. (1) Routing: stranded_reconciler.py:609-615 sends auto-close through _execute_decision whenever self._mitigation_engine is not None and pos.mitigation_decision is set; _maybe_auto_close is explicitly a fallback for tests/dev. (2) Prod always has the engine: main.py (~1787) passes the real PriceStore into reconciler_from_env, which never supplies mitigation_engine, so

12. **[major]** Verified in current code. The phantom sweep's age gate (stranded_reconciler.py:660-668) passes PHANTOM_MIN_AGE_S=86400 to list_stuck_arbs, whose SQL (stuck_trade_recovery.py:122) filters on NOW()-created_at — row creation age, not last fill/update time. _sweep_phantom_records (lines 671-713) has no recency guard on updated_at/terminal_at/fill time; it closes any >24h-old non-terminal arb whose fil

13. **[major]** Confirmed in current code. stranded_reconciler.py _maybe_auto_close increments _cum_close_submitted and calls _persist_close_attempt only on the success path (lines 1899-1902); the except branch (1925-1934) marks the attempt but freezes the cap counter and writes no execution_orders audit row. The retry gate (lines 602-608) explicitly re-allows a position that failed "e.g. adapter timeout" after t

14. **[major]** Confirmed in current code (working tree == HEAD, no later fix in range). Detection at arbiter/mapping/auto_validator.py:657-659 checks the marker in review_note + notes, but the release path (lines 780-792) strips "[no-auto-promote]" only from `note` (= review_note) and calls store.update_status, which per market_map.py:685-703 writes only status/review_note/allow_auto_trade — the notes column is 

15. **[major]** CONFIRMED in current code. stranded_reconciler.py lines 699-703 build the phantom sweep's filled-leg list with `float(leg.get("fill_qty") or 0) > 0` and no ForecastEx fallback; no commit after 392223e touches the file. store.py lines 271-275 + its order_exposure CTE (lines 294-308) document that FX pre-fix fills persist as status='filled'/fill_qty=0/quantity>0 while the broker HOLDS the position, 

16. **[major]** Confirmed in current HEAD code. (1) _close_phantom_record (stranded_reconciler.py:724-743) flips status to 'closed' while leaving realized_pnl/unwind_pnl untouched. (2) realized_pnl_split (store.py:726-733, added in HEAD commit 8c34a05) counts status='closed' rows' realized_pnl+unwind_pnl in realized_terminal. (3) /api/pnl (api.py:3348-3354) publishes realized_terminal as the net_trading_pnl headl

17. **[major]** CONFIRMED at current HEAD (8c34a05 — the commit that introduced realized_pnl_split; nothing later touches it). (1) 'cancelled' is a phantom: exhaustive enumeration of execution_arbs.status writers (store.py stubs→'pending'; record_arb←engine statuses submitted/filled/failed/recovering/simulated; store.py:575 'failed'; store.py:433 and stranded_reconciler.py:729 'closed'; stuck_trade_recovery.py:23

18. **[minor]** Confirmed against current HEAD. In sweep_quarantine_releases (arbiter/mapping/auto_validator.py) only store.get is wrapped (lines 651-654); all four update_status writes — _reset (667-672), duplicate-guard strip (694-697), streak increment (768-771), release (789-792) — are unwrapped. store.update_status (market_map.py:685-703) is get()+upsert() over raw asyncpg with no internal catching, so trans

19. **[minor]** The code-level mechanism is confirmed: sweep_quarantine_releases (auto_validator.py:627-630, 756) enforces only verdict=="YES" from llm_verifier.verify, which is dual-reviewer solely when the http backend routes to the sidecar (scripts/llm_verifier_service.py); with LLM_VERIFIER_HTTP_URL/BACKEND unset, _detect_backend (llm_verifier.py:462, evaluated at import) silently falls back to a single cli/a

20. **[major]** Confirmed at current HEAD. sweep_quarantine_releases (auto_validator.py:652) reads the mapping, then awaits llm_verify with up to 130s HTTP timeout (llm_verifier.py:322; CLI 120s, secondary 180s), then writes via store.update_status at lines 667/694/768/789 passing the STALE mapping.status and a review_note derived from the stale note. update_status (market_map.py:685) is get→mutate→full-row-upser

21. **[major]** Verified against current HEAD. (1) _persist_close_attempt (arbiter/recovery/stranded_reconciler.py:1144-1166, added in 77e09cb) creates the STRAND- arb via record_arb_stub, which inserts execution_arbs with status='pending' (arbiter/execution/store.py:763), then upserts exactly one order leg; nothing ever transitions the stub to a terminal status. (2) list_half_recorded_arbs Mode 1 (store.py:354-3

22. **[minor]** Confirmed against current HEAD (8c34a05). auto_promote_validated processes candidate+review rows (run_full_cycle line 1149-1151), and its duplicate-canonical (lines 899-904) and party-conflict (lines 956-963) quarantines write the "[no-auto-promote]" marker while preserving mapping.status — so a candidate row stays status=candidate with the marker. run_full_cycle line 1146 passes only review_resul

23. **[major]** Verified against current HEAD. llm_verifier.py:_remember (lines 148-153) caches YES/NO with expires_at=None (never expires); only MAYBE gets a TTL. The sidecar (scripts/llm_verifier_service.py:120-124, 236-238) additionally persists verdicts to disk keyed only by version|roster|sorted question strings with no TTL, so verdicts survive restarts. sweep_quarantine_releases (auto_validator.py:736-749) 

24. **[major]** Every link of the chain verified in current HEAD (8c34a05). (1) sweep_quarantine_releases (auto_validator.py:700-731) accepts authenticated price-store liveness for markets public checks cannot see (design intent of ab3d6ce, whose commit message confirms PM-US markets show only 1 live venue to public checks). (2) auto_promote_validated's gate at line 847 uses vr.live_platform_count, computed solel

25. **[major]** Verified against current HEAD. Line 681 computes covered = {"kalshi","polymarket"} - failed_platforms, and the docstring's own invariant ("covered only if fetch succeeded AND truth source is configured") is implemented only for forecastex (account_id guard at 683-688). _fetch_kalshi_positions (846-847) and _fetch_polymarket_us_positions (932-935) return [] without raising when creds/config are abs

26. **[minor]** Confirmed in current HEAD of arbiter/web/ops.html. PageIncidents keeps one `fetched` state (line 4954) that is never cleared on tab switch; safeJson (line 378) returns null on 401/network failure and load() deliberately leaves `fetched` untouched on null (lines 4975-4979), setting only an amber loadErr note. Render at line 5012 uses `fetched` whenever it is an array, and line 5013 re-filters clien

27. **[major]** Verified against current HEAD (8c34a05, last commit in range). (1) market_owner_counts (market_map.py:1321-1358) is confirmed-only. (2) Both consumers snapshot it once before their loops and never update it: sweep pre-check at auto_validator.py:640-648/685-698, promote guard at 830-838/888-892; the promote body (978-984) sets CONFIRMED+allow_auto_trade=True without folding the new owner into the d

28. **[major]** Confirmed against current HEAD. The YES handler (api.py:2188-2274) now calls mapping_store.set_forecastex_conid(canonical_id, new_conid, side="yes") at line 2245 — a pure single-column SQL UPDATE (mapping/market_map.py:575-604) that never touches the in-process MARKET_MAP — and the handler contains no MARKET_MAP patch before returning 200. At base commit 6d565f0 the handler used mapping_store.upse

29. **[major]** Verified against current code at /Users/rentamac/Documents/arbiter/arbiter/api.py (no later commit in 6d565f0..HEAD touches the throttle; the only later api.py commits are 60766d9, 392223e, 887d46a, 8c34a05, none of which alter lines 212-262).  Code confirms the mechanism exactly as claimed: - `_login_throttled` (line 234) prunes `_login_failures_global` to the last 300s and returns True unconditi

30. **[minor]** Confirmed against current HEAD (8c34a05, working tree clean for api.py). In handle_debug_memory, line 4714 computes `store` using the correct attribute names (ArbiterAPI sets self.store at api.py:292; the scanner sets self.store at scanner/arbitrage.py:238) but the variable is never used again in the function. Lines 4715-4717 instead consult `self.scanner.price_store` / `self.price_store`, and nei
