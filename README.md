# AgentRank

AI commerce readiness benchmark and Merchant Compiler.

AgentRank measures whether AI buyer agents can actually transact with a merchant,
identifies why they fail, compiles ordinary merchant catalogs and policies into a
structured machine readable commerce representation, then reruns the identical benchmark
to measure the change in conversion and simulated GMV.

The project is being built in phases. Phase 0 is the repository and development
foundation. No commerce functionality is implemented yet.

## Requirements

| Tool | Version |
|------|---------|
| Python | 3.14 |
| Node | 24 |
| PostgreSQL | 18, run through Docker Compose |
| uv | latest |
| pnpm | 11 |
| Docker Engine and Compose v2 | latest |
| Make | any |

Ubuntu is the reference platform. All scripts assume Bash.

## Setup

```bash
cp .env.example .env
make install
make db-up
make migrate
make seed-dev
```

`make install` creates the Python virtual environment at `.venv` and installs frontend
dependencies from the lock files. `make db-up` starts PostgreSQL and waits for it to
become healthy. `make seed-dev` loads a small development catalog: one merchant, five
products, ten variants. It is safe to run repeatedly and is never run by the application
itself. Run `make help` to list every target.

The values in `.env.example` are development defaults that match the local Docker Compose
service. They are not secrets and must not be reused anywhere real.

## Checks

One command validates the repository. It must pass before every commit and every push.

```bash
make check
```

It runs backend linting and formatting, mypy, pytest, frontend linting and formatting,
TypeScript, Vitest, a production Next.js build, the text style scanner and whitespace
validation. Backend tests need PostgreSQL running, so run `make db-up` first.

## Local database

PostgreSQL 18 runs through Docker Compose.

```bash
make db-up       # start and wait for healthy
make db-verify   # confirm it is reachable and is PostgreSQL 18
make db-down     # stop, keeping data
make db-reset    # destroy the data volume and start clean
```

Data lives in the named volume `agentrank_postgres_data` and survives container restarts.

## Backend

Start PostgreSQL first, then run the API:

```bash
make api
```

| Endpoint | Meaning |
|----------|---------|
| `GET /health` | The API process is running. Never touches the database. |
| `GET /ready` | Every required dependency is reachable. Returns 503 when one is not. |
| `GET /api/v1/commerce/products/{product_id}` | One product with its merchant and every variant. |
| `POST /api/v1/commerce/products/search` | Search one merchant's catalog. |

Search is merchant scoped, deterministic and variant aware. A product is returned when at
least one of its variants satisfies every filter, and the response carries exactly those
variants as `eligible_variants`. Amounts are integers of minor currency units and always
travel with their currency, so a price ceiling must state the currency it is in.

```bash
curl -sS localhost:8000/api/v1/commerce/products/search \
  -H 'content-type: application/json' \
  -d '{"merchant_id":"...","query":"100W charger","max_price_amount_minor":500000,"currency":"INR"}'
```

Run the backend tests, which need a running database:

```bash
make test-backend
```

## Frontend

The console is a Next.js app in `apps/web`. It renders on the server and fetches the API
from the Next.js server rather than the browser, so no CORS configuration is needed and the
API URL is not exposed to the client.

```bash
make web             # http://localhost:3000
make test-frontend
make build-frontend
```

`AGENTRANK_API_BASE_URL` selects the backend. It defaults to `http://localhost:8000`.

## Migrations

Every schema change is an Alembic migration. Schema is never created or altered as an
application startup side effect.

```bash
uv run alembic upgrade head          # apply
uv run alembic downgrade -1          # undo one revision
uv run alembic revision -m "message" # new empty revision
uv run alembic revision --autogenerate -m "message"
uv run alembic check                 # models and migrations agree
```

Autogenerate is a starting point, not an answer. Read every generated revision and make
sure `downgrade` really reverses `upgrade`.

## Continuous integration

GitHub Actions runs on pull requests and on pushes to `main`, in two jobs. Backend runs
against a real PostgreSQL 18 service container: dependency install from the lock file,
Ruff, mypy, migrations up and down and up, pytest, the text style scanner and whitespace
validation. Frontend runs ESLint, Prettier, TypeScript, Vitest and a production build.

CI needs no secrets and calls no external services. Run `make check` locally first. CI is
independent verification, not the debugging loop.

## Layout

```text
apps/api      FastAPI backend
apps/web      Next.js console
migrations    Alembic revisions
scripts       repository tooling
tests         backend tests
```

Directories appear as features need them. There are no placeholder packages.

## Conventions

- PostgreSQL 18 is used for development, tests and CI. SQLite is not used anywhere.
- Every schema change is an Alembic migration. Schema is never created or mutated as an
  application startup side effect.
- Money is stored as an integer count of minor currency units, never as a floating point
  number. Rupees 4,999.00 is stored as 499900. Any monetary value that crosses a subsystem
  boundary carries its currency alongside the amount.
- Secrets come from environment variables. `.env` is never committed.
- The em dash character is not used anywhere in this repository. `make check-text`
  enforces this.
