# OPENCLAW_RISK_REGISTER

Updated: 2026-04-21 02:43 PDT

| ID | Severity | Risk | Evidence | Mitigation / next step |
|---|---|---|---|---|
| R01 | low | Host default `python3` still does not represent the verified repo runtime | repo verification now passes via `.venv` / `ARBITER_PYTHON`, but bare host `python3` remains `3.9.6` | Keep launcher/bootstrap docs pointing at repo `.venv`; only treat host `python3` as informational |
| R02 | medium | Some historical docs may still lag the current Polymarket US / operator-settings flow | `GOLIVE.md`, `STATUS.md`, and `HANDOFF.md` are updated, but older planning docs remain historical | Continue treating `HANDOFF.md`, `STATUS.md`, and `GOLIVE.md` as canonical and audit remaining stale references opportunistically |
| R03 | medium | Repo-native GSD workflow cannot be followed literally here | `CLAUDE.md` mandates `/gsd-*`, but no command or entrypoint exists on host | Use `OPENCLAW_*` durable files as replacement control plane and document the mismatch |
| R04 | medium | Source TODOs remain on live-adjacent setup code | `scripts/setup/onboard_polymarket_us.py` still contains selector-confirmation TODOs | Audit script against real portal assumptions, fix or document manual fallback |
| R05 | low | TypeScript CLI still contains an unimplemented Kalshi client stub | `src/collectors/kalshi-client.ts` TODO for real RSA-auth API calls | Determine whether this path is production-relevant or legacy-only, then implement or explicitly de-scope |
