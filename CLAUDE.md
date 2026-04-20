<!-- GSD:project-start source:PROJECT.md -->
## Project

**Arbiter Dashboard**

A cross-platform prediction market arbitrage system that detects price discrepancies across Kalshi and Polymarket, then executes trades to capture the spread. It includes a real-time WebSocket dashboard for monitoring prices, opportunities, positions, and execution. The system is built but untested against live APIs.

**Core Value:** Execute live arbitrage trades across both platforms without losing money to bugs, stale prices, or partial fills.

### Constraints

- **Capital**: Under $1K per platform initially -- system must handle small position sizes
- **Timeline**: ASAP -- get to live trades as fast as possible, even with manual monitoring
- **Risk tolerance**: Low -- cannot afford to lose capital to bugs. Safety > speed.
- **Platform APIs**: Must comply with rate limits and terms of service for both platforms
- **Auth credentials**: API keys stored in .env file, RSA keys in arbiter/keys/ (git-ignored)
<!-- GSD:project-end -->

<!-- GSD:stack-start source:codebase/STACK.md -->
## Technology Stack

## Languages
- TypeScript 5.4.0 - Frontend CLI pipeline and arbitrage execution logic (`src/`)
- Python 3.12 - Backend API server and core arbitrage system (`arbiter/`)
- JavaScript/HTML - Static dashboard frontend (`index.html`, HTML5)
- SQL - PostgreSQL database schema (`arbiter/sql/`)
- Shell - Build and deployment scripts (`scripts/`, Makefile)
## Runtime
- Node.js (ES2022 target via TypeScript compilation)
- Python 3.12 (slim Docker image)
- Docker 20+ (containerization)
- npm (JavaScript) - Version in `package.json`
- pip3 (Python) - Requirements in `requirements.txt`
- Lockfiles: `package-lock.json` (npm), no Python lockfile
## Frameworks
- aiohttp 3.9.0+ - Async HTTP server for API and dashboard (`arbiter/api.py`)
- asyncpg 0.29.0+ - Async PostgreSQL client for database operations
- Vitest 4.1.4 - TypeScript/JavaScript unit testing (`src/__tests__/`)
- pytest 5.0+ - Python test runner (configured in `conftest.py`)
- TypeScript Compiler (tsc) - Compiles TypeScript to JavaScript (`dist/`)
- tsx 4.7.0 - Direct TypeScript execution for CLI development
## Key Dependencies
- web3 6.0.0+ - Ethereum/Web3 interaction (Polymarket support)
- py-clob-client 0.25.0+ - Polymarket CLOB (Conditional Long/Short) client
- cryptography 41.0.0+ - Kalshi API signing and encryption
- redis[hiredis] 5.0.0+ - In-memory quote caching and fanout queues
- python-dotenv 1.0.0+ - Environment variable loading
- aiohttp - HTTP/WebSocket server framework
## Configuration
- `.env` file (required, template provided as `.env.template`)
- `tsconfig.json` - TypeScript compilation config (ES2022, CommonJS modules via Node16)
- `package.json` - Node.js dependencies and npm scripts
- `Dockerfile` - Docker image definition (Python 3.12 slim)
- `docker-compose.yml` - Multi-service orchestration (PostgreSQL, Redis, Arbiter API)
## Platform Requirements
- Node.js 18+ (for TypeScript compilation and tsx)
- Python 3.11+ (for backend)
- Docker Desktop (recommended for local services)
- PostgreSQL 14+ (via Docker)
- Redis 7+ (via Docker)
- Docker/Kubernetes deployment
- PostgreSQL 14+ (external or managed)
- Redis 7+ (external or managed)
- Python 3.12 runtime
## Supported Markets
- **Kalshi** - Prediction market with authentication (`py-clob-client` for CLOB orders)
- **Polymarket** - Ethereum-based AMM via CLOB (`web3` + `py-clob-client`)
## Key Versions
- Node.js: 18+ (ES2022 target)
- Python: 3.12
- TypeScript: 5.4.0
- PostgreSQL: 16 (Alpine in Docker)
- Redis: 7 (Alpine in Docker)
<!-- GSD:stack-end -->

<!-- GSD:conventions-start source:CONVENTIONS.md -->
## Conventions

Conventions not yet established. Will populate as patterns emerge during development.
<!-- GSD:conventions-end -->

<!-- GSD:architecture-start source:ARCHITECTURE.md -->
## Architecture

## Pattern Overview
- **Polyglot:** TypeScript CLI for dry-run pipeline, Python backend for live trading and dashboard API
- **Async-first:** All I/O is non-blocking (aiohttp, asyncpg, asyncio)
- **Event-driven:** Price updates trigger scanner, which broadcasts opportunities to subscribers
- **Fee-aware:** Cross-platform arbitrage math includes venue-specific fee structures (Kalshi quadratic, Polymarket market-specific)
- **Dual execution modes:** Dry-run simulation (TypeScript) and live trading (Python)
## Layers
- **Purpose:** Fetch live market prices from two platforms (Kalshi, Polymarket)
- **Location:** `arbiter/collectors/` (Python), `src/collectors/` (TypeScript)
- **Contains:** Platform-specific HTTP clients with retry logic and circuit breakers
- **Depends on:** External market APIs, Redis quote cache
- **Used by:** Price scanner, portfolio monitor
- `arbiter/collectors/kalshi.py` - Kalshi market data (authenticated CLOB queries)
- `arbiter/collectors/polymarket.py` - Polymarket via thegraph.com indexing
- `src/collectors/kalshi-client.ts` - TypeScript Kalshi client (CLI only)
- **Purpose:** Centralized quote cache with subscriptions for state changes
- **Location:** `arbiter/utils/price_store.py`
- **Contains:** In-memory and Redis-backed quote storage (30-second TTL)
- **Depends on:** Redis client
- **Used by:** Scanner, execution engine, portfolio monitor
- **Purpose:** Canonical market identification (link Kalshi market to Polymarket contract)
- **Location:** `arbiter/mapping/market_map.py`, `arbiter/config/settings.py:MARKET_MAP`
- **Contains:** Mapping status (candidate, review, confirmed), scoring algorithm
- **Depends on:** None (config only)
- **Used by:** Scanner (to identify cross-platform opportunities)
- **Purpose:** Detect arbitrage opportunities from matched prices
- **Location:** `arbiter/scanner/arbitrage.py`
- **Contains:**
- **Depends on:** Price store, market mapping, fee config
- **Used by:** Execution engine, portfolio monitor
- `ArbitrageScanner` - Main scanning loop
- `ArbitrageOpportunity` - Dataclass for opportunity representation
- Fee functions: `kalshi_order_fee()`, `polymarket_order_fee()`
- **Purpose:** Order submission, fill simulation, state tracking
- **Location:** `arbiter/execution/engine.py`
- **Contains:**
- **Depends on:** Platform APIs, balance monitor, price store
- **Used by:** Main orchestrator, portfolio monitor
- `ExecutionEngine` - Coordinates order lifecycle
- `Order` - Order state machine
- `ArbExecution` - Full arbitrage trade record (buy leg + sell leg)
- `ExecutionIncident` - Error/warning tracking
- **Purpose:** Track balances, portfolio health, readiness for live trading
- **Location:** `arbiter/monitor/balance.py`, `arbiter/portfolio/monitor.py`, `arbiter/readiness.py`
- **Contains:**
- **Depends on:** Collectors, execution engine
- **Used by:** Main orchestrator, API routes
- **Purpose:** Verify execution math correctness, reconcile runtime balances vs. recorded P&L
- **Location:** `arbiter/audit/math_auditor.py`, `arbiter/audit/pnl_reconciler.py`
- **Contains:**
- **Depends on:** Execution engine, balance monitor
- **Used by:** Main orchestrator (reconciliation loop)
- **Purpose:** WebSocket-driven dashboard for real-time operations
- **Location:** `arbiter/api.py`, `arbiter/web/` (HTML/CSS/JS)
- **Contains:**
- **Depends on:** All upstream layers (price store, scanner, execution, monitor, audit)
- **Used by:** Dashboard frontend, external integrations
- **Purpose:** Standalone dry-run arbitrage pipeline for testing
- **Location:** `src/cli.ts`, supporting modules
- **Contains:** Sequential pipeline (collect → match → detect → risk gate → execute → log)
- **Depends on:** Collector clients, matcher, detector, executor
- **Used by:** Development/testing only
## Data Flow
- **Transient:** Quote cache (Redis, 30s TTL)
- **Semi-persistent:** Current opportunities, open executions (in-memory during runtime)
- **Persistent:** Execution history, positions, incidents, market mappings (PostgreSQL)
- `PriceStore.subscribe()` → receives each price update
- `ArbitrageScanner.subscribe()` → receives each opportunity
- `ExecutionEngine.subscribe()` → receives each execution fill
- `ExecutionEngine.subscribe_incidents()` → receives errors/warnings
## Key Abstractions
- **Purpose:** Represents a fee-aware cross-platform price discrepancy
- **Location:** `arbiter/scanner/arbitrage.py`
- **Pattern:** Dataclass with computed properties (gross_edge, total_fees, net_edge_cents)
- **Fields:** 
- **Purpose:** Represent a single order or full arbitrage trade
- **Location:** `arbiter/execution/engine.py`
- **Pattern:** State machine with lifecycle (pending → submitted → filled → settled)
- **Order fields:** order_id, platform, market_id, side, price, quantity, status, fill_price, fill_qty
- **ArbExecution fields:** arb_id, opportunity, leg_yes (Order), leg_no (Order), realized_pnl
- **Purpose:** Point-in-time account balance
- **Location:** `arbiter/monitor/balance.py`
- **Fields:** platform, balance, timestamp, is_low (boolean)
- **Purpose:** Single market quote with metadata
- **Location:** `arbiter/utils/price_store.py`
- **Pattern:** Dataclass with to_dict() for API serialization
- **Fields:** platform, canonical_id, yes_price, no_price, yes_volume, no_volume, timestamp, fee_rate, mapping_status, mapping_score
## Entry Points
- **Location:** `arbiter/main.py:main()`
- **Invocation:** `python -m arbiter.main [--live] [--api-only]`
- **Responsibilities:**
- **Location:** `src/cli.ts:main()`
- **Invocation:** `npx tsx src/cli.ts` or `node dist/cli.js`
- **Responsibilities:**
- **Location:** `arbiter/api.py:ArbiterAPI.serve()`
- **Responsibilities:**
## Error Handling
- **Collector failures:** Circuit breaker (fail open after N errors) → fall back to cached prices
- **Order submission failures:** Log incident, don't retry (risk of accidental duplicate orders)
- **Fill monitoring:** Re-quote checks on slow fills, optional timeout → recovery workflow
- **Database connectivity:** Retry with exponential backoff (managed by asyncpg)
- **User errors:** Validation on market mappings, position confirmations
## Cross-Cutting Concerns
- Framework: Python `logging` module
- Configured in: `arbiter/utils/logger.py:setup_logging()`
- Levels: DEBUG, INFO, WARNING, ERROR, CRITICAL (configurable via `--log-level`)
- Trade logging: Custom `TradeLogger` class writes JSON to `logs/` directory
- Config loading: `arbiter/config/settings.py:load_config()` validates all required env vars
- Market mappings: `arbiter/mapping/market_map.py` status workflow (candidate → review → confirmed)
- Opportunity thresholds: `min_edge_cents`, `max_position_usd`, `persistence_scans`
- Dashboard: HMAC-SHA256 session tokens in `arbiter/api.py`
- Platform APIs: API keys + private keys from env vars
- Token expiry: 7 days, verified on each request
- Kalshi: Quadratic fee function `kalshi_order_fee()` with cent rounding
- Polymarket: Market-category-specific rates with fallback defaults
<!-- GSD:architecture-end -->

<!-- GSD:skills-start source:skills/ -->
## Project Skills

No project skills found. Add skills to any of: `.claude/skills/`, `.agents/skills/`, `.cursor/skills/`, or `.github/skills/` with a `SKILL.md` index file.
<!-- GSD:skills-end -->

<!-- GSD:workflow-start source:GSD defaults -->
## GSD Workflow Enforcement

Before using Edit, Write, or other file-changing tools, start work through a GSD command so planning artifacts and execution context stay in sync.

Use these entry points:
- `/gsd-quick` for small fixes, doc updates, and ad-hoc tasks
- `/gsd-debug` for investigation and bug fixing
- `/gsd-execute-phase` for planned phase work

Do not make direct repo edits outside a GSD workflow unless the user explicitly asks to bypass it.
<!-- GSD:workflow-end -->



<!-- GSD:profile-start -->
## Developer Profile

> Profile not yet configured. Run `/gsd-profile-user` to generate your developer profile.
> This section is managed by `generate-claude-profile` -- do not edit manually.
<!-- GSD:profile-end -->
