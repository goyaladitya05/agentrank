"""What a merchant evaluation workspace is made of, before any of it is stored.

Pure domain code. No SQLAlchemy, no FastAPI, no clock, no model and no network, so the same
source snapshot and the same configuration always produce the same workspace.

A workspace is the answer to one question: what does AgentRank have to build before it can
measure a merchant it has never measured. Until now the answer was an operator writing two JSON
documents by hand, which is fine for a world somebody authored on purpose and is the whole
reason a real merchant needed a developer before their first evaluation.

Three vocabularies live here.

`BootstrapConfiguration` is everything about the generation that is a choice rather than
evidence. It is frozen into the workspace identity, so two workspaces built from one snapshot
under different choices are two different workspaces and neither is the other's replacement.

`MissionFamily` is the shape of a generated mission. It is a label on what was built and never
an instruction to build one: a family appears in a suite only when the merchant's own frozen
catalog supports it, and a family the catalog cannot support is reported rather than invented.

`BootstrapBlocker` is a refusal a merchant can read. Every one of them names a specific thing
about the evidence, because "setup failed" is not something anybody can act on.

What is deliberately absent is a generation seed. Sampling here is a stable total order over
content followed by a bounded take, so there is nothing random to seed, and a seed would be a
knob that changes which benchmark a merchant is measured against without changing any evidence
about them.
"""

import hashlib
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from agentrank_api.benchmark.definitions import MAX_KEY_LENGTH, MAX_MISSIONS
from agentrank_api.benchmark.identity import HASH_ALGORITHM, canonical_json
from agentrank_api.errors import AgentRankError

# Which generator built a workspace. It is part of the configuration digest, so a change to what
# this package produces gives every later workspace a new identity rather than quietly making an
# old one irreproducible. Bump it when the generated catalog or the generated missions change
# meaning, never for a comment or a rename.
GENERATOR_VERSION = "workspace-v1"

# A first evaluation is bounded because it is executed one mission at a time against a paid
# model provider. The default is a suite an operator can read in one screen and a merchant can
# wait for, and the ceiling is far below what a published suite may hold, because a bootstrap
# that generated two hundred missions from a large catalog would be spending a merchant's quota
# on a number nobody chose.
DEFAULT_MISSION_BUDGET = 12
MIN_MISSION_BUDGET = 2
MAX_MISSION_BUDGET = 40

# How many units a multi-unit mission asks for. Two rather than a larger number, because the
# point is to exercise a quantity at all and every extra unit narrows which merchants can
# support the family.
MULTI_UNIT_QUANTITY = 2

# The largest quantity a stock-limited abstention mission may ask for. A merchant whose shelf
# holds eighty of something would otherwise produce a mission asking for eighty one, which is
# genuinely impossible and is not a thing any buyer would ask for.
MAX_STOCK_ABSTENTION_QUANTITY = 6

_MISSION_BUDGET_CEILING = min(MAX_MISSION_BUDGET, MAX_MISSIONS)


class MissionFamily(StrEnum):
    """The shape of one generated mission, as a label on what the merchant's data supported.

    Each member says which fact about the frozen evaluation catalog made the mission possible,
    and every one of them is decided before a buyer runs. None of them is a difficulty rating
    and none of them is read by the evaluator, which marks a mission against its typed
    constraints exactly as it marks an authored one.

    `POLICY_CONSTRAINT` is here and is never produced. A mission oracle states whether a
    purchase is available or no acceptable purchase exists, and the answer to a policy question
    is neither, so this benchmark has nothing to mark one against. Listing it is how a merchant
    is told that rather than left to notice the absence.
    """

    CATEGORY_PURCHASE = "CATEGORY_PURCHASE"
    BUDGET_CONSTRAINED_PURCHASE = "BUDGET_CONSTRAINED_PURCHASE"
    MULTI_UNIT_PURCHASE = "MULTI_UNIT_PURCHASE"
    SPECIFICATION_PURCHASE = "SPECIFICATION_PURCHASE"
    BUDGET_ABSTENTION = "BUDGET_ABSTENTION"
    STOCK_ABSTENTION = "STOCK_ABSTENTION"
    UNAVAILABLE_ABSTENTION = "UNAVAILABLE_ABSTENTION"
    SPECIFICATION_ABSTENTION = "SPECIFICATION_ABSTENTION"
    POLICY_CONSTRAINT = "POLICY_CONSTRAINT"


# The order families are drawn from when a mission budget cannot hold all of them. Purchases and
# abstentions alternate, so a small budget still produces a suite that measures both completing
# a purchase and correctly declining one rather than whichever family the catalog happened to
# support most of.
FAMILY_ORDER: tuple[MissionFamily, ...] = (
    MissionFamily.CATEGORY_PURCHASE,
    MissionFamily.BUDGET_ABSTENTION,
    MissionFamily.BUDGET_CONSTRAINED_PURCHASE,
    MissionFamily.UNAVAILABLE_ABSTENTION,
    MissionFamily.SPECIFICATION_PURCHASE,
    MissionFamily.STOCK_ABSTENTION,
    MissionFamily.MULTI_UNIT_PURCHASE,
    MissionFamily.SPECIFICATION_ABSTENTION,
)

# Why one family produced nothing, in this repository's own words rather than a stack trace.
POLICY_UNSUPPORTED = (
    "A mission is marked on whether a purchase was available, so an answer to a policy question"
    " is not something this benchmark can mark. Policy text is still given to the buyer as"
    " merchant information."
)
NO_SUPPORTING_EVIDENCE = (
    "This merchant's frozen evaluation catalog supports no mission of this shape."
)


@dataclass(frozen=True, slots=True)
class BootstrapConfiguration:
    """Every choice a bootstrap makes that is not read off the merchant's evidence.

    Small on purpose. Each field here is something a workspace has to be reproducible against,
    and a field nothing reads would be one more thing an operator could change without changing
    what anybody is measured on.
    """

    mission_budget: int = DEFAULT_MISSION_BUDGET
    generator_version: str = GENERATOR_VERSION

    def __post_init__(self) -> None:
        if not MIN_MISSION_BUDGET <= self.mission_budget <= _MISSION_BUDGET_CEILING:
            raise ValueError(
                f"a mission budget must be between {MIN_MISSION_BUDGET} and"
                f" {_MISSION_BUDGET_CEILING}, got {self.mission_budget}"
            )
        if not self.generator_version.strip():
            raise ValueError("a bootstrap generator version must not be blank")
        if len(self.generator_version) > MAX_KEY_LENGTH:
            raise ValueError(f"a generator version must be at most {MAX_KEY_LENGTH} characters")

    def to_payload(self) -> dict[str, Any]:
        """The configuration as it is stored, shown and hashed."""
        return {
            "mission_budget": self.mission_budget,
            "generator_version": self.generator_version,
        }

    @property
    def digest(self) -> str:
        """The labelled digest that makes two different configurations two workspaces."""
        body = hashlib.sha256(canonical_json(self.to_payload()).encode("utf-8"))
        return f"{HASH_ALGORITHM}:{body.hexdigest()}"


@dataclass(frozen=True, slots=True)
class BootstrapBlocker:
    """One specific reason a workspace cannot be built from this merchant's evidence.

    A code and a sentence, exactly as an evaluation launch blocker is. The code is stable and
    machine readable; the sentence is what a merchant reads, and it names the thing about their
    own evidence that has to change.
    """

    code: str
    message: str


class BootstrapRefusedError(AgentRankError):
    """Generation could not honestly produce a workspace from this evidence.

    Carries the blocker rather than a message, so the preflight can render the same refusal the
    command would raise. This is a domain refusal and never a bug: a merchant whose source
    describes nothing purchasable has evidence this generator cannot build a benchmark from, and
    that is an answer rather than a failure.
    """

    def __init__(self, blocker: BootstrapBlocker) -> None:
        super().__init__(blocker.message)
        self.blocker = blocker


@dataclass(frozen=True, slots=True)
class FamilyComposition:
    """How many missions of one family a generated suite holds, and what they expect."""

    family: MissionFamily
    missions: int
    purchase_available: int
    no_acceptable_purchase: int

    def to_payload(self) -> dict[str, Any]:
        return {
            "family": self.family.value,
            "missions": self.missions,
            "purchase_available": self.purchase_available,
            "no_acceptable_purchase": self.no_acceptable_purchase,
        }


@dataclass(frozen=True, slots=True)
class UnsupportedFamily:
    """One mission family this merchant's evidence did not support, and why not."""

    family: MissionFamily
    reason: str

    def to_payload(self) -> dict[str, Any]:
        return {"family": self.family.value, "reason": self.reason}


def workspace_key(merchant_slug: str, suffix: str) -> str:
    """A stable slug identifying one merchant's generated artifact, bounded to a key length.

    A benchmark fixture key and a benchmark suite key are globally unique with their version, so
    two merchants sharing one would collide on a constraint rather than on a name nobody reads.
    The merchant slug is what keeps them apart, and slugs are long enough that the obvious
    concatenation can exceed the key bound.

    When it does, the slug is truncated and a digest of the whole slug is appended, so two
    merchants whose slugs agree on the first characters still get different keys. When it does
    not, the key is the plain concatenation, which is what an operator reading a report wants to
    see.
    """
    candidate = f"{merchant_slug}{suffix}"
    if len(candidate) <= MAX_KEY_LENGTH:
        return candidate
    digest = hashlib.sha256(merchant_slug.encode("utf-8")).hexdigest()[:8]
    keep = MAX_KEY_LENGTH - len(suffix) - len(digest) - 1
    if keep < 1:
        raise ValueError(f"suffix {suffix!r} leaves no room for a merchant slug")
    head = merchant_slug[:keep].rstrip("-")
    if not head:
        raise ValueError(f"merchant slug {merchant_slug!r} cannot be shortened into a key")
    return f"{head}-{digest}{suffix}"


# A generated mission key. Built from a family and an ordinal rather than from anything the
# merchant wrote, for the same reason the objectives are: a key is read back on every surface
# and a merchant string in one would be merchant text in a place nothing bounds.
_MISSION_KEY = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")


def mission_key(family: MissionFamily, ordinal: int) -> str:
    """The stable key of one generated mission within its suite."""
    key = f"{family.value.lower().replace('_', '-')}-{ordinal:02d}"
    if _MISSION_KEY.fullmatch(key) is None:  # pragma: no cover - families are fixed
        raise ValueError(f"generated mission key {key!r} is not a slug")
    return key
