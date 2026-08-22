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
replays prepared reports and a future LLM buyer all satisfy it, and none of them can tell from
the signature which of the others it is standing beside.

What it returns is an `ExecutorReport` rather than an evaluator input, and that is the trust
boundary rather than a naming choice. An executor names identifiers and actions; trusted
orchestration establishes what they came to from the merchant's own rows. There is no
implementation of this protocol that can state a price, a quoted total, an authorization decision
or a payment status, because the type it returns has nowhere to put one.

`ExecutorIdentity` is what produced a result. A benchmark whose historical runs cannot say which
strategy produced them is a benchmark that silently compares two different things: change how
the reference executor selects a candidate and every earlier run keeps its numbers while new
ones are produced differently, with nothing on either to show it.

The kind and the version are declared, because what matters to a reader is whether somebody says
the behavior changed. The revision beside them is derived, because the failure a declaration
cannot catch is the one nobody meant, and `implementation_revision` moves whether or not anybody
remembers to. Neither is a substitute for the other: a digest cannot say a change mattered, and a
version cannot say a change happened.
"""

import hashlib
import re
import uuid
from collections.abc import Awaitable
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Protocol

from agentrank_api.benchmark.definitions import KEY_PATTERN, MAX_KEY_LENGTH, AgentMissionBrief
from agentrank_api.benchmark.report import ExecutorReport

_KEY = re.compile(KEY_PATTERN)

# The same labelled digest shape every other identity in this schema stores, so one check
# constraint pattern describes a suite hash, a fixture hash, an evaluator version and this.
REVISION_PATTERN = r"^sha256:[0-9a-f]{64}$"

_REVISION = re.compile(REVISION_PATTERN)


@dataclass(frozen=True, slots=True)
class BenchmarkRunCapability:
    """The one active benchmark world a trusted commerce caller may mutate.

    This remains pure execution vocabulary so a trusted executor can forward it to a buyer
    surface without importing the run repository or any evaluator-side benchmark code.
    """

    merchant_id: uuid.UUID
    run_id: uuid.UUID


@dataclass(frozen=True, slots=True)
class ExecutorIdentity:
    """Which strategy produced a benchmark result, which version of it, and what it actually was.

    `kind` names the strategy in the same lowercase hyphenated slug shape every other machine
    readable name in this system uses. `version` is declared rather than derived, and bumping it
    is the deliberate act of saying that results produced before it are not comparable with
    results produced after.

    `revision` is what the declaration cannot do. A version is a promise a person keeps, and the
    failure it cannot catch is the one nobody meant: edit how a candidate is selected, leave the
    number alone, and every future run stamps `reference-v1` while buying something different.
    The digest moves whether or not anybody remembers to, so two runs of one suite that disagree
    can at least be told apart afterwards. It is optional because a strategy may not have a
    derivable one, and null means nobody recorded it rather than that nothing changed.

    It is not automatic semantic versioning and this is deliberately not claimed to be. A digest
    says two runs were produced by different code; it does not say the behaviour changed, and it
    cannot say the behaviour is the same when it moves for a comment. The version is still the
    statement a reader needs and this is the evidence beside it.

    There is no model identifier here and no provider. Neither exists yet. When they do, the
    natural revision for a model buyer is a digest over what actually decides its behaviour, the
    model, the prompt, the tool schema and the sampling configuration, which is the same
    question this field already asks.
    """

    kind: str
    version: int
    revision: str | None = None

    def __post_init__(self) -> None:
        if len(self.kind) > MAX_KEY_LENGTH:
            raise ValueError(f"an executor kind must be at most {MAX_KEY_LENGTH} characters")
        if _KEY.fullmatch(self.kind) is None:
            raise ValueError(f"an executor kind must match {KEY_PATTERN}, got {self.kind!r}")
        if self.version < 1:
            raise ValueError(f"an executor version must be at least 1, got {self.version}")
        if self.revision is not None and _REVISION.fullmatch(self.revision) is None:
            raise ValueError(
                f"an executor revision must match {REVISION_PATTERN}, got {self.revision!r}"
            )

    @property
    def label(self) -> str:
        """How this executor is named in a report or on a command line.

        `reference-v1` rather than `reference@1`, so an executor is never mistaken at a glance
        for a suite or a fixture, which both use the `key@version` form.
        """
        return f"{self.kind}-v{self.version}"


def implementation_revision(*modules: ModuleType) -> str:
    """A labelled digest of the source of the modules that decide one executor's behaviour.

    The smallest honest thing that moves on its own. It is a digest of source bytes, so it moves
    for a comment as readily as for a rewritten selection rule, which is why it is recorded
    beside a declared version rather than instead of one: it detects drift and does not classify
    it.

    What it covers is exactly the modules named. For the scripted buyer that is its own module,
    where the selection rule, the candidate assessment and the abstention rule all live. What it
    does not cover is the shared comparison vocabulary those rules call into, so a change to how
    an attribute is compared moves the behaviour without moving this. That limit is written down
    in docs/shortcomings.md rather than papered over by hashing half the application.
    """
    digest = hashlib.sha256()
    for module in sorted(modules, key=lambda named: named.__name__):
        source = Path(module.__file__ or "")
        digest.update(module.__name__.encode("utf-8"))
        digest.update(source.read_bytes())
    return f"sha256:{digest.hexdigest()}"


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
    ) -> Awaitable[ExecutorReport]: ...
