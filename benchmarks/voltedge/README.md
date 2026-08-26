# VoltEdge

The first merchant AgentRank benchmarks, and the first suite authored against it.

A catalog and a workload in one directory, because the workload is only meaningful beside the
catalog it was written for. Every mission's ground truth is a claim about `catalog.json`, and a
test asserts that every one of those claims still holds by recomputing it, so these files cannot
quietly drift into describing a merchant they no longer match.

```text
catalog.json   the world a benchmark run puts this merchant back to    voltedge-catalog@1
suite.json     the missions, each with the brief and its oracle        voltedge-core@2
```

## Why this is not in the Python package

`suite.json` holds every mission's expected outcome. That is the answer key, and a buyer must
never be able to read it. While these definitions were a module in `agentrank_api.benchmark`, an
executor process could import them and read fourteen labelled answers indexed by the mission key
it had just been handed. The distribution built from `apps/api` contains `src/agentrank_api` and
nothing else, so an authored world at the top of the repository is outside everything a worker
can import, and the operator commands are given a path to it instead.

See `agentrank_api.benchmark.authored` for the reader and SECURITY.md for what the boundary
does and does not cover.

## Publishing

```bash
make seed-benchmark
```

or

```bash
uv run python -m agentrank_api.cli benchmark seed --world benchmarks/voltedge
```

Both register the world, put the merchant's catalog back to exactly what `catalog.json`
describes, and publish the suite. All three steps are convergent: running them twice changes
nothing. Editing either document without bumping its version is refused rather than applied,
because a published suite is the historical record a run points at.

## Small on purpose

Fourteen missions, not a hundred. What this world is for is exercising every dimension the
current commerce foundation can actually decide, once each, so that the benchmark has something
real to be wrong about before it is scaled.

The catalog is deliberately imperfect, and each flaw is a commerce failure real merchants have:

```text
VE-HUB-7P        no category at all
VE-PWR-20K-NAV   a colour the merchant never published as an attribute
VE-CBL-USBC-3M   active, in the catalog, out of stock
VE-DCK-LEGACY    a product withdrawn from sale, its variant still stocked
VE-HUB-7P-EU     the same hub priced in a second currency
VE-CHG-140-BLK   real, in stock, and beyond every mission's budget
```

## Two rules govern the missions

Mission keys name the buyer's task and never the answer. `three-metre-cable` says what the buyer
wants; `out-of-stock-control` would say what the oracle thinks, and the key travels inside the
brief a future agent reads. It is a rule rather than a mechanism, and it is written here because
there is nothing that can enforce it.

Every control mission is unavailable for a different reason: stock, budget, a product that does
not exist, an unpublished category, an unpublished attribute, a withdrawn product. A suite whose
controls are all built the same way teaches an agent to recognise the device rather than the
merchant.

## Mission order is part of the workload

Version 1 listed the eight purchasable missions and then the six controls, so the ground truth
was a step function of the call index: an executor holding one integer counter scored fourteen
out of fourteen on its first ever run without reading the catalog. An independent review found
it. No more than two consecutive missions share an expected outcome now, which a test enforces,
and reordering is why the suite is at version 2.

## What is not here

Nothing simulates the Merchant Compiler, which does not exist. These missions exercise the
structured commerce foundation as it is, and compiler before and after datasets come later.
