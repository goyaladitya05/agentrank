# Repository commands. `make check` is the single local gate that must pass before every
# commit and every push.
SHELL := bash
.SHELLFLAGS := -eu -o pipefail -c
.DEFAULT_GOAL := help

UV ?= uv
PNPM ?= pnpm
WEB := --filter @agentrank/web
API_APP := agentrank_api.main:create_app
OPERATOR_CLI := agentrank_api.cli

.PHONY: help install format lint format-check typecheck test test-backend test-frontend \
	build-frontend test-browser check-text check-whitespace check \
	db-up db-down db-reset db-verify migrate seed-dev seed-benchmark api web payments credentials \
	benchmark

help: ## List available targets
	@grep -hE '^[a-zA-Z][a-zA-Z0-9_-]*:.*## ' $(MAKEFILE_LIST) \
		| awk -F':.*## ' '{printf "  %-16s %s\n", $$1, $$2}' | sort

install: ## Install backend and frontend dependencies from the lock files
	$(UV) sync
	$(PNPM) install --frozen-lockfile

format: ## Rewrite code to match the formatters
	$(UV) run ruff format .
	$(UV) run ruff check --fix .
	$(PNPM) $(WEB) format

lint: ## Lint backend and frontend
	$(UV) run ruff check .
	$(PNPM) $(WEB) lint

format-check: ## Verify formatting without rewriting anything
	$(UV) run ruff format --check .
	$(PNPM) $(WEB) format:check

typecheck: ## Type check backend and frontend
	$(UV) run mypy apps/api migrations tests scripts
	$(PNPM) $(WEB) typecheck

test-backend: ## Run backend tests. Requires PostgreSQL to be running
	$(UV) run pytest

test-frontend: ## Run frontend tests
	$(PNPM) $(WEB) test

test: test-backend test-frontend test-browser ## Run all tests

# The seeded keys reach the tests through the environment and never through an argument vector,
# and the artifact scan runs whether the workflows passed or failed: a failing run is exactly when
# a trace is retained, so it is exactly when a credential left in one would matter.
test-browser: migrate build-frontend ## Run critical browser workflows against local services
	@compiler_key="$$($(UV) run python scripts/seed_compiler_e2e.py)"; \
		reevaluation="$$($(UV) run python scripts/seed_reevaluation_e2e.py)"; \
		reevaluation_key="$$(printf '%s\n' "$$reevaluation" | sed -n 1p)"; \
		status=0; \
		AGENTRANK_E2E_KEY="$$compiler_key" \
		AGENTRANK_E2E_REEVALUATION_KEY="$$reevaluation_key" \
		AGENTRANK_E2E_REEVALUATION_WORLD="$$(printf '%s\n' "$$reevaluation" | sed -n 2p)" \
		$(PNPM) $(WEB) test:e2e || status=$$?; \
		AGENTRANK_E2E_SECRETS="$$compiler_key $$reevaluation_key" \
		$(UV) run python scripts/check-e2e-artifacts.py \
			apps/web/test-results apps/web/playwright-report; \
		exit $$status

build-frontend: ## Build the console the way production would
	$(PNPM) $(WEB) build

check-text: ## Fail if the em dash character appears in project text
	$(UV) run python scripts/check-text-style.py

check-whitespace: ## Fail on trailing whitespace and related errors
	./scripts/check-whitespace.sh

check: lint format-check typecheck test build-frontend check-text check-whitespace ## The full local gate
	@echo "all checks passed"

db-up: ## Start PostgreSQL and wait for it to become healthy
	docker compose up -d --wait

db-down: ## Stop PostgreSQL, keeping the data volume
	docker compose down

db-reset: ## Destroy the data volume and start a clean database
	docker compose down -v
	docker compose up -d --wait

db-verify: ## Confirm the local database is reachable and is PostgreSQL 18
	./scripts/check-postgres.sh

migrate: ## Apply all migrations
	$(UV) run alembic upgrade head

seed-dev: ## Create or refresh the local development catalog
	$(UV) run python scripts/seed_dev_catalog.py

seed-benchmark: ## Create or refresh the VoltEdge catalog and publish its benchmark suite
	$(UV) run python scripts/seed_benchmark.py

api: ## Run the API with reload
	$(UV) run uvicorn $(API_APP) --factory --reload --port 8000

web: ## Run the console with reload
	$(PNPM) $(WEB) dev

payments: ## Run the payment operator CLI. Example: make payments ARGS="list-unresolved"
	$(UV) run python -m $(OPERATOR_CLI) payments $(ARGS)

credentials: ## Run the credential operator CLI. Example: make credentials ARGS="list --merchant-slug ampere-supply"
	$(UV) run python -m $(OPERATOR_CLI) credentials $(ARGS)

benchmark: ## Run the benchmark operator CLI. Example: make benchmark ARGS="run"
	$(UV) run python -m $(OPERATOR_CLI) benchmark $(ARGS)
