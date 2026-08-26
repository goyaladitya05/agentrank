"""Reading an authored benchmark world from operator files, and publishing it.

The authored definitions live in `benchmarks/` at the top of the repository and deliberately
not in this package. That is the whole point of this module existing: a benchmark mission's
expected outcome is the answer key, and until now it was Python in the package a buyer process
runs from, so a worker could `import agentrank_api.benchmark.voltedge` and read every mission's
ground truth indexed by the key it had just been handed. An independent test audit proved it by
doing it. Naming conventions, private modules and comments saying "do not import this" would all
have left that import working.

So the boundary is a packaging one. `apps/api/pyproject.toml` builds exactly `src/agentrank_api`
into the distribution, `benchmarks/` is a sibling of `apps/`, and there is therefore no module,
no package data entry and no installed file through which a worker can reach an authored suite.
What is left in this package is a reader that needs to be given a path, and a worker is given an
empty working directory and an environment that names none.

```text
operator side    benchmarks/<world>/catalog.json, suite.json      the answer key
                 this reader, run by an operator command
                 immutable BenchmarkSuite and BenchmarkMission rows

worker side      one AgentMissionBrief, one merchant credential
                 the merchant's commerce API over HTTP
```

Once a world is published, executing it needs none of these files: the runner reads missions
from the rows and hands out briefs. The fixture is read again to put the world back before every
mission, which is trusted operator side work with a database credential, and is not something a
worker does or could do.

What this boundary is and is not is written down in SECURITY.md rather than overclaimed
here. It removes every ordinary runtime route to the authored source: the import system, the
installed distribution, `sys.path`, the working directory and the environment. It is not an
operating system sandbox, and in a developer checkout the repository is still an ordinary
readable directory on the same filesystem.

The format is JSON, in exactly the payload shapes the domain types already serialize themselves
to, so an authored file and a stored definition are the same document and the content hash of a
suite does not depend on which of them it was built from. Unknown keys are refused rather than
ignored, because a misspelled key in an authored file is a mission that quietly means something
else. Prose belongs in the world's README, which nothing parses.
"""

import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from agentrank_api.benchmark.definitions import (
    AgentMissionBrief,
    BenchmarkMissionDefinition,
    BenchmarkSuiteDefinition,
    ExpectedOutcome,
    MissionOracle,
)
from agentrank_api.benchmark.environment import (
    BenchmarkEnvironmentService,
    PreparedEnvironment,
)
from agentrank_api.benchmark.fixtures import BenchmarkFixture
from agentrank_api.benchmark.models import BenchmarkSuite
from agentrank_api.benchmark.suites import BenchmarkSuiteService
from agentrank_api.commerce.catalog_fixture import SeedProduct, SeedVariant

# The two documents one authored world is written in. A world is a directory rather than a file
# because the workload and the catalog it is authored against are versioned separately and are
# edited by different people at different times.
SUITE_FILE = "suite.json"
CATALOG_FILE = "catalog.json"

_SUITE_KEYS = frozenset({"key", "version", "merchant_slug", "name", "missions"})
_MISSION_KEYS = frozenset({"brief", "oracle"})
_ORACLE_KEYS = frozenset({"expected_outcome", "simulated_value_amount_minor"})
_BRIEF_KEYS = frozenset(
    {"key", "objective", "quantity", "budget", "hard_constraints", "preferences"}
)
_CATALOG_KEYS = frozenset({"key", "version", "merchant_slug", "merchant_name", "products"})
_PRODUCT_KEYS = frozenset(
    {"external_id", "title", "description", "category", "is_active", "variants"}
)
_VARIANT_KEYS = frozenset(
    {
        "sku",
        "label",
        "price_amount_minor",
        "currency",
        "inventory_quantity",
        "attributes",
        "is_active",
    }
)


class AuthoredDefinitionError(ValueError):
    """A file that is not the authored definition it is being read as.

    Named after the file rather than after the field, because an operator reading this is
    holding a path and needs to know which document to open before they need to know which key
    is wrong.
    """

    def __init__(self, path: Path, detail: str) -> None:
        super().__init__(f"{path}: {detail}")
        self.path = path
        self.detail = detail


@dataclass(frozen=True, slots=True)
class AuthoredWorld:
    """One merchant's authored world: the catalog it starts from and the workload against it.

    The two are held together because a mission oracle is a claim about a catalog, and a suite
    published against one merchant and prepared against another would be marked with ground
    truth that was never established anywhere. Both name their merchant and this refuses a pair
    that disagrees, which is the same rule the run service applies to a published suite.
    """

    fixture: BenchmarkFixture
    suite: BenchmarkSuiteDefinition

    def __post_init__(self) -> None:
        if self.fixture.merchant_slug != self.suite.merchant_slug:
            raise ValueError(
                f"the authored catalog describes {self.fixture.merchant_slug} and the suite was"
                f" authored against {self.suite.merchant_slug}"
            )

    @property
    def merchant_slug(self) -> str:
        """The merchant this world is about."""
        return self.suite.merchant_slug


def read_world(directory: Path) -> AuthoredWorld:
    """Read one authored world from a directory holding its two documents."""
    return AuthoredWorld(
        fixture=read_fixture(directory / CATALOG_FILE),
        suite=read_suite(directory / SUITE_FILE),
    )


def read_suite(path: Path) -> BenchmarkSuiteDefinition:
    """One authored suite definition, validated by the same constructors a run is marked with.

    Every value goes through `BenchmarkSuiteDefinition` and `MissionOracle`, so an authored file
    cannot express a mission the application would refuse: a control mission carrying simulated
    value, a mission worth more than its budget and a malformed key are all refused here exactly
    as they were when the definitions were Python.
    """
    document = _document(path)
    _only(path, document, _SUITE_KEYS, "suite")
    try:
        return BenchmarkSuiteDefinition(
            key=_text(path, document, "key"),
            version=_integer(path, document, "version"),
            merchant_slug=_text(path, document, "merchant_slug"),
            name=_text(path, document, "name"),
            missions=tuple(_mission(path, entry) for entry in _array(path, document, "missions")),
        )
    except (ValueError, KeyError, TypeError) as refused:
        raise AuthoredDefinitionError(path, _said(refused)) from refused


def read_fixture(path: Path) -> BenchmarkFixture:
    """One authored catalog, as the world a benchmark run puts a merchant back to."""
    document = _document(path)
    _only(path, document, _CATALOG_KEYS, "catalog")
    try:
        return BenchmarkFixture(
            key=_text(path, document, "key"),
            version=_integer(path, document, "version"),
            merchant_slug=_text(path, document, "merchant_slug"),
            merchant_name=_text(path, document, "merchant_name"),
            products=tuple(_product(path, entry) for entry in _array(path, document, "products")),
        )
    except (ValueError, KeyError, TypeError) as refused:
        raise AuthoredDefinitionError(path, _said(refused)) from refused


async def publish_world(
    session: AsyncSession, world: AuthoredWorld
) -> tuple[PreparedEnvironment, BenchmarkSuite]:
    """Register the world, put its catalog back, and publish the suite authored against it.

    Convergent in all three. Registering an unchanged fixture returns the registration that
    exists, preparing an untouched world rewrites the same values and reports nothing created,
    and publishing an unchanged suite returns the one already published. Editing either
    definition without bumping its version is refused rather than applied, which is what makes a
    historical run interpretable after the files have moved on.

    Each step commits its own work, which is the service boundary in every case.
    """
    environments = BenchmarkEnvironmentService(session)
    await environments.register(world.fixture)
    prepared = await environments.prepare(world.fixture)
    suite = await BenchmarkSuiteService(session).publish(world.suite)
    return prepared, suite


def _mission(path: Path, payload: Any) -> BenchmarkMissionDefinition:
    document = _entry(path, payload, "a mission")
    _only(path, document, _MISSION_KEYS, "mission")
    brief = _entry(path, document.get("brief"), "a mission brief")
    _only(path, brief, _BRIEF_KEYS, "mission brief")
    oracle = _entry(path, document.get("oracle"), "a mission oracle")
    _only(path, oracle, _ORACLE_KEYS, "mission oracle")
    return BenchmarkMissionDefinition(
        brief=AgentMissionBrief.from_payload(brief),
        oracle=MissionOracle(
            expected_outcome=_outcome(path, oracle),
            simulated_value_amount_minor=_integer(path, oracle, "simulated_value_amount_minor"),
        ),
    )


def _outcome(path: Path, document: dict[str, Any]) -> ExpectedOutcome:
    value = _text(path, document, "expected_outcome")
    try:
        return ExpectedOutcome(value)
    except ValueError as unknown:
        raise AuthoredDefinitionError(path, f"{value!r} is not an expected outcome") from unknown


def _product(path: Path, payload: Any) -> SeedProduct:
    document = _entry(path, payload, "a product")
    _only(path, document, _PRODUCT_KEYS, "product")
    return SeedProduct(
        external_id=_text(path, document, "external_id"),
        title=_text(path, document, "title"),
        description=_optional_text(path, document, "description"),
        category=_optional_text(path, document, "category"),
        is_active=_boolean(path, document, "is_active"),
        variants=tuple(_variant(path, entry) for entry in _array(path, document, "variants")),
    )


def _variant(path: Path, payload: Any) -> SeedVariant:
    document = _entry(path, payload, "a variant")
    _only(path, document, _VARIANT_KEYS, "variant")
    attributes = document.get("attributes", {})
    if not isinstance(attributes, dict):
        raise AuthoredDefinitionError(path, "variant attributes must be an object")
    return SeedVariant(
        sku=_text(path, document, "sku"),
        label=_optional_text(path, document, "label"),
        price_amount_minor=_integer(path, document, "price_amount_minor"),
        currency=_text(path, document, "currency"),
        inventory_quantity=_integer(path, document, "inventory_quantity"),
        attributes=dict(attributes),
        is_active=_boolean(path, document, "is_active"),
    )


def _said(refused: Exception) -> str:
    """What a refusal from a domain constructor says, without naming the file twice.

    A refusal this module already raised carries its detail and its path; wrapping it again
    would print the path inside its own message. A `KeyError` from a payload constructor is the
    one refusal that does not read as a sentence, so it is turned into one.
    """
    if isinstance(refused, AuthoredDefinitionError):
        return refused.detail
    if isinstance(refused, KeyError):
        return f"is missing the key {refused.args[0]!r}"
    return str(refused)


def _document(path: Path) -> dict[str, Any]:
    """The file, as a JSON object, or a refusal naming the file rather than the parser."""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as unreadable:
        raise AuthoredDefinitionError(path, f"could not be read: {unreadable}") from unreadable
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as malformed:
        raise AuthoredDefinitionError(path, f"is not JSON: {malformed}") from malformed
    if not isinstance(payload, dict):
        raise AuthoredDefinitionError(path, "must hold one JSON object")
    return payload


def _only(path: Path, document: dict[str, Any], allowed: Iterable[str], label: str) -> None:
    """Refuse a document carrying a key this format does not define.

    Ignoring unknown keys would make a misspelled `missions` an empty suite and a misspelled
    `inventory_quantity` a variant with no stock, both of which are authored files that read
    correctly and mean something else.
    """
    unknown = sorted(set(document) - set(allowed))
    if unknown:
        raise AuthoredDefinitionError(path, f"{label} has unknown keys: {', '.join(unknown)}")


def _entry(path: Path, payload: Any, label: str) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise AuthoredDefinitionError(path, f"{label} must be an object")
    return payload


def _array(path: Path, document: dict[str, Any], name: str) -> Sequence[Any]:
    value = document.get(name)
    if not isinstance(value, list):
        raise AuthoredDefinitionError(path, f"{name} must be an array")
    return value


def _text(path: Path, document: dict[str, Any], name: str) -> str:
    value = document.get(name)
    if not isinstance(value, str):
        raise AuthoredDefinitionError(path, f"{name} must be a string")
    return value


def _optional_text(path: Path, document: dict[str, Any], name: str) -> str | None:
    if document.get(name) is None:
        return None
    return _text(path, document, name)


def _integer(path: Path, document: dict[str, Any], name: str) -> int:
    value = document.get(name)
    if not isinstance(value, int) or isinstance(value, bool):
        raise AuthoredDefinitionError(path, f"{name} must be an integer")
    return value


def _boolean(path: Path, document: dict[str, Any], name: str) -> bool:
    value = document.get(name)
    if not isinstance(value, bool):
        raise AuthoredDefinitionError(path, f"{name} must be true or false")
    return value
