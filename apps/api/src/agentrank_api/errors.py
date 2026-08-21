"""Application errors that cross layer boundaries.

Services raise these. Routes do not catch them; `create_app` installs handlers that turn
them into responses, so a route stays three lines long and every layer keeps raising the
same error for the same situation.
"""


class AgentRankError(Exception):
    """Base class for every error this application raises deliberately."""


class NotFoundError(AgentRankError):
    """A resource was addressed by an identifier that does not exist."""

    def __init__(self, resource: str, identifier: str) -> None:
        super().__init__(f"{resource} {identifier} was not found")
        self.resource = resource
        self.identifier = identifier
