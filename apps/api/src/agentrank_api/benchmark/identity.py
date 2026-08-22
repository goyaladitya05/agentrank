"""The content hash that gives a suite definition an identity independent of where it lives.

A benchmark result is only meaningful if the workload behind it can be reproduced. A version
number alone does not give that, because a version number is a promise a person keeps: edit
the fixture, leave the number alone, and every historical result quietly starts meaning
something else. The hash is what makes that impossible to do by accident. Publishing a suite
whose key and version already exist under a different hash is refused, so an edit either
changes nothing or forces a new version.

What is hashed is what changes the measurement:

```text
hashed       suite key, version, merchant slug
hashed       every mission, in order: key, objective, quantity, budget, hard constraints,
             preferences, expected outcome, simulated value
not hashed   the suite's display name
not hashed   database identifiers and timestamps
```

The display name is excluded because it is a label. Correcting a typo in it does not change
what any agent sees or what any evaluator decides, and forcing a version bump for it would
push authors towards editing published versions in place, which is the failure this hash
exists to prevent. Identifiers and timestamps are excluded because they are properties of a
row rather than of a workload; including them would give one definition a different identity
in every database.

Canonicalisation is JSON with sorted keys, no insignificant whitespace and no NaN. Mission
order is preserved rather than sorted, because the order missions are presented in is part of
the workload. The digest is labelled with its algorithm for the same reason a credential
verifier is: a stored hash that does not say how it was produced cannot be replaced later
without guessing what the existing rows are.
"""

import hashlib
import json
from typing import Any

from agentrank_api.benchmark.definitions import BenchmarkSuiteDefinition

HASH_ALGORITHM = "sha256"

# `sha256:` and sixty four hexadecimal characters, the same shape and the same length as the
# credential verifier, so one check constraint pattern describes both kinds of stored digest.
HASH_PATTERN = r"^sha256:[0-9a-f]{64}$"

HASH_LENGTH = 71


def canonical_payload(definition: BenchmarkSuiteDefinition) -> dict[str, Any]:
    """The semantically relevant content of one suite, as a plain JSON object.

    Separate from the hash so that a test can assert what is inside it rather than only that
    two digests differ. A field that belongs in the identity and is missing here is a field an
    author could change without the version noticing, which is the one bug this module can
    have.
    """
    return {
        "key": definition.key,
        "version": definition.version,
        "merchant_slug": definition.merchant_slug,
        "missions": [mission.to_payload() for mission in definition.missions],
    }


def canonical_json(payload: dict[str, Any]) -> str:
    """One serialization for one payload.

    `sort_keys` is what makes the digest independent of the order a dictionary happened to be
    built in. `separators` removes the whitespace that would otherwise differ between Python
    versions and pretty printers. `allow_nan` is off because neither NaN nor infinity is JSON,
    so a value that reached here would produce a digest over something no database could
    store.
    """
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )


def suite_content_hash(definition: BenchmarkSuiteDefinition) -> str:
    """The labelled digest of one suite definition's semantically relevant content."""
    digest = hashlib.sha256(canonical_json(canonical_payload(definition)).encode("utf-8"))
    return f"{HASH_ALGORITHM}:{digest.hexdigest()}"
