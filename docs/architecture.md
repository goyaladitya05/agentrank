# Architecture

How AgentRank is put together, and which component owns which kind of truth. This is a
description of the system as it stands, not a history of how it got here.

## Shape

A modular monolith with a small number of separate processes. There is one Python package,
`agentrank_api`, organised into modules with explicit boundaries between them, and there are no
microservices, no message broker and no service mesh.

Processes exist where a process boundary buys something specific:

```text
Next.js console      renders merchant pages, holds the browser session secret
FastAPI API          every commerce, compiler, import and benchmark endpoint
Benchmark dispatcher claims one queued evaluation and executes it
Isolated buyer       one process per mission, no database, spawned by the dispatcher
PostgreSQL           durable coordination and the whole evidence record
```

The dispatcher is separate because a benchmark run takes as long as a suite takes. Holding a
browser request open across one would make the merchant's network the thing that decides whether a
measurement survives. The buyer is separate because it is the untrusted half and a process is the
only boundary Python actually provides.

## Frontend and backend boundary

The console is a Next.js application that renders on the server. It calls the API from the Next.js
server rather than from the browser, so no CORS configuration exists, the API base URL is never
exposed to the client, and the browser talks to exactly one origin.

The console holds no merchant API key. A merchant signs in with their own, the console exchanges it
for a durable API session and forgets the key, and the browser holds only an opaque cookie. The
console derives the API session verifier from that cookie by HMAC under a deployment secret, so the
cookie alone is inert. Because sessions live in PostgreSQL rather than in console memory, a session
survives a console restart and resolves on any console process, which is what allows more than one.

## PostgreSQL

PostgreSQL is the only datastore. There is no cache, no queue service and no object store, and
SQLite is not used anywhere including in tests.

It carries three distinct jobs:

- **Durable evidence.** Source snapshots, compiler runs, published representations, benchmark runs,
  mission results, agent traces, payment attempts and the append-only audit trail. None of it is
  reconstructible from anywhere else.
- **Coordination.** The evaluation launch queue, per-merchant advisory locks that serialize world
  preparation and workspace bootstrap, provider spending permits, and partial unique indexes that
  enforce invariants like one succeeded payment per mandate.
- **Schema as a contract.** Every build knows the Alembic revision it expects and reports not ready
  when the database is at a different one.

Schema is never created or altered as an application startup side effect. Migrations are an
explicit operator step.

Money is stored as an integer count of minor currency units, never as a floating point number, and
any monetary value crossing a subsystem boundary carries its currency alongside the amount.

## Source and import

A merchant's own information enters the system as a **source snapshot**: an immutable,
content-addressed document describing their products, variants, prices, stock and policies. Source
history is versioned, and a snapshot is never edited.

There are two ways to produce one, and they converge:

```text
the source editor    the merchant submits the document directly
the page importer    AgentRank reads the merchant's own public pages
```

The importer is four steps with hard boundaries between them: a bounded public GET of the URLs the
merchant named, deterministic extraction with omissions stated, merchant inspection of the draft,
and confirmation. An import never creates a snapshot on its own, never compiles, never publishes,
never builds a workspace and never calls a model. Confirming produces exactly what typing the same
document into the editor would produce, through the same service and the same schema.

Extraction never guesses. A fact the merchant's page did not state is recorded as absent rather
than inferred, because inferring it would mean measuring a product that does not exist.

The importer is synchronous. It fetches at most a dozen pages, sequentially, each bounded, under an
overall deadline well inside a request lifetime. A queue would add a job table, a worker, a polling
surface and a set of partial states to avoid a wait the merchant chose by pressing a button.

## Merchant Compiler and Commerce IR

The Merchant Compiler turns a frozen source snapshot into **Commerce IR**: a structured,
agent-ready representation where every important fact is explicit, typed and unit-bearing.

Three properties define it:

- **It is deterministic.** No model runs. The same snapshot and the same compiler build produce the
  same candidates in the same order.
- **It infers nothing.** Every fact in a representation traces back to a source field the merchant
  stated. The compiler does not decide that a title implies a wattage.
- **It owns no financial truth.** A representation is a description of a merchant's catalog for the
  purpose of discovery. Prices, stock, quotes, authorization and payment are decided by the
  deterministic commerce runtime reading the merchant's authoritative rows, never by compiled
  output. A published representation cannot move a quote total.

The merchant reviews candidates and publishes. Review decisions are permanent, and a published
representation is immutable and content-identified, which is what lets a benchmark run pin exactly
which artifact it measured.

## Deterministic commerce runtime

The commerce kernel owns everything a purchase actually depends on: the catalog, quoting,
inventory, spending mandates, the two authorization gates, and payment execution.

Every rule here is deterministic and pure where it can be. Authorization is two independent
functions that read no clock beyond an instant handed to them, touch no database and consult no
model. Quotes are priced from the catalog, never from the request. Inventory is compared at quote
time and reserved at admission, under a fixed lock order.

No model authorizes a financial action. A model buyer requests a quote and attempts a payment;
whether either is permitted is decided here. See [SECURITY.md](../SECURITY.md) for the gates.

Payment execution never holds a database transaction across the network. It locks and marks the
attempt in flight, commits, calls the provider with nothing open, then locks everything the outcome
touches and records it. Only a deterministic fake provider and Razorpay Test Mode exist; there is no
live payment mode.

## Evaluation workspace

A merchant with a source snapshot has evidence but no way to be measured against it. The
**evaluation workspace** is the bootstrap out of that, and it writes exactly three things in one
transaction: a benchmark environment registering this merchant as a benchmark world, a generated
suite with its missions, and a workspace row recording that both came from this snapshot under this
configuration.

It writes no commerce row at all. The world is materialized by the existing benchmark preparation,
which already owns the shelf lock and the refusal to reset a world a run is using, so "bootstrap
cannot alter authoritative commerce state" is true because there is no statement here that could.

Suite generation reads the frozen source-derived catalog and nothing else: no compiler run, no
representation, no benchmark result and no previous suite. It runs no model, and sampling is a
stable total order followed by a bounded take, so there is no random number generator and no seed.

## Benchmark system

A **suite** is a set of missions, immutable and content-addressed. A **mission** is a pair: an
`AgentMissionBrief` holding everything the buyer may see, and a `MissionOracle` holding what the
evaluator knows and the buyer must not.

A **run** executes one suite against one merchant, and pins what it measured: the suite, the
environment, the executor kind and revision, the catalog hash, the evaluator version, and the
representation identity where one was read. Two runs whose pins differ were measured against
different things.

A **launch** is a merchant's request for an evaluation. Admission resolves every methodology-critical
identity server side from the authenticating credential, checks it against the digest of the plan
the merchant was shown, freezes it on the row, commits, and answers with a queued launch. Nothing is
executed in the request.

The world is restored before every mission, so one mission cannot change what the next one sees, and
a run holds a claim on its world that another run cannot take.

## Isolated buyer execution

The dispatcher claims one queued launch and executes it. By the time it runs there is no request, no
session and no browser credential left to consult, so the merchant, the purpose, the suite, the world
and the buyer configuration all come from what admission froze. Nothing takes an identity from a
caller.

Each mission runs in its own worker process, given one mission on stdin and an allowlisted
environment with no database URL, in an empty working directory. It returns one report of identifiers
and actions. It cannot mark its own mission, state a commerce fact, or say whose fault an
interruption was: all of that is established on the trusted side from the merchant's own rows and
from what the trusted tool boundary recorded.

A dispatch that claimed nothing reports `UNSERVICEABLE` and names the executor it is waiting for, so
"there is no work" and "there is work no worker here can do" are different answers. A launch frozen
to a provider a worker holds no credential for stays queued for one that does.

## Provider governance

Live model calls happen inside a worker with no database, so the process making the call cannot be
what decides whether the call is affordable. That decision is made on the trusted side, before the
process exists, and committed before anything leaves the machine:

```text
reserve a permit and commit it   durable, outside any long transaction
start the worker process         the network call happens here, with no database
read what the worker reported    trusted code, from a process that has already exited
reconcile or assume it spent     one write, and it can never restore an unknown request
```

Settlement is asymmetric on purpose. A clean exit charges what was reported; an unknown outcome
charges the full grant permanently. Spending is accounted per run rather than per launch, because an
operator executing an experiment sample spends the same money without a launch row.

Provider concurrency is read off the launch table rather than held in a lease of its own, so there is
one answer to "how many evaluations are calling this provider right now" and one existing recovery
path when a dispatcher dies holding one. Both checks run under a transaction-scoped advisory lock
keyed by provider name.

## Diagnostics

Diagnostics is a read model over runs. It produces the reading a merchant needs: what changed
between two runs, whether two arms of an experiment were comparable, how much simulated demand moved,
and which methodological caveats must travel with every one of those statements.

It is deterministic and pure, taking flattened facts and producing deltas, transitions, warnings and
the strongest conclusion the evidence supports. It invents no statistics: no confidence intervals, no
significance language, no weighting. See [methodology.md](methodology.md) for what it will and will
not conclude.

## Browser session model

```text
merchant API key   ar_  presented once to open a session, never stored, forgotten by the console
cookie                  opaque, held by the browser, inert without the console secret
session verifier   ars_ derived from the cookie by HMAC, stored by the API, bound to the credential
```

The two token grammars are disjoint, so neither can parse as the other. Sessions expire on their own
schedule, are closed by signing out, and die when the credential behind them is revoked.

## Operator command line

Provisioning merchants, issuing and revoking credentials, seeding and running benchmarks, dispatching
launches, and resolving stranded payments are command line operations rather than endpoints. Nothing
authenticates an operator, and an endpoint that terminalized a payment or released a merchant's stock
would be an unauthenticated way to do exactly that. A command line moves the trust boundary onto
something that already exists: running these commands requires being able to run this repository's
code against its database.

Every command delegates to the same services the API uses. There is no SQL, no lock and no state rule
in the command line, because a second copy of any of them would be a second answer.

## Deployment topology

```text
Console (Next.js)  -->  API (FastAPI)  -->  PostgreSQL
                             ^                  ^
                             |                  |
                        Dispatcher  -------------
                             |
                        Buyer worker (one per mission, no database)
```

The console reaches the API. The API and the dispatcher both reach PostgreSQL. The buyer worker
reaches only a loopback commerce endpoint with a short-lived credential bound to its run, and,
when it is a model buyer, the external provider. PostgreSQL is never publicly exposed.

Configuration comes from the environment. A deployment reads no `.env`, states its database
explicitly, and fails at startup naming the variable that is missing. Migrations are applied as an
explicit step before processes start serving.

## Known limitations

Stated here rather than discovered later. Measurement limitations are in
[methodology.md](methodology.md); these are the system ones.

**Launch admission has a window.** Each statement gets its own snapshot and the admission advisory
lock serializes other launches rather than benchmark runs, so a run reaching completed partway
through a preflight is a real interleaving. The reads are ordered so that the check for existing
completed evidence is last, which narrows it to the window between that read and the insert. No
ordering closes that window.

**A quote does not reserve stock.** Inventory is compared at quote time, never decremented and never
held. Stock can change between the quote and any later execution; admission is where a hold is
taken.

**Shipping and discount have no authoritative source.** `shipping_amount_minor` and
`discount_amount_minor` exist on a quote so the price model is complete and so adding a shipping
provider or a promotion engine later is not a migration. Neither is reachable from the API.

**Inventory underflow is clamped and reported.** A total below the quantity held by committed
reservations should be unreachable while nothing in this application writes stock. It becomes
reachable the day a merchant inventory endpoint exists, and the honest answer then is that money
moved and the merchant is oversold, which the caller records in the audit trail rather than hiding
behind a negative number.

**Alembic autogenerate does not see changed constraint expressions.** It detects a missing or added
constraint, not a changed expression on one that exists on both sides. A check constraint whose value
list grows has to be dropped and recreated by hand, or the model and the database drift apart
silently.

**There is no account model.** One merchant API key is one identity. Two people sharing a key are
indistinguishable, and there is no role, permission or ownership concept above the merchant.
