# Security

AgentRank runs untrusted model output against a commerce system that holds stock, quotes and
payment state. This document describes the boundaries that makes that safe, and it is written to
be checkable against the code rather than reassuring. Where a boundary is narrower than its name
suggests, the limit is stated here rather than left to be discovered.

## Reporting

There is no configured security reporting channel for this project yet, and no monitored security
address. Nothing here should be read as a disclosure commitment or a response-time promise.

AgentRank has no public signup, no hosted multi-tenant production deployment and no external
users. Until one of those changes, the honest statement is that the boundaries below are
maintained deliberately and that there is nowhere to report a finding to.

## What this system is not

Two absences shape every boundary below, and neither is an oversight.

**There is no user authentication.** A merchant API credential is a machine credential held by
whatever integration acts on one merchant's behalf. It answers exactly one question, which
merchant a request is scoped to, and it never answers who is holding it. Two people sharing a key
are indistinguishable, and nothing in this system claims otherwise. `credential_id` on an audit
event is evidence about which key authorized a request, not attribution to a person.

**There is no authenticated operator.** The operator command line provisions merchants, issues
credentials and resolves stranded payments, and nothing authenticates the person running it. The
argument that makes it acceptable is scope rather than strength: to run those commands you must
already be able to run this repository's code against this repository's database, and anybody who
can do that could write the same rows by hand. That argument holds only while the operator surface
is local. Audit events from operator actions record the `SYSTEM` actor, which names a role and
deliberately not an identity, because a label without evidence behind it would look like
attribution and would not be.

## Merchant authentication

A merchant API key is presented as `Authorization: Bearer <key>`. It is verified against a stored
digest; the key itself is never stored and cannot be recovered from the database. `/health` and
`/ready` need no credential. Every commerce endpoint does.

Merchant isolation is structural rather than filtered. Queries are merchant scoped, and the
commerce tables use composite foreign keys carrying `merchant_id`, so a row belonging to one
merchant cannot be referenced by another merchant's row whatever a caller passes. Another
merchant's identifier raises the same not-found error as an identifier that does not exist, so
the API never confirms that a resource belongs to somebody else.

Authentication failure is the stronger claim of the two, and it is asserted as whole responses
compared against each other rather than case by case: no credential, a malformed one, an
unissued one, a valid identifier with the wrong secret and a revoked one all produce one
indistinguishable answer. That is exactly the property a per-case assertion would miss, because
the failure it guards against is one branch answering differently from the others.

Revocation takes effect through the authenticating query itself: the revocation condition is in
the SQL, so there is no cache to wait out. A request that was already authenticated when a
revocation committed is not retroactively unauthenticated. That is a stated boundary rather than a
gap, and it is the ordinary semantics of a committed transaction.

## Browser sessions

The console is a Next.js server that renders merchant pages by calling the API. It holds no
merchant API key of its own, and there is no environment credential anywhere that stands in for a
signed-in merchant.

A merchant signs in with their own key. The console exchanges it for a durable session held by the
API and forgets the key. Nothing in the session table can produce a merchant API key, so a copy of
that table is not a set of working credentials.

The value in the browser cookie is not the session verifier the API stores. The console derives
the verifier from the cookie by HMAC under a deployment secret
(`AGENTRANK_CONSOLE_SESSION_SECRET`). A cookie recovered from a retained browser trace, a proxy log
or a support screenshot is therefore inert without that secret; the cookie and the secret are both
required and neither is sufficient.

Sessions are bound to the credential that opened them, so revoking a leaked merchant key closes
the sessions it minted. Session verifiers use a `ars_` prefix and merchant API keys use `ar_`. The
two grammars are disjoint, so neither can ever parse as the other, and which one arrived is decided
by the value rather than by anything the caller says about it.

Session cookies are `Secure` unless `AGENTRANK_COOKIE_SECURE` is exactly `false`, which exists so a
console on plain `http://localhost` can hold a session at all. Setting it in a deployment puts the
session cookie on the wire in the clear.

## SSRF-safe public import

The merchant page importer is the only place this application fetches a URL that a request body
chose. Everything else AgentRank connects to is an address this repository decided. It is written
as one narrow mechanism so that there is exactly one answer to what AgentRank may connect to.

Four decisions run in order, and none trusts the one before it:

```text
the URL         a shape this repository will consider at all
the addresses   every address the name resolves to, all of them, before any connection
the connection  made to a validated address literal and never to the name again
the response    bounded in bytes, in type and in time, and read as data
```

The third decision is the one usually missing. Validating a hostname and then handing that
hostname to an HTTP client lets the client resolve it a second time, and a name that answered with
a public address for the check can answer with `127.0.0.1` for the connection. So the connection is
made to the address that was already validated: the request URL carries the address literal, the
`Host` header carries the original name so the merchant's server routes correctly, and the SNI
hostname carries the original name so TLS is verified against the name rather than the number.
There is no second resolution to poison.

Redirects are followed by this module and not by the HTTP client, because a client following
redirects applies none of the above to the second hop. Every hop goes through all four decisions
from the beginning.

The importer sends `GET`, sends no body, presents no credential, stores no cookie, runs no script,
and never reads a response to decide what to fetch next. Only the merchant's supplied URL list
decides what is fetched.

`AGENTRANK_IMPORT_ALLOWED_NETWORKS` is the single way to widen the address policy beyond globally
routable addresses, and it exists so the test suite can import from a synthetic storefront on
loopback. It is refused structurally in any deployment: the process refuses to start unless
`AGENTRANK_ENV` is present in the process environment and names `development`, `ci` or `test`.
Present, not merely defaulted. `AGENTRANK_ENV` defaults to `development`, so an absence must never
be what authorizes reaching a private network from an attacker-influenced URL.

## Buyer isolation

A benchmark mission is carried out by a buyer that may be a model. That buyer runs in a separate
process, one process per mission, and the isolation is a process boundary rather than an
arrangement of private names.

What the worker is given:

```text
stdin      one mission request: a brief, a merchant, a base URL, one merchant credential
env        PATH, HOME, the locale, TMPDIR and the certificate paths, as an allowlist
cwd        an empty directory of its own
```

What it is not given: a database URL, this application's settings, the suite, the run, any other
mission, the expected outcome, the simulated value, or anything about what an earlier mission did.
With no database URL there is nothing for a session to be built from, and there is no benchmark
route on the API it can reach, so no request it can make touches a suite, a run or another
mission's result.

The empty working directory is not a detail. Settings read a `.env` relative to the working
directory, so a worker started inside a checkout would read the developer's `POSTGRES_PASSWORD` off
disk with an otherwise empty environment. The worker refuses to run if it can build settings at
all, which checks the outcome rather than the inputs.

The worker cannot mark its own mission. Its report carries identifiers and actions only: no
status, no failure reason, no error origin, no price, no authorization decision and no payment
status. What those identifiers came to is established on the trusted side from the merchant's own
rows. A worker that invents a success has nothing behind it, and one that hides a real purchase is
found anyway, because payments are swept rather than reported.

**What this is not.** This is not operating system sandboxing and does not claim to be. The
untrusted thing is a model handed a brief and a tool schema, never a Python interpreter. In a
developer checkout the repository is still a readable directory on the same filesystem.

**The in-process path is weaker and is not the boundary.** The deterministic reference executor
runs in process, and the buyer surface it holds contains application services which hold a session,
so reaching one takes a deliberate act through two private attributes rather than being impossible.
Python offers no arrangement of private names that would change that. The same applies to the
in-process tool witness: it guarantees that the executor does not decide the fault origin, not that
it could not tamper with the record. The out-of-process boundary is the real one, and it is watched
by a server-side record the worker cannot reach at all.

## MissionOracle isolation

A mission is a pair of types, not two conventions on one type:

```text
AgentMissionBrief    everything a buyer agent is allowed to see
MissionOracle        what the evaluator knows and the agent must not
```

They are separate types so that "do not hand the answer to the agent" survives somebody adding a
field in a hurry. A brief that accidentally carried an expected outcome would turn every control
mission into a giveaway and nothing would fail.

Authored suites live in `benchmarks/<world>/` at the top of the repository rather than inside the
Python package. This was a packaging change, not a rule. While the definitions were a module in
`agentrank_api.benchmark`, a worker process could import them and read every mission's expected
outcome indexed by the mission key it had just been handed, and an independent audit proved it by
doing exactly that. The distribution built from `apps/api` contains `src/agentrank_api` and nothing
else, so there is no module to import and nothing in the working directory to read. `PYTHONPATH`
came off the environment allowlist at the same time, closing a checkout on the path as a second
route.

## Compiler firewall

The merchant source document is the untrusted half of the system. Everything else a merchant
submits is a decision about something AgentRank already holds; a source document is content that
becomes an immutable artifact which a deterministic compiler reads and turns into an agent-facing
representation.

It is bounded in the layer the browser reaches:

```text
identity is the server's   no key, no version, no merchant. A body carrying one is refused
every field is bounded     length, count and range, so no document is unbounded work
nothing is coerced         strict mode, so "12" is not 12 and 1 is not true
nothing extra survives     unknown fields are a refusal, not a silent drop
```

Refusing unknown fields is the compiler firewall at this boundary rather than tidiness: a benchmark
answer smuggled into an unexpected field would be stored as source evidence and read by the
extractor, and there is no unexpected field.

The compiler runs no model and infers nothing. Every fact in a compiled representation is traceable
to a source field the merchant stated, and a merchant reviews candidates before anything is
published.

Generated benchmark missions never carry merchant prose. Objectives are written by the generator
and parameterized by a quantity alone, because a mission objective is the one channel a buyer reads
as its own goal rather than as merchant data.

## Provider credential handling

Model provider credentials are held as secret values that mask themselves in reprs, exception
messages, log lines and serialized settings. A missing provider credential is a capability the
process does not have, not a failure: the process starts, and launches frozen to that provider stay
queued for a worker that holds the key.

The isolated worker receives exactly one credential for the selected provider and never the other
provider's. A provider variable may list several keys, each belonging to a different provider
project. The trusted side hands one to each mission's process and moves to the next key for the
next mission, and a mission the provider refused before the buyer made any commerce request is
retried once per remaining key. The worker never sees the list, and which key was presented is
not evidence about anything: the model, the frozen configuration and every other pin are the same
whichever key answered. Each process logs one startup line naming its environment, its expected
schema revision and which capabilities it holds, in names and never in values.

Spending is governed on the trusted side, before the worker process exists. A permit is reserved
and committed durably before anything leaves the machine, and settled after the process has exited.
No provider call is made inside a database transaction and no transaction is held across one. When
a worker's outcome is unknown the full grant stays charged, so an unanticipated failure overcounts
rather than silently restoring allowance for a request that may already have been paid for.

Razorpay is Test Mode only, and structurally so. The key id must begin `rzp_test_`; a live key is
refused at startup, and there is no environment variable, request field or flag that relaxes it.
This project has no live mode, and removing that check would be the deliberate act of enabling real
money. The key secret stays in the API process, is never returned by anything, never appears in an
audit payload, and has no accessor that would make doing so convenient. The browser receives only
the public key id and an order identifier.

## Deterministic financial authorization

No model authorizes a financial action. A model buyer can request a quote and attempt a payment;
whether either is permitted is decided by deterministic code reading the merchant's own rows.

Two independent gates, neither substituting for the other:

```text
financial   is this checkout within the mandate's merchant, currency, amount ceiling,
            quantity ceiling and validity window, and is the quote itself still valid
semantic    does what is being bought satisfy the buyer's authoritative hard constraints
```

Both are pure functions. They read no clock beyond an instant handed to them, touch no database,
call no external service and consult no model, so the same inputs always produce the same decision.
Payment admission requires both at once, with the mandate, the checkout and the variant rows held,
in a fixed lock order.

Three rules make the semantic gate fail closed:

- every constraint applies to every line, so "category chargers" is not satisfied by a checkout
  that also contains headphones
- a missing attribute is a denial, never a default. Inferring it from a product description would
  be inventing the answer
- values of different kinds are never compared. `"100"` is not `100`, and reporting either a pass
  or an ordinary mismatch from that comparison would report a fact nobody established

The load-bearing case is an absent constraint set. A mandate can exist without one, and an absent
set is never read as "there were no semantic requirements", which would be the most dangerous
default available. It is reported as `INTENT_CONSTRAINTS_MISSING` and the result is not authorized,
whatever the financial gate said.

A mandate is a ceiling for one purchase and not a balance drawn down by several. At most one
successful payment may ever consume a mandate, enforced by a partial unique index on the payment
attempt table rather than by application care.

Quotes are priced from the catalog and never from the request. A caller states which variants and
how many; what they cost, and what they are, is snapshotted from the catalog at the moment of
quoting.

## Audit trail

Audit events are append only, enforced at two levels: the application exposes no update and no
delete, and a database trigger rejects both. The trail records what the system decided, not
messages a human wrote.

## Deployment posture

`AGENTRANK_ENV` decides which rules a process runs under, and it is read from the process
environment before configuration is loaded. `development`, `ci` and `test` may be configured from a
`.env`; anything else is a deployment, reads no file, and must state `POSTGRES_HOST`,
`POSTGRES_DB`, `POSTGRES_USER` and `POSTGRES_PASSWORD` or it refuses to start naming the one that
is missing. A `.env` that tried to set `AGENTRANK_ENV` is refused, because a file deciding which
rules apply to it is a file deciding whether it gets read.

Configuration failures name fields and never values. Pydantic's own rendering is deliberately
discarded, because it embeds a truncated repr of the whole input and the truncation keeps the tail,
which would print the last environment-sourced value into whatever reads a failed boot.

`/health` and `/ready` require no credential, which makes what they may say a security question.
They name components and states, never a host, a user, a URL, a configured value or a driver's own
message. The schema revision is reported deliberately: it is a build fact that appears in migration
filenames and repository history, and it is what makes a readiness probe useful during a deploy.

Schema is never created or altered as an application startup side effect. Migrations are an
explicit step, and a process serving traffic against a schema it was not built for reports not
ready rather than ready with a warning.
