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
make credentials ARGS="create --merchant-slug ampere-supply --label local-dev"
```

`make install` creates the Python virtual environment at `.venv` and installs frontend
dependencies from the lock files. `make db-up` starts PostgreSQL and waits for it to
become healthy. `make seed-dev` loads a small development catalog: one merchant, five
products, ten variants. It is safe to run repeatedly and is never run by the application
itself. Run `make help` to list every target.

The last command mints a merchant API key and prints it once. Every commerce endpoint needs
one, presented as `Authorization: Bearer <key>`; `/health` and `/ready` do not. There is no
fixed development key in this repository and there will not be one: a key that is written down
somewhere is a key that ends up somewhere it should not, so each developer mints their own and
it exists only in their own database and their own shell history. Lose it and issue another,
then revoke the first:

```bash
make credentials ARGS="list --merchant-slug ampere-supply"
make credentials ARGS="revoke <credential-id>"
```

The values in `.env.example` are development defaults that match the local Docker Compose
service. They are not secrets and must not be reused anywhere real.

## Benchmark

The benchmark measures whether a buyer can complete a purchase against a merchant, and where it
breaks. It runs against a versioned fixture world rather than against whatever is in the
database, and the world is put back before every mission so that one mission cannot change what
the next one sees.

```bash
make benchmark ARGS="seed"
make benchmark ARGS="run --representation-label baseline"
make benchmark ARGS="show <run-id>"
```

The authored world lives in `benchmarks/voltedge/`: `catalog.json` is the shelf a run puts the
merchant back to, and `suite.json` is the missions with their expected outcomes. They are files at
the top of the repository rather than a module in the application package, because a mission's
expected outcome is the answer key and the package is what a buyer process runs from. The commands
take `--world`, defaulting to that directory.

`seed` registers the VoltEdge world, restores its catalog and publishes the suite authored
against it. `run` executes all fourteen missions with the deterministic reference executor,
through the real checkout, authorization, inventory and payment path with a deterministic fake
provider. No real money is involved; stock genuinely leaves the shelf inside the benchmark world
and is restored before the next mission.

The reference executor is not an AI buyer. It has no model, no prompt and no language
understanding, and it reads structured commerce fields a real storefront does not publish. Its
completion rate is evidence that the benchmark path works, and it must never be presented as
evidence of what an autonomous agent can do. Every report says so.

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

`AGENTRANK_CONSOLE_SESSION_SECRET` is required and the console refuses to start without it. Every
browser session credential is derived from it, so every console process serving one deployment
needs the same value, and changing it signs every merchant out at once. Generate one with
`openssl rand -hex 32`. On plain HTTP localhost, also set `AGENTRANK_COOKIE_SECURE=false`, which
is the only thing that lets a session cookie be issued without HTTPS.

The console holds no merchant API key. A merchant signs in with theirs, the console exchanges it
for a durable session held by the API and forgets the key, and the browser holds only an opaque
cookie. There is no environment credential that stands in for a signed in merchant.

### Razorpay test checkout

`/razorpay` opens a Razorpay Standard Checkout against an AgentRank quote. It is integration UI
rather than product UI: paste a checkout identifier, pay with a Razorpay test card, and the page
reports the resulting AgentRank state.

It needs a signed in merchant session in the browser, and a Razorpay Test Mode key pair in the
API's `.env`:

```text
RAZORPAY_KEY_ID=rzp_test_...
RAZORPAY_KEY_SECRET=...
```

The key id must begin `rzp_test_`. A live key is refused at startup and there is no variable,
request field or flag that relaxes that: this project has no live mode. The key secret stays in
the API process; the browser receives only the public key id and an order identifier, which is
what Standard Checkout needs.

This page has not yet been run against real Razorpay Test Mode keys. The integration is verified
end to end against a transport fake and the signature formula is pinned against a digest computed
outside this codebase, so what is proven is that the application sends what Razorpay documents
and handles every answer Razorpay documents. Whether Razorpay agrees is unproven until somebody
completes one test payment.

The console authenticates a merchant by their own API key and holds the resulting session in
PostgreSQL, so a session survives a console restart and resolves on any console process. What it
still has is no account model: one merchant API key is one identity, and two people sharing a key
are indistinguishable.

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
