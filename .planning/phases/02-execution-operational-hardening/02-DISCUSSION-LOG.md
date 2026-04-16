# Phase 2: Execution & Operational Hardening - Discussion Log

> **Audit trail only.** Do not use as input to planning, research, or execution agents.
> Decisions are captured in CONTEXT.md — this log preserves the alternatives considered.

**Date:** 2026-04-16
**Phase:** 02-execution-operational-hardening
**Areas discussed:** Adapter extraction pattern, State persistence strategy, Retry and resilience approach, Logging and observability

---

## Adapter Extraction Pattern

### Q1: How should per-platform adapters be structured?

| Option | Description | Selected |
|--------|-------------|----------|
| Protocol (ABC) with platform classes | Abstract PlatformAdapter with place_order, cancel_order, get_fills. KalshiAdapter and PolymarketAdapter implement it. | |
| Thin wrappers keeping engine as coordinator | Platform HTTP logic moves to adapters, engine keeps lifecycle. | |
| You decide | Claude picks based on codebase shape. | ✓ |

**User's choice:** You decide

### Q2: Should FOK enforcement live in the adapters or the engine?

| Option | Description | Selected |
|--------|-------------|----------|
| Adapters enforce FOK per platform | Each adapter knows its platform's FOK mechanism. | |
| Engine validates, adapters submit | Belt-and-suspenders — both layers check. | |
| You decide | Claude picks based on platform APIs. | ✓ |

**User's choice:** You decide

---

## State Persistence Strategy

### Q3: How should execution state be persisted to PostgreSQL?

| Option | Description | Selected |
|--------|-------------|----------|
| Write every state transition | Full audit trail, survives crash at any point. | |
| Write on terminal states only | Simpler, fewer writes, loses in-flight orders on crash. | |
| You decide | Claude optimizes for "cannot afford to lose capital to bugs." | ✓ |

**User's choice:** You decide

### Q4: Should the system recover in-flight orders on restart, or start clean?

| Option | Description | Selected |
|--------|-------------|----------|
| Recover and reconcile | Query platforms for open orders, reconcile with DB, resume monitoring. | |
| Start clean, alert on orphans | Flag non-terminal DB orders as orphaned, operator checks manually. | |
| You decide | Claude picks based on safety > speed. | ✓ |

**User's choice:** You decide

---

## Retry and Resilience Approach

### Q5: What should happen to the existing CircuitBreaker in retry.py?

| Option | Description | Selected |
|--------|-------------|----------|
| Keep CircuitBreaker, add tenacity for retries | Layered — tenacity for transients, CircuitBreaker for sustained. | |
| Replace everything with tenacity | One retry library, simpler. | |
| You decide | Claude picks based on OPS-03 and existing code. | ✓ |

**User's choice:** You decide

---

## Logging and Observability

### Q6: How should the structlog migration work?

| Option | Description | Selected |
|--------|-------------|----------|
| Full structlog migration | Replace all stdlib logging with structlog. | |
| structlog wrapper over stdlib | Processor chain on top of stdlib logging. | |
| You decide | Claude picks based on OPS-01 and migration effort. | ✓ |

**User's choice:** You decide

---

## Claude's Discretion

User delegated all implementation pattern choices (adapter shape, FOK enforcement layer, persistence strategy, restart recovery, retry/CircuitBreaker layering, structlog migration style) to Claude. Decisions captured as D-14 through D-19 in CONTEXT.md, all bounded by:
- Locked requirements from REQUIREMENTS.md (EXEC-01..05, OPS-01..04)
- Project constraints from CLAUDE.md: safety > speed, cannot afford to lose capital
- Phase 1 decisions that cannot be disturbed (Kalshi dollar strings, Polymarket auth, PredictIt already removed)

## Deferred Ideas

- WebSocket price feeds — v2 (OPT-01)
- Automated kill switch triggers — v2 (OPT-04)
- Telegram kill/alert integration — Safety & Risk phase, not this one
- Settlement divergence monitoring — v2 (MON-01)
