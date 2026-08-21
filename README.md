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

Install the Python toolchain and create the local virtual environment at `.venv`:

```bash
uv sync
```

Install frontend dependencies:

```bash
pnpm install --frozen-lockfile
```

Create a local environment file:

```bash
cp .env.example .env
```

The values in `.env.example` are development defaults that match the local Docker Compose
service. They are not secrets and must not be reused anywhere real.

## Local database

PostgreSQL 18 runs through Docker Compose. Start it and wait for it to become healthy:

```bash
docker compose up -d --wait
```

Confirm it is reachable and running the expected major version:

```bash
./scripts/check-postgres.sh
```

Stop it, keeping the data volume:

```bash
docker compose down
```

Data lives in the named volume `agentrank_postgres_data` and survives container restarts.
Remove it with `docker compose down -v` when a clean database is wanted.

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
