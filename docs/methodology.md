# Benchmark methodology

What an AgentRank number means, and what it does not. The purpose of this document is to let
somebody reading the repository establish that benchmark outcomes are not manufactured.

Everything below is enforced in code rather than left to care. Where a guarantee is narrower than
its name suggests, the limit is written down here.

## The separation everything rests on

A mission is a pair of types, never two conventions on one type:

```text
AgentMissionBrief    everything a buyer agent is allowed to see
MissionOracle        what the evaluator knows and the agent must not
```

Two types rather than one, because "do not hand the answer to the agent" has to survive somebody
adding a field in a hurry. A brief that accidentally carried an expected outcome would quietly turn
every control mission into a giveaway, and nothing would fail.

The brief is written in the vocabulary a real buyer intent already uses, so a mission becomes a
buyer intent rather than being translated into one. There is deliberately no second language for
"black only" or "at most 5000 rupees".

Authored suites live outside the Python package the buyer worker runs from, so there is no module
to import and nothing in the working directory to read. See [SECURITY.md](../SECURITY.md).

## RAW versus compiled discovery

The central claim AgentRank tests is that compiling a merchant's information into an agent-ready
representation changes whether AI buyers can transact. That requires two discovery surfaces that
differ in exactly the intended way.

```text
RAW        the storefront surface a human shopper sees: names, prose descriptions,
           variant labels, prices, availability and categories
COMPILED   an agent-ready surface where every important fact is explicit, typed and
           unit-bearing, taken from the pinned Commerce IR representation
```

Three rules make the split honest rather than cosmetic:

**The catalog's own typed attribute dictionaries never reach a model buyer through either arm.** A
raw view drops them. A compiled view replaces them with facts taken from the representation under
test. So the compiled arm's enrichment comes from the treatment artifact itself and never from the
authoritative catalog the evaluator marks against, which is what stops the oracle leaking into an
arm through its own enrichment.

**Only discovery answers are projected.** Quotes, reservations, authorization decisions and payments
are financial truth from the commerce kernel and are identical for both arms. The one exception is
the quote line's typed attribute snapshot, which is stripped for both arms alike, because it is
catalog structure arriving through the checkout channel after selection has already happened, and
leaving it would let a raw buyer recover the treatment difference by quoting.

**Everything else survives untouched.** Titles, descriptions, labels, prices, stock counts and
categories are facts an ordinary storefront publishes to any shopper. Hiding them would make the raw
arm artificially broken rather than ordinarily informed.

## Generated merchant-specific suites

A merchant's first evaluation runs against a suite generated from their own frozen source snapshot.

**Every oracle is computed, never asserted.** A candidate mission is proposed from catalog facts,
and its expected outcome is decided by the same predicate a benchmark run uses to recompute ground
truth while it executes, written in the same vocabulary the authorization gate denies with. Nothing
writes an expected outcome down; it asks the merchant's own frozen data and takes the answer. A
candidate whose computed outcome disagrees with the family that proposed it is dropped rather than
relabelled.

That makes two properties true by construction:

```text
a purchase mission is genuinely purchasable    something in the frozen catalog satisfies it
an abstention mission is genuinely impossible  nothing in the frozen catalog satisfies it
```

Four separations keep a generated suite a measurement of the merchant rather than of AgentRank:

- Generation reads the frozen source-derived catalog and nothing else. No compiler run, no Commerce
  IR, no candidate and no published representation. If a first benchmark needed compiled facts to
  state its own truth, the compiler would be measuring itself.
- It reads no benchmark result, no mission trace, no diagnostic finding and no previous suite. A
  workload shaped around what a buyer failed at last time is not a measurement of a merchant.
- It runs no model. The only semantic claims a generated mission makes are ones the merchant stated
  as structured data.
- No generated mission's prose carries merchant text. Objectives are written by the generator and
  parameterized by a quantity alone, so a merchant whose category reads `wireless-chargers` does not
  measure as more machine-readable than one whose category reads `cat-17` for a reason that has
  nothing to do with their data.

Sampling is a stable total order followed by a bounded take. There is no random number generator and
therefore no seed: the same catalog and configuration produce the same suite, in the same order, in
every process and every database.

**A generated suite is not an industry benchmark.** It is specific to one merchant's catalog, and
two merchants' numbers are not comparable to each other. It measures whether a buyer can transact
with *this* merchant, which is the question the product exists to answer.

## Deterministic evaluation

The buyer is untrusted and cannot mark its own work. Its report carries identifiers and actions
only: no status, no failure reason, no error origin, no price, no authorization decision and no
payment status.

What those identifiers came to is established on the trusted side by reading the merchant's own
rows:

```text
what was selected       the variant the merchant's own quote references, described by the
                        catalog as it was before the mission ran
what it was quoted      total and currency from the checkout row
whether it was allowed  the authorization the merchant's own API answered
whether it was paid     the payment attempt rows this merchant produced during the mission
```

Four rules decide it. The quote wins over the report about what was bought, so substituting a
cheaper or more compliant identifier into a report changes nothing. The catalog wins over everything
about what that variant is, read from the pre-mission catalog, so a mission is never compared
against a shelf the mission itself changed. A payment is found rather than reported, so a buyer that
invents a success has nothing behind it and one that hides a real purchase is found anyway. What
leaves no row is read from the trusted tool boundary.

The evaluator itself is a pure function over facts. The world is restored before every mission, so
one mission cannot change what the next one sees.

## Reference execution is not AI performance

The deterministic reference executor has no model, no prompt and no language understanding, and it
reads structured commerce fields a real storefront does not publish.

Its completion rate is evidence that the benchmark path works. It is never evidence of what an
autonomous agent can do, and every report says so in its own disclaimer field. A reference run and a
model run are different kinds of measurement and must never be presented on one axis.

## Simulated demand

Every purchasable mission carries an authored value in the brief's currency. Summed across a run,
that is **simulated demand**: what this workload says the merchant's sales would have been worth if
every available purchase had been completed.

It is simulated, and the word is in the type name so it survives being quoted. No money moves in a
benchmark run. None of these figures is revenue, a forecast, or a measured business result.

The partition is three ways rather than two:

```text
captured       demand the buyer served
lost           demand the merchant could have served and did not
not_measured   demand nobody found out about, because the benchmark's own machinery failed
```

The third bucket is the honesty of the whole figure. Billing AgentRank's own infrastructure failures
to the merchant would be the easiest possible way to make a report look worse than the truth.

Simulated demand is reported per currency and never summed across currencies. The one accessor that
would produce a single figure refuses when a run spans more than one.

## Failure attribution

Which bucket a failure reaches is decided by whose side of the boundary failed:

```text
MERCHANT   a merchant surface returned an error rather than an answer or a business refusal.
           A commerce readiness finding. The mission is FAILED, its value is lost demand
AGENT      the buyer failed to carry out a mission it was equipped for: its process died, it
           ran past its time, it produced a report nobody could read, it called a tool with
           arguments that are not that tool's. The mission is FAILED, its value is lost demand
HARNESS    the benchmark's own machinery could not carry the mission out. Not a fact about the
           merchant and not a fact about the buyer. The mission is ERRORED, value not measured
```

The line between AGENT and HARNESS is drawn fail closed: anything the trusted side cannot positively
attribute to its own machinery is the buyer's, because the alternative is excusing the thing under
test. This is a correction to an earlier design where everything not the merchant's was called
HARNESS, under which a model that crashed whenever it could not solve something would have been
excused from every one of those missions.

**Provider failures are attributed, not absorbed.** A mission that ended on a model provider outage
is recorded as such, and an experiment containing any of them carries `PROVIDER_FAILURES_PRESENT`:
those outcomes reflect provider availability as much as either representation. Provider throttling
that lands on whichever arm ran second is a confound this project has actually met, which is why the
warning exists.

Merchant business refusals are separated from caller state errors by reading the merchant's own
machine-readable codes rather than the executor's account of them. A merchant declining to quote for
something it does not sell is a measurement; a mandate this execution created a moment ago having
vanished is not. Anything unclassified is treated as the caller's own state, which is the fail
closed direction.

Safety counts come from their own flags rather than from whichever failure reason happened to be
primary, so no reordering of failure precedence can hide an escape.

## There is no weighted AgentRank score

There is no headline number, and there is nowhere in the code to put one.

A weighted score is a claim about what matters and by how much. Weights chosen before there are
outputs to define them against would be invented rather than learned, and a single number is exactly
the artifact that gets quoted without its methodology.

What is published instead is raw counts, the ratios those counts obviously support, and simulated
demand by currency. Denominators come from the suite rather than from what happened:
`purchase_missions` counts every mission whose ground truth says a purchase was available, including
ones that errored or never ran. That is what makes two runs of one suite comparable, because a flaky
harness lowers a rate rather than quietly moving the bar. Errored and unfinished counts are reported
beside every rate rather than folded in, so a reader who wants a different denominator has the counts
to build it.

## Why null results are valid

A comparison that finds nothing is a result, and the system is built to be able to say so.

Every comparison of two runs carries `NOT_A_CONTROLLED_EXPERIMENT`. Two runs separated in time
differ in whatever else moved between them, so a difference is an observation and not an effect,
even when every methodology dimension matches.

For a controlled raw-versus-compiled experiment, the strongest supported conclusion is one of:

```text
PARITY                 every completed pair agreed on every mission, on safety and on captured
                       simulated demand. Compilation did not help here, at this sample size,
                       for this model and this catalog
OUTCOME_DIFFERENCES    missions changed outcome between arms, reported as transitions
INCOMPLETE             no complete raw and compiled pair has been measured yet
NOT_INTERPRETABLE      completed samples exist, but methodology evidence rules out reading
                       this as a representation comparison
```

`PARITY` is a real, publishable finding. It does not mean compilation cannot help anywhere; it means
it did not help here. A benchmark that could only report improvement would not be a benchmark.

There are no confidence intervals, no significance language and no weighting anywhere in the
diagnosis. Counts, sums within one currency, and stated caveats.

## NOT_INTERPRETABLE stays not interpretable

Some evidence rules out causal reading regardless of what the printed metrics say. When it does, the
conclusion is `NOT_INTERPRETABLE` and the metrics below it are marked descriptive only.

The warnings that force it are the ones that break comparability rather than merely weaken it: arms
that were not counterbalanced, provider failures present, samples taken before the discovery
boundary made the treatment honest, a resolved model mismatch between arms, and an unrecorded
implementation version. Incomplete pairs do the same.

A `NOT_INTERPRETABLE` experiment is never upgraded by a later reading, and matching numbers do not
rescue it. Every warning that can force it corresponds to something that actually happened in this
repository's history: provider throttling that landed on whichever arm ran second before retrying
existed, experiments run before the discovery boundary was honest, and single-pair studies whose
result looked decisive.

## Limitations of the measurement

**A version stamp is not a proof.** The evaluator version covers the failure vocabulary, the
precedence ordering and the attribution tables, which are data and can be hashed. It does not cover
the body of the evaluation function. A change to how a budget is compared, or to which stage a reason
is raised at, moves the marking without moving the stamp.

**An implementation revision detects drift and does not classify it.** The executor revision is a
digest of source bytes, so it moves for a comment as readily as for a rewritten selection rule, and
it covers only the executor's own module. A change to the shared comparison vocabulary those rules
call into moves the behaviour without moving the digest. It says two runs came from different code
and never that the behaviour changed, which is why a declared version stays beside it.

**Two whole benchmark runs are not safe against each other.** The advisory lock that serializes world
preparation is a lock on preparation, not on runs. A run holds a claim on its world, and a second run
is refused rather than allowed to reset the world underneath the first, but the lock itself makes no
broader guarantee.

**One selection is modelled per mission.** A quote covering several lines is described by the line
the executor named if that line is on it, and by the first otherwise. The quoted total covers all of
them, so the budget is still checked against everything that would be paid.

**A mission is not re-marked from a swept payment.** A payment reference is recorded only when the
attempt really is succeeded for this merchant. Where it is not, the evaluation stands on the report
rather than being revised.

**Repeat exposure is an inference channel.** A buyer that sees the same suite repeatedly can learn
from the repetition. Mission order is treated as part of the workload for this reason: an earlier
version of the development suite listed all purchasable missions and then all controls, which made
ground truth a step function of the call index, and an executor holding one integer counter scored
fourteen out of fourteen on its first run without reading the catalog. An independent review found
it. No more than two consecutive missions now share an expected outcome, which a test enforces.

**Control missions must fail for varied reasons.** A suite whose controls are all built the same way
teaches an agent to recognise the device rather than the merchant.

**Model claims never authorize financial actions.** A buyer's statement about what it selected, what
it cost or whether it was allowed is a claim, not a fact. Every financial decision is made by
deterministic code reading the merchant's own rows. See [SECURITY.md](../SECURITY.md).
