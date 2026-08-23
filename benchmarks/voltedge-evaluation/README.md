# VoltEdge semantic evaluation

`voltedge-evaluation@1` is an independently authored evaluation workload. It is
separate from `voltedge-core@2`, which remains the development benchmark unchanged.

The catalog and source were frozen before the suite/oracles were written. The suite author used
the authoritative catalog only, not compiler output, buyer traces, or previous live outcomes.
Every purchasable mission's simulated value is the cheapest quantity-adjusted qualifying catalog
line. Controls have concrete, varied reasons: absent size or colour, unavailable stock quantity,
budget, inactive sale state, and unmet semantic thresholds.

`source.json` is normal merchant information with no benchmark fields. The compiler receives only
that source snapshot. The buyer receives one mission brief and its selected raw or compiled
projection; neither path receives this suite's oracle.
