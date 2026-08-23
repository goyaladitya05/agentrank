"""Independent semantic evaluation world checks, separate from voltedge-core@2."""

from itertools import pairwise
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from agentrank_api.benchmark.authored import publish_world, read_world
from agentrank_api.benchmark.catalog import satisfies
from agentrank_api.benchmark.definitions import ExpectedOutcome
from agentrank_api.benchmark.runner import BenchmarkRunService
from agentrank_api.commerce.repository import MerchantRepository
from agentrank_api.compiler.service import MerchantCompilerService
from agentrank_api.representation.fixtures import read_source
from agentrank_api.representation.projection import raw_projection
from agentrank_api.representation.service import MerchantRepresentationService

pytestmark = pytest.mark.anyio

DIRECTORY = Path("benchmarks/voltedge-evaluation")
WORLD = read_world(DIRECTORY)
SOURCE = read_source(DIRECTORY / "source.json")


def test_evaluation_world_is_separate_and_substantially_broader_than_development_world() -> None:
    assert WORLD.fixture.label == "voltedge-evaluation-catalog@1"
    assert WORLD.suite.label == "voltedge-evaluation@1"
    assert SOURCE.label == "voltedge-evaluation-source@1"
    assert len(WORLD.fixture.products) > 20
    assert len(WORLD.suite.missions) == 18
    assert len(SOURCE.products) == len(WORLD.fixture.products)


def test_source_is_catalog_coherent_and_carries_no_oracle_shape() -> None:
    catalog = {product.external_id: product for product in WORLD.fixture.products}
    assert {product.external_id for product in SOURCE.products} == set(catalog)
    for source_product in SOURCE.products:
        fixture_product = catalog[source_product.external_id]
        assert source_product.title == fixture_product.title
        assert source_product.description == fixture_product.description
        assert source_product.category == fixture_product.category
        fixture_variants = {variant.sku: variant for variant in fixture_product.variants}
        assert {variant.sku for variant in source_product.variants} == set(fixture_variants)
        for source_variant in source_product.variants:
            fixture_variant = fixture_variants[source_variant.sku]
            assert (
                source_variant.price_amount_minor,
                source_variant.currency,
                source_variant.inventory_quantity,
            ) == (
                fixture_variant.price_amount_minor,
                fixture_variant.currency,
                fixture_variant.inventory_quantity,
            )
    forbidden = {"expected_outcome", "simulated_value", "simulated_value_amount_minor"}
    assert not _keys(SOURCE.payload()) & forbidden


async def test_oracles_and_simulated_values_are_recomputed_from_evaluation_catalog(
    session: AsyncSession,
) -> None:
    await publish_world(session, WORLD)
    merchant = await MerchantRepository(session).get_by_slug(WORLD.merchant_slug)
    assert merchant is not None
    entries = await BenchmarkRunService(session).catalog(merchant.id)

    for mission in WORLD.suite.missions:
        qualifying = [entry for entry in entries if satisfies(mission.brief, entry)]
        assert bool(qualifying) is (
            mission.oracle.expected_outcome is ExpectedOutcome.PURCHASE_AVAILABLE
        )
        expected_value = min(
            (entry.price_amount_minor * mission.brief.quantity for entry in qualifying), default=0
        )
        assert mission.oracle.simulated_value_amount_minor == expected_value


def test_suite_order_and_controls_do_not_disclose_answers() -> None:
    outcomes = [mission.oracle.expected_outcome for mission in WORLD.suite.missions]
    longest, current = 1, 1
    for previous, outcome in pairwise(outcomes):
        current = current + 1 if outcome is previous else 1
        longest = max(longest, current)
    assert longest <= 2
    assert longest == 2
    giveaways = ("control", "impossible", "unavailable", "out-of-stock", "fail", "nothing")
    assert not any(word in mission.key for mission in WORLD.suite.missions for word in giveaways)
    assert sum(outcome is ExpectedOutcome.PURCHASE_AVAILABLE for outcome in outcomes) == 10
    assert sum(outcome is ExpectedOutcome.NO_ACCEPTABLE_PURCHASE for outcome in outcomes) == 8


async def test_evaluation_source_compiles_without_benchmark_inputs(session: AsyncSession) -> None:
    await publish_world(session, WORLD)
    merchant = await MerchantRepository(session).get_by_slug(WORLD.merchant_slug)
    assert merchant is not None
    source = await MerchantRepresentationService(session).publish_source(SOURCE)
    compiler = MerchantCompilerService(session)
    run = await compiler.run(merchant.id, source.id)
    representation = await compiler.publish(merchant.id, run.id)

    assert representation.source_snapshot_id == source.id
    assert representation.compiler_run_id == run.id
    assert "expected_outcome" not in str(representation.payload)
    assert "simulated_value" not in str(raw_projection(source))


def _keys(value: Any) -> set[str]:
    if isinstance(value, dict):
        return set(value) | set().union(*(_keys(item) for item in value.values()))
    if isinstance(value, list):
        return set().union(*(_keys(item) for item in value))
    return set()
