"""Application errors that cross layer boundaries, and how they appear on the wire.

Services raise these. Routes do not catch them; `create_app` installs handlers that turn
them into responses, so a route stays three lines long and every layer keeps raising the
same error for the same situation.

`ErrorResponse` lives here rather than in a module of its own so that an error and its
serialized form stay together, and so that routes can reference it without importing from
`main` and creating a cycle.
"""

from pydantic import BaseModel


class AgentRankError(Exception):
    """Base class for every error this application raises deliberately."""


class NotFoundError(AgentRankError):
    """A resource was addressed by an identifier that does not exist."""

    def __init__(self, resource: str, identifier: str) -> None:
        super().__init__(f"{resource} {identifier} was not found")
        self.resource = resource
        self.identifier = identifier


class ConflictError(AgentRankError):
    """A well formed request that the current state of the system refuses.

    Different from `NotFoundError`, where the thing addressed does not exist, and
    different from a validation failure, where the request itself is wrong. An inactive
    variant and an insufficient stock level are neither: the request is well formed and
    the resource is there, and the answer would have been different an hour ago.

    `reason` is a stable machine readable code, not prose, for the same reason event
    types are: a buyer agent has to tell "out of stock" from "no longer sold" without
    reading English.
    """

    def __init__(
        self,
        reason: str,
        detail: str,
        *,
        resource: str | None = None,
        identifier: str | None = None,
    ) -> None:
        super().__init__(detail)
        self.reason = reason
        self.detail = detail
        self.resource = resource
        self.identifier = identifier


class ErrorResponse(BaseModel):
    """The body of every deliberate error response.

    Machine readable on purpose. An AI buyer agent needs to tell "this product does not
    exist" apart from "the request was malformed" without parsing prose.
    """

    error: str
    detail: str
    resource: str | None = None
    identifier: str | None = None
