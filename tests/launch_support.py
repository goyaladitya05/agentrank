"""One merchant with everything an evaluation launch needs, built through the real services.

Two worlds, because there are two commands and they need different things.

`build_initial_world` is a merchant AgentRank has just been pointed at: a registered benchmark
world, a published suite authored against it, and one source snapshot recording the merchant's
own information. Nothing is compiled and nothing has ever been measured, which is exactly the
state a first evaluation exists for.

`build_launch_world` is that merchant one loop later: the source compiled and the representation
published, which is what a re-evaluation freezes.

The catalog and the source describe the same product and the same SKU, and the source states a
wattage in prose that the catalog does not publish as an attribute. That is the shape the whole
product loop is about: a benchmark finds an attribute a buyer could not read, the compiler has a
proposal at exactly that address, and a re-evaluation measures the representation that resolves
it.
"""

import uuid
from dataclasses import dataclass

from benchmark_support import mission, suite
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession

from agentrank_api.benchmark.authored import AuthoredWorld
from agentrank_api.benchmark.definitions import BenchmarkMissionDefinition
from agentrank_api.benchmark.environment import BenchmarkEnvironmentService
from agentrank_api.benchmark.fixtures import BenchmarkFixture
from agentrank_api.benchmark.launch import MerchantEvaluationLaunchService
from agentrank_api.benchmark.models import BenchmarkEnvironment, BenchmarkSuite
from agentrank_api.benchmark.report import ExecutorReport
from agentrank_api.benchmark.runner import BenchmarkRunService
from agentrank_api.benchmark.suites import BenchmarkSuiteService
from agentrank_api.commerce.catalog_fixture import SeedProduct, SeedVariant
from agentrank_api.commerce.repository import MerchantRepository
from agentrank_api.compiler.service import MerchantCompilerService
from agentrank_api.config import Settings
from agentrank_api.constraints.rules import ConstraintOperator
from agentrank_api.mandates.intent import RequiredAttribute
from agentrank_api.representation.definitions import (
    MerchantSourceDefinition,
    SourceProduct,
    SourceVariant,
)
from agentrank_api.representation.models import CommerceRepresentation
from agentrank_api.representation.service import MerchantRepresentationService

PRICE = 499900
CURRENCY = "INR"
STOCK = 4

# The mission every launch world publishes: it requires a wattage the catalog does not carry,
# so a run against this world produces an attribute finding with a compiler address behind it.
WATTAGE = RequiredAttribute("wattage", 100, ConstraintOperator.GTE)


def external_id(slug: str) -> str:
    return f"{slug}-charger"


def sku(slug: str) -> str:
    return f"{slug}-black".upper()


def world_fixture(slug: str) -> BenchmarkFixture:
    """The catalog a run puts this merchant back to, with no published wattage."""
    return BenchmarkFixture(
        key=f"{slug}-catalog",
        version=1,
        merchant_slug=slug,
        merchant_name=slug,
        products=(
            SeedProduct(
                external_id=external_id(slug),
                title="Charger",
                description=None,
                category="chargers",
                variants=(
                    SeedVariant(
                        sku=sku(slug),
                        label="Black",
                        price_amount_minor=PRICE,
                        currency=CURRENCY,
                        inventory_quantity=STOCK,
                        attributes={"color": "black"},
                    ),
                ),
            ),
        ),
    )


def world_source(slug: str, *, version: int = 1) -> MerchantSourceDefinition:
    """The merchant's own words about the same product, stating the wattage in prose."""
    return MerchantSourceDefinition(
        key=f"{slug}-source",
        version=version,
        merchant_slug=slug,
        products=(
            SourceProduct(
                external_id=external_id(slug),
                title="Charger",
                description="A three-port charger rated to 100W.",
                category="chargers",
                variants=(
                    SourceVariant(
                        sku=sku(slug),
                        label="Black",
                        price_amount_minor=PRICE,
                        currency=CURRENCY,
                        inventory_quantity=STOCK,
                        merchant_metadata={"finish": "black"},
                    ),
                ),
                merchant_metadata={},
            ),
        ),
        policy_text={},
    )


@dataclass(frozen=True, slots=True)
class InitialWorld:
    """A merchant with benchmark machinery and no compiler output of any kind.

    The identifiers are held as plain values beside the rows they came from, for the reason
    `LaunchWorld` states.
    """

    merchant_id: uuid.UUID
    merchant_slug: str
    suite_id: uuid.UUID
    suite_key: str
    suite_version: int
    environment_id: uuid.UUID
    fixture: BenchmarkFixture
    environment: BenchmarkEnvironment
    suite: BenchmarkSuite
    source_snapshot_id: uuid.UUID
    authored: AuthoredWorld


@dataclass(frozen=True, slots=True)
class LaunchWorld:
    """Everything a launch resolves, so a test can assert what was frozen against it.

    The identifiers are held as plain values beside the rows they came from. The dispatcher
    deliberately ends its own transaction partway through, which expires every loaded instance,
    and a test that read an attribute off one afterwards would be asserting through a lazy load
    rather than against the value it set up.
    """

    merchant_id: uuid.UUID
    merchant_slug: str
    representation_id: uuid.UUID
    suite_id: uuid.UUID
    suite_key: str
    suite_version: int
    environment_id: uuid.UUID
    fixture: BenchmarkFixture
    environment: BenchmarkEnvironment
    suite: BenchmarkSuite
    source_snapshot_id: uuid.UUID
    compiler_run_id: uuid.UUID
    representation: CommerceRepresentation
    # The operator side documents a dispatcher is given: the catalog a run puts this merchant
    # back to, beside the workload it was authored against.
    authored: AuthoredWorld


async def build_initial_world(
    session: AsyncSession,
    slug: str = "first-shop",
    *,
    missions: tuple[BenchmarkMissionDefinition, ...] = (),
    source_version: int = 1,
    with_source: bool = True,
) -> InitialWorld:
    """Register the world, publish the suite, and record the merchant's own information.

    `with_source` off is a merchant AgentRank has benchmark machinery for and no record of, which
    is the one thing a first evaluation is refused for.
    """
    world = world_fixture(slug)
    environments = BenchmarkEnvironmentService(session)
    await environments.register(world)
    await environments.prepare(world)
    definition = suite(
        *(missions or (mission("buy-a-charger", constraints=(WATTAGE,)),)),
        key=f"{slug}-suite",
        merchant_slug=slug,
    )
    published = await BenchmarkSuiteService(session).publish(definition)
    merchant = await MerchantRepository(session).get_by_slug(slug)
    assert merchant is not None
    snapshot_id = uuid.uuid7()
    if with_source:
        snapshot = await MerchantRepresentationService(session).publish_source(
            world_source(slug, version=source_version)
        )
        snapshot_id = snapshot.id
    registered = await environments.require_registered(world)
    return InitialWorld(
        merchant_id=merchant.id,
        merchant_slug=slug,
        suite_id=published.id,
        suite_key=published.suite_key,
        suite_version=published.version,
        environment_id=registered.id,
        fixture=world,
        environment=registered,
        suite=published,
        source_snapshot_id=snapshot_id,
        authored=AuthoredWorld(fixture=world, suite=definition),
    )


async def build_launch_world(
    session: AsyncSession,
    slug: str = "relaunch-shop",
    *,
    missions: tuple[BenchmarkMissionDefinition, ...] = (),
    source_version: int = 1,
) -> LaunchWorld:
    """Register the world, publish the suite, compile the source and publish its representation."""
    initial = await build_initial_world(
        session, slug, missions=missions, source_version=source_version
    )
    compiler = MerchantCompilerService(session)
    compiler_run = await compiler.run(initial.merchant_id, initial.source_snapshot_id)
    representation = await compiler.publish(initial.merchant_id, compiler_run.id)
    return LaunchWorld(
        merchant_id=initial.merchant_id,
        merchant_slug=slug,
        representation_id=representation.id,
        suite_id=initial.suite_id,
        suite_key=initial.suite_key,
        suite_version=initial.suite_version,
        environment_id=initial.environment_id,
        fixture=initial.fixture,
        environment=initial.environment,
        suite=initial.suite,
        source_snapshot_id=initial.source_snapshot_id,
        compiler_run_id=compiler_run.id,
        representation=representation,
        authored=initial.authored,
    )


def without_providers(settings: Settings) -> Settings:
    """Settings with no model provider configured.

    Pinned rather than inherited, because a developer machine with a provider key in `.env` and
    a CI runner without one would otherwise resolve different buyers and these tests would prove
    whatever the environment happened to hold.
    """
    return settings.model_copy(update={"openai_api_key": None, "gemini_api_key": None})


def with_openai(settings: Settings) -> Settings:
    """Settings that look like a deployment with an OpenAI runtime credential.

    The value is synthetic and never leaves the test database or this process: nothing here
    reaches a provider, and the launch path records only the requested model.
    """
    return settings.model_copy(
        update={"openai_api_key": SecretStr("test-openai-key"), "gemini_api_key": None}
    )


def with_gemini(settings: Settings) -> Settings:
    """Settings that look like a deployment with only a Gemini runtime credential."""
    return settings.model_copy(
        update={"openai_api_key": None, "gemini_api_key": SecretStr("test-gemini-key")}
    )


async def complete_run(session: AsyncSession, world: LaunchWorld | InitialWorld) -> uuid.UUID:
    """One completed benchmark run for this merchant, with every mission attempted and none won.

    Enough for the one thing callers need it for: whether this merchant has any usable benchmark
    evidence at all, which is what decides whether a first evaluation is still the command they
    are making. The reports are empty, so every mission is marked by the ordinary evaluator
    against the ordinary oracle and the run completes with a real, poor result rather than with
    a status somebody wrote by hand.
    """
    runs = BenchmarkRunService(session)
    started = await runs.start_run(
        suite_key=world.suite_key,
        suite_version=world.suite_version,
        merchant_slug=world.merchant_slug,
        environment=world.environment,
    )
    for definition in world.authored.suite.missions:
        await runs.start_mission(started.id, definition.key, merchant_id=world.merchant_id)
        await runs.record_result(
            started.id,
            definition.key,
            ExecutorReport(world.merchant_id),
            merchant_id=world.merchant_id,
        )
    finished = await runs.complete_run(started.id, merchant_id=world.merchant_id)
    return finished.id


async def queue_launch(
    session: AsyncSession,
    settings: Settings,
    world: LaunchWorld | InitialWorld,
    *,
    request_key: str,
) -> uuid.UUID:
    """One admitted launch, through the merchant-facing service the console calls.

    The preflight is read first and its digest and purpose carried into the request, which is
    what the console does and what admission checks. A test that skipped it would be admitting a
    plan nobody was shown.
    """
    service = MerchantEvaluationLaunchService(session, settings)
    plan = await service.plan(world.merchant_id)
    launch = await service.request(
        world.merchant_id,
        purpose=plan.purpose,
        representation_id=plan.representation_id,
        request_key=request_key,
        plan_digest=plan.digest,
    )
    return launch.id
