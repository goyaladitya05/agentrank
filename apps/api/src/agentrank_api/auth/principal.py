"""Who a request has proved itself to be.

Its own module so that a route, a service and the authentication code can all name this type
without any of them importing the others.
"""

import uuid
from dataclasses import dataclass

from agentrank_api.benchmark.execution import BenchmarkRunCapability


@dataclass(frozen=True, slots=True)
class AuthenticatedMerchant:
    """One authenticated caller, with stable identity and an optional benchmark capability.

    Frozen, and deliberately two plain UUIDs rather than the credential row it came from. Three
    reasons, and each of them is a bug that would otherwise be possible:

    - a mapped object handed to a route is a mapped object a route can write through
    - a mapped object expires on rollback, and several services in this application roll back
      deliberately. A principal that stopped being readable partway through a refusal would be
      a principal that could not be recorded in the refusal
    - a credential row carries a verifier, and a verifier has no business travelling into a
      service, a schema or an audit payload

    `merchant_id` is the authorization boundary: it is what every merchant scoped query is
    filtered by, and it comes from the credential row rather than from anything the caller sent.

    `credential_id` is evidence, not identity. It says which key authorized a request. It does
    not say who holds that key, and nothing in this system claims it does: a credential is a
    machine credential and a machine credential is not a person. See docs/security.md.

    `benchmark_capability` exists only when the authenticated credential was issued by the
    benchmark runner for the currently persisted run. It carries no mutable credential state and
    lets mutation services distinguish the active run's own worker from an external caller.
    """

    merchant_id: uuid.UUID
    credential_id: uuid.UUID
    benchmark_capability: BenchmarkRunCapability | None = None
