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
	build-frontend test-browser test-smoke check-text check-whitespace check \
	test-release db-up db-down db-reset db-verify db-backup db-restore migrate seed-dev \
	seed-benchmark api web payments credentials benchmark merchants

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

# Already part of `make test-backend`, because it is an ordinary pytest module. Here as well so
# it can be run alone: it starts real processes and takes about ninety seconds, which is a long
# time to wait for it while iterating on it.
test-smoke: ## Start the supported topology in a clean database and run one merchant evaluation
	$(UV) run pytest tests/test_deployment_smoke.py

# The whole merchant product in one deployment: an operator provisions a merchant, that merchant
# imports their own public pages from a storefront served on loopback, and everything from the
# source snapshot to the comparison happens over HTTP against real processes. Also an ordinary
# pytest module, and here as well because it starts three processes and is worth running alone.
test-release: ## Take one merchant through the entire product against a clean deployment
	$(UV) run pytest tests/test_release_candidate.py

# The seeded keys reach the tests through the environment and never through an argument vector,
# and the artifact scan runs whether the workflows passed or failed: a failing run is exactly when
# a trace is retained, so it is exactly when a credential left in one would matter.
test-browser: migrate build-frontend ## Run critical browser workflows against local services
	@compiler_key="$$($(UV) run python scripts/seed_compiler_e2e.py)"; \
		reevaluation="$$($(UV) run python scripts/seed_reevaluation_e2e.py)"; \
		reevaluation_key="$$(printf '%s\n' "$$reevaluation" | sed -n 1p)"; \
		source_refresh="$$($(UV) run python scripts/seed_source_refresh_e2e.py)"; \
		source_key="$$(printf '%s\n' "$$source_refresh" | sed -n 1p)"; \
		first="$$($(UV) run python scripts/seed_first_evaluation_e2e.py)"; \
		first_key="$$(printf '%s\n' "$$first" | sed -n 1p)"; \
		setup="$$($(UV) run python scripts/seed_workspace_e2e.py)"; \
		setup_key="$$(printf '%s\n' "$$setup" | sed -n 1p)"; \
		import_seed="$$($(UV) run python scripts/seed_import_e2e.py)"; \
		import_key="$$(printf '%s\n' "$$import_seed" | sed -n 1p)"; \
		status=0; \
		AGENTRANK_E2E_KEY="$$compiler_key" \
		AGENTRANK_E2E_REEVALUATION_KEY="$$reevaluation_key" \
		AGENTRANK_E2E_REEVALUATION_WORLD="$$(printf '%s\n' "$$reevaluation" | sed -n 2p)" \
		AGENTRANK_E2E_KEY_SOURCE="$$source_key" \
		AGENTRANK_E2E_SOURCE_REPRESENTATION="$$(printf '%s\n' "$$source_refresh" | sed -n 2p)" \
		AGENTRANK_E2E_FIRST_KEY="$$first_key" \
		AGENTRANK_E2E_FIRST_WORLD="$$(printf '%s\n' "$$first" | sed -n 2p)" \
		AGENTRANK_E2E_SETUP_KEY="$$setup_key" \
		AGENTRANK_E2E_SETUP_MERCHANT="$$(printf '%s\n' "$$setup" | sed -n 2p)" \
		AGENTRANK_E2E_IMPORT_KEY="$$import_key" \
		$(PNPM) $(WEB) test:e2e || status=$$?; \
		AGENTRANK_E2E_SECRETS="$$compiler_key $$reevaluation_key $$source_key $$first_key $$setup_key $$import_key" \
		$(UV) run python scripts/check-e2e-artifacts.py \
			apps/web/test-results apps/web/playwright-report; \
		exit $$status

build-frontend: ## Build the console the way production would
	$(PNPM) $(WEB) build

check-text: ## Fail if an em dash or an emoji appears in project text
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

# PostgreSQL holds every immutable artifact this product produces and none of it is
# reconstructible from anywhere else. These are the whole backup story: pg_dump and pg_restore,
# run by an operator, on whatever schedule they choose. tests/test_backup_restore.py executes
# both against a populated database, so the procedure is one that has been run rather than one
# that has been written down.
# `.env` is exported first, for the reason `make web` gives: these are shell scripts, and a shell
# script does not read a dotenv file the way pydantic-settings does. Without it a developer whose
# database password lives in `.env` gets an authentication failure from a backup command that
# every other operator path would have run.
db-backup: ## Dump the database. Example: make db-backup ARGS="agentrank.dump"
	set -a; [ -f .env ] && . ./.env; set +a; ./scripts/backup.sh $(ARGS)

db-restore: ## Restore a dump into an empty database. Example: make db-restore ARGS="agentrank.dump agentrank_restored"
	set -a; [ -f .env ] && . ./.env; set +a; ./scripts/restore.sh $(ARGS)

migrate: ## Apply all migrations
	$(UV) run alembic upgrade head

seed-dev: ## Create or refresh the local development catalog
	$(UV) run python scripts/seed_dev_catalog.py

seed-benchmark: ## Create or refresh the VoltEdge catalog and publish its benchmark suite
	$(UV) run python scripts/seed_benchmark.py

api: ## Run the API with reload
	$(UV) run uvicorn $(API_APP) --factory --reload --port 8000

# Next.js reads .env from apps/web, not from here, so the root .env every other command uses
# would be invisible to the console. Exported into the environment instead, which is also the
# shape a deployment uses: the console is configured by its environment and never by a file.
web: ## Run the console with reload
	set -a; [ -f .env ] && . ./.env; set +a; $(PNPM) $(WEB) dev

payments: ## Run the payment operator CLI. Example: make payments ARGS="list-unresolved"
	$(UV) run python -m $(OPERATOR_CLI) payments $(ARGS)

credentials: ## Run the credential operator CLI. Example: make credentials ARGS="list --merchant-slug ampere-supply"
	$(UV) run python -m $(OPERATOR_CLI) credentials $(ARGS)

benchmark: ## Run the benchmark operator CLI. Example: make benchmark ARGS="run"
	$(UV) run python -m $(OPERATOR_CLI) benchmark $(ARGS)

merchants: ## Provision merchants. Example: make merchants ARGS="create --merchant-slug acme --name Acme"
	$(UV) run python -m $(OPERATOR_CLI) merchants $(ARGS)
