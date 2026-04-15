# ARBITER — Developer Makefile

.PHONY: help test test-watch lint clean migrate migrate-plan db-reset db-shell \
        start-dev stop docker-build docker-push health

# ── Defaults ────────────────────────────────────────────────────────────
PYTEST      := python3 -m pytest
PYLINT      := python3 -m pylint
COMPOSE     := docker compose
EXPORTDIR   := ./exports

help: ## Show this help
	@grep -E '^[^#]+:.*##' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

# ── Local Dev ──────────────────────────────────────────────────────────
test: ## Run all tests
	$(PYTEST) -x -q

test-watch: ## Run tests with auto-reload
	$(PYTEST) -x -q --reload --reload-dir=arbiter/

test-verbose: ## Run tests verbose
	$(PYTEST) -v

test-cover: ## Run with coverage
	$(PYTEST) --cov=arbiter --cov-report=term-missing --cov-report=html

lint: ## Run linter
	$(PYLINT) arbiter/ --disable=C,R

clean: ## Remove __pycache__, .pytest_cache, coverage reports
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name .pytest_cache -exec rm -rf {} + 2>/dev/null || true
	rm -rf htmlcov/ .coverage

# ── Database ───────────────────────────────────────────────────────────
migrate: ## Apply pending migrations
	python3 scripts/migrate.py --apply

migrate-plan: ## Show pending migrations (dry run)
	python3 scripts/migrate.py --plan

db-verify: ## Verify database connectivity and schema
	python3 scripts/migrate.py --verify

db-reset: ## Reset database (WARNING: destroys data)
	docker $(COMPOSE) exec postgres psql -U arbiter -c "DROP SCHEMA public CASCADE; CREATE SCHEMA public;"
	python3 scripts/migrate.py --apply

db-shell: ## Open psql shell
	docker $(COMPOSE) exec postgres psql -U $(shell grep PG_USER .env 2>/dev/null | cut -d= -f2 || echo arbiter) $(shell grep PG_DATABASE .env 2>/dev/null | cut -d= -f2 || echo arbiter_dev)

# ── Docker ─────────────────────────────────────────────────────────────
start-dev: ## Start all services via docker compose
	$(COMPOSE) up -d
	@echo "Waiting for postgres to be ready..."
	@docker $(COMPOSE) exec -T postgres pg_isready -U arbiter && echo "DB ready" || true
	@echo "Running migrations..."
	docker $(COMPOSE) exec -T arbiter python scripts/migrate.py --apply || true
	@echo "Arbiter running at http://localhost:8090"
	@echo "Dashboard: http://localhost:8090/ops"

start-dev-bg: start-dev ## Alias

stop: ## Stop all services
	$(COMPOSE) down

docker-build: ## Build Docker image
	docker build -t arbiter:latest .

docker-push: docker-build ## Build and push to GHCR
	docker tag arbiter:latest ghcr.io/sandeepportfolio/arbiter:latest
	docker push ghcr.io/sandeepportfolio/arbiter:latest

health: ## Check health of all services
	@curl -sf http://localhost:8090/api/health && echo " API: OK" || echo " API: FAIL"
	@docker $(COMPOSE) exec -T postgres pg_isready -U arbiter && echo " Postgres: OK" || echo " Postgres: FAIL"
	@docker $(COMPOSE) exec -T redis redis-cli ping | grep PONG && echo " Redis: OK" || echo " Redis: FAIL"

# ── Quick smoke ────────────────────────────────────────────────────────
smoke: test ## Run quick smoke (same as test right now)

# ── Secrets ─────────────────────────────────────────────────────────────
gen-secret: ## Generate a random UI session secret
	@echo "UI_SESSION_SECRET=$$(openssl rand -hex 32)"

# ── Docs ───────────────────────────────────────────────────────────────
docs: ## Build and serve docs locally
	cd docs && python3 -m http.server 8765
