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


class AuthenticationError(AgentRankError):
    """A request that did not establish which merchant it is acting for.

    Deliberately carries nothing. There is no field for which credential was presented, no
    field for how far verification got and no constructor parameter that could become one,
    because every one of those distinctions is a distinction worth keeping: whether a
    credential identifier exists, whether a key was revoked, whether a secret was merely wrong.

    One error for six situations. A missing header, a header in another scheme, a malformed
    token, an unknown credential, a revoked credential and a wrong secret all raise this and
    all produce the same response, which is what makes them indistinguishable from outside
    rather than merely undocumented.

    It is not a `NotFoundError` and not a `ConflictError`. Those two say something about a
    resource; this one says something about the caller, and the caller is nobody yet.
    """

    reason = "unauthenticated"
    detail = "a valid merchant API credential is required"

    def __init__(self) -> None:
        super().__init__(self.detail)


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


class UpstreamError(AgentRankError):
    """An external system this application depends on did not give a usable answer.

    Its own class rather than a `ConflictError`, and the distinction is not cosmetic. A conflict
    says the request was fine and the state refused it, which tells a caller to change something
    or to wait for the state to change. This says the request was fine, the state was fine, and
    a system neither party controls did not cooperate. The next move is to try again later, and
    a caller that could not tell the two apart would keep editing a request that was never wrong.

    Answered as 502 rather than 409 for the same reason. It is also deliberately not a 500: this
    application did not fail, and a caller reading its own monitoring should be able to see the
    difference between a bug here and a gateway that timed out.

    `reason` is a stable machine readable code and `detail` is a sentence this repository wrote.
    Nothing an upstream said reaches either. A vendor's prose in this application's error body
    is a vendor's prose in a buyer agent's parser, and it changes without notice.
    """

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(detail)
        self.reason = reason
        self.detail = detail


class ErrorResponse(BaseModel):
    """The body of every deliberate error response.

    Machine readable on purpose. An AI buyer agent needs to tell "this product does not
    exist" apart from "the request was malformed" without parsing prose.
    """

    error: str
    detail: str
    resource: str | None = None
    identifier: str | None = None


class InvalidField(BaseModel):
    """Where one request body was wrong, and what was wrong with it.

    `location` and `message` and nothing else. In particular not the value: it is the caller's
    own, so returning it leaks nothing, and it is also the caller's own in size and in shape,
    which is the whole problem. The framework's default handler serializes it, recursing once
    per nesting level of whatever arrived, and a body of a thousand open brackets is a thousand
    frames inside an exception handler that no `try` in the request path is outside of.
    """

    location: list[str]
    message: str


class InvalidRequestResponse(ErrorResponse):
    """A request body this application could not read, said in a way an agent can act on.

    Extends the ordinary error shape rather than replacing it, so a caller that already parses
    `error` and `detail` needs no second parser, and one that wants to fix a specific field has
    `fields` to read instead of a sentence.
    """

    fields: list[InvalidField]
