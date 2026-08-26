"""An evaluation measures an artifact that describes the world it is run in, or it is refused.

A run has two halves that come from different places. The world is the isolated catalog the buyer
transacts in and the oracle is computed from, which for a merchant with an evaluation workspace
is the catalog that workspace generated from one source snapshot. The merchant information handed
to the buyer as context is the artifact under test, which is a source snapshot for a first
evaluation and a published Commerce IR for a re-evaluation.

The two used to be resolved independently, and a merchant who refreshed their source between
building a setup and being measured got a buyer told about a shop it was not standing in. Nothing
was corrupted in storage: the run recorded exactly what it did. What was wrong was the
measurement, and the merchant was the party it was attributed to.

```text
world says      the charger costs 499900 and there are ten
artifact says   the charger costs 249900
buyer           quotes 249900, is charged 499900, breaks its own budget, is marked wrong
```

Both halves of the fix are here. A first evaluation freezes the workspace's own snapshot, so the
state is unreachable rather than refused. A re-evaluation is refused by name when the published
representation states a price, an availability or a SKU the frozen world contradicts, because
recompiling a newer source is the ordinary product loop and the artifact it produces is only
sometimes a description of a different shop.
"""

import uuid
from dataclasses import replace

import pytest
from launch_support import without_providers
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from workspace_support import catalogued, product, source, variant

from agentrank_api.benchmark.evaluation_launch import EvaluationPurpose
from agentrank_api.benchmark.experiment import CompilerImpactExperimentService
from agentrank_api.benchmark.grounding import (
    VariantFacts,
    contradictions,
    representation_facts,
    world_facts,
)
from agentrank_api.benchmark.launch import MerchantEvaluationLaunchService
from agentrank_api.benchmark.models import BenchmarkEnvironment
from agentrank_api.commerce.repository import MerchantRepository
from agentrank_api.compiler.service import MerchantCompilerService
from agentrank_api.config import Settings
from agentrank_api.representation.definitions import MerchantSourceDefinition
from agentrank_api.representation.models import CommerceRepresentation
from agentrank_api.representation.service import MerchantRepresentationService
from agentrank_api.workspace.service import MerchantEvaluationWorkspaceService

pytestmark = pytest.mark.anyio


def _catalog(price: int, stock: int) -> MerchantSourceDefinition:
    """One product, one variant, so the only thing that varies is what is under test."""
    return source(
        product(
            "CHG",
            variant("CHG-1", label="Black", price=price, stock=stock, metadata={"finish": "black"}),
            title="Charger",
            description="A three-port charger rated to 100W.",
            category="chargers",
        ),
        slug="grounding-shop",
    )


async def _built(session: AsyncSession, price: int = 499900, stock: int = 10) -> uuid.UUID:
    merchant = await MerchantRepository(session).create(
        slug="grounding-shop", name="Grounding Shop"
    )
    await session.commit()
    snapshot = await MerchantRepresentationService(session).publish_source(_catalog(price, stock))
    await MerchantEvaluationWorkspaceService(session).bootstrap(
        merchant.id, source_snapshot_id=snapshot.id
    )
    return merchant.id


async def _publish(
    session: AsyncSession, merchant_id: uuid.UUID, definition: MerchantSourceDefinition
) -> None:
    """Compile and publish a representation from one source snapshot, the ordinary way."""
    snapshot = await MerchantRepresentationService(session).publish_source(definition)
    compiler = MerchantCompilerService(session)
    run = await compiler.run(merchant_id, snapshot.id)
    await compiler.publish(merchant_id, run.id)


async def test_a_representation_compiled_from_the_same_evidence_is_launchable(
    catalog_settings: Settings, session: AsyncSession
) -> None:
    """The ordinary loop. Recompiling and publishing the setup's own source contradicts nothing."""
    merchant_id = await _built(session)
    await _publish(session, merchant_id, _catalog(499900, 10))

    plan = await MerchantEvaluationLaunchService(session, without_providers(catalog_settings)).plan(
        merchant_id
    )
    assert plan.purpose is EvaluationPurpose.REEVALUATION
    assert plan.launchable, [blocker.code for blocker in plan.blockers]


async def test_a_representation_stating_another_price_is_refused_by_name(
    catalog_settings: Settings, session: AsyncSession
) -> None:
    merchant_id = await _built(session, price=499900)
    await _publish(session, merchant_id, replace(_catalog(249900, 10), version=2))

    plan = await MerchantEvaluationLaunchService(session, without_providers(catalog_settings)).plan(
        merchant_id
    )
    refusal = {blocker.code: blocker.message for blocker in plan.blockers}
    assert plan.launchable is False
    assert "CHG-1" in refusal["representation_measures_another_catalog"]


async def test_a_representation_withdrawing_a_line_the_world_sells_is_refused(
    catalog_settings: Settings, session: AsyncSession
) -> None:
    merchant_id = await _built(session, stock=10)
    await _publish(session, merchant_id, replace(_catalog(499900, 0), version=2))

    plan = await MerchantEvaluationLaunchService(session, without_providers(catalog_settings)).plan(
        merchant_id
    )
    assert "representation_measures_another_catalog" in [blocker.code for blocker in plan.blockers]


async def test_an_operator_authored_world_has_no_pair_to_compare_and_is_not_refused(
    catalog_settings: Settings, session: AsyncSession
) -> None:
    """A world nobody generated has no source snapshot behind it, so there is nothing to check."""
    merchant = await MerchantRepository(session).create(slug="authored-shop", name="Authored")
    await session.commit()
    await _publish(
        session, merchant.id, replace(catalogued("authored-shop"), merchant_slug="authored-shop")
    )
    plan = await MerchantEvaluationLaunchService(session, without_providers(catalog_settings)).plan(
        merchant.id
    )
    assert "representation_measures_another_catalog" not in [
        blocker.code for blocker in plan.blockers
    ]


def test_an_unknown_availability_in_a_representation_contradicts_nothing() -> None:
    """A representation that says it does not know is not a representation that disagrees."""
    world = {"S": VariantFacts(price_amount_minor=100, currency="INR", purchasable=True)}
    stated = {"S": VariantFacts(price_amount_minor=100, currency="INR", purchasable=None)}
    assert contradictions(world, stated) == ()


def test_a_world_line_the_artifact_omits_is_not_a_contradiction() -> None:
    """An incomplete description is a worse discovery surface rather than a false one."""
    world = {
        "A": VariantFacts(price_amount_minor=100, currency="INR", purchasable=True),
        "B": VariantFacts(price_amount_minor=200, currency="INR", purchasable=True),
    }
    assert contradictions(world, {"A": world["A"]}) == ()


def test_the_two_readers_agree_on_a_world_and_a_representation_that_match() -> None:
    catalog = {
        "products": [
            {
                "is_active": True,
                "variants": [
                    {
                        "sku": "S",
                        "price_amount_minor": 100,
                        "currency": "INR",
                        "inventory_quantity": 3,
                        "is_active": True,
                    }
                ],
            }
        ]
    }
    representation = {
        "products": [
            {
                "variants": [
                    {
                        "sku": "S",
                        "price": {"value": {"amount_minor": 100, "currency": "INR"}},
                        "availability": {"value": "TRUE"},
                    }
                ]
            }
        ]
    }
    assert contradictions(world_facts(catalog), representation_facts(representation)) == ()


async def test_a_controlled_experiment_refuses_the_same_drift_a_launch_does(
    catalog_settings: Settings, session: AsyncSession
) -> None:
    """Both arms are handed merchant information and both transact in one world.

    Every lineage rule the experiment already enforced binds the representation to the source
    snapshot and both to the merchant. None of them bound either to the environment, so an
    operator could pair a world generated from one snapshot with a representation compiled from
    another: both arms would be told a price the shelf does not hold, both would break their own
    budgets, and the experiment would report a compiler comparison of the drift.
    """
    merchant_id = await _built(session, price=499900)
    await _publish(session, merchant_id, replace(_catalog(249900, 10), version=2))
    workspace = await MerchantEvaluationWorkspaceService(session).current_summary(merchant_id)
    assert workspace is not None
    environment = await session.get(BenchmarkEnvironment, workspace.environment_id)
    representation = (
        await session.execute(
            select(CommerceRepresentation)
            .where(CommerceRepresentation.merchant_id == merchant_id)
            .order_by(CommerceRepresentation.write_order.desc())
            .limit(1)
        )
    ).scalar_one()
    assert environment is not None

    with pytest.raises(ValueError, match="this benchmark environment does not hold"):
        await CompilerImpactExperimentService(session).create(
            merchant_id=merchant_id,
            suite_id=workspace.suite_id,
            environment=environment,
            source_snapshot_id=representation.source_snapshot_id,
            compiled_representation_id=representation.id,
            buyer_configuration={},
            buyer_configuration_digest=f"sha256:{'0' * 64}",
            sample_count=1,
        )
