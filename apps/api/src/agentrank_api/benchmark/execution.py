"""The seam a benchmark mission is carried out through, and who carried it out.

Pure domain code. No SQLAlchemy, no HTTP, no commerce service and no model, so this module can
be imported by an executor without the executor thereby gaining a route to anything the
evaluator knows.

That is the entire point of it being separate. `MissionExecutor` used to live beside the run
service, which imports the oracle side of the benchmark to mark results with. An executor
importing its own protocol from there would have `evaluate_mission` and `satisfies` one
attribute access away, and "the executor cannot see the oracle" would rest on nobody reaching
for them. Here there is nothing to reach for.

Two things live here.

`MissionExecutor` is what carries a mission out. It receives an `AgentMissionBrief` and the
merchant to shop, and nothing else: no suite, no run identifier, no oracle, no other mission and
nothing about what any earlier mission did. A scripted reference executor, a fixture that
replays prepared results and a future LLM buyer all satisfy it, and none of them can tell from
the signature which of the others it is standing beside.

`ExecutorIdentity` is what produced a result. A benchmark whose historical runs cannot say which
strategy produced them is a benchmark that silently compares two different things: change how
the reference executor selects a candidate and every earlier run keeps its numbers while new
ones are produced differently, with nothing on either to show it. It is deliberately a declared
kind and version rather than a git commit, because what matters is whether the behavior changed
and a commit moves for a comment.
"""

import re
import uuid
from collections.abc import Awaitable
from dataclasses import dataclass
from typing import Protocol

from agentrank_api.benchmark.definitions import KEY_PATTERN, MAX_KEY_LENGTH, AgentMissionBrief
from agentrank_api.benchmark.observation import ObservedResult

_KEY = re.compile(KEY_PATTERN)


@dataclass(frozen=True, slots=True)
class ExecutorIdentity:
    """Which strategy produced a benchmark result, and which version of it.

    `kind` names the strategy in the same lowercase hyphenated slug shape every other machine
    readable name in this system uses. `version` is declared rather than derived, and bumping it
    is the deliberate act of saying that results produced before it are not comparable with
    results produced after.

    There is no model identifier here and no provider. Neither exists, and a nullable column for
    one would be a guess at the shape of an agent that has not been built.
    """

    kind: str
    version: int

    def __post_init__(self) -> None:
        if len(self.kind) > MAX_KEY_LENGTH:
            raise ValueError(f"an executor kind must be at most {MAX_KEY_LENGTH} characters")
        if _KEY.fullmatch(self.kind) is None:
            raise ValueError(f"an executor kind must match {KEY_PATTERN}, got {self.kind!r}")
        if self.version < 1:
            raise ValueError(f"an executor version must be at least 1, got {self.version}")

    @property
    def label(self) -> str:
        """How this executor is named in a report or on a command line.

        `reference-v1` rather than `reference@1`, so an executor is never mistaken at a glance
        for a suite or a fixture, which both use the `key@version` form.
        """
        return f"{self.kind}-v{self.version}"


class MissionExecutor(Protocol):
    """Whatever carries a mission out.

    One call, and it receives a brief rather than a mission, so an executor cannot reach the
    oracle even by accident. The merchant is passed alongside because an executor has to know
    which catalog it is shopping.

    `identity` is what the run records. It is on the executor rather than passed to the runner
    beside it, because an argument at a call site is a label somebody can get wrong and an
    attribute on the thing that did the work is not.
    """

    @property
    def identity(self) -> ExecutorIdentity: ...

    def __call__(
        self, brief: AgentMissionBrief, *, merchant_id: uuid.UUID
    ) -> Awaitable[ObservedResult]: ...
