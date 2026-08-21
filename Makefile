# Repository commands. `make check` is the single local gate that must pass before every
# commit and every push.
SHELL := bash
.SHELLFLAGS := -eu -o pipefail -c
.DEFAULT_GOAL := help

UV ?= uv
PNPM ?= pnpm
WEB := --filter @agentrank/web
API_APP := agentrank_api.main:create_app

.PHONY: help install format lint format-check typecheck test test-backend test-frontend \
	build-frontend check-text check-whitespace check \
	db-up db-down db-reset db-verify migrate api web

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

test: test-backend test-frontend ## Run all tests

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

api: ## Run the API with reload
	$(UV) run uvicorn $(API_APP) --factory --reload --port 8000

web: ## Run the console with reload
	$(PNPM) $(WEB) dev
