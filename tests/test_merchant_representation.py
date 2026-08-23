"""Representation boundary tests: source truth is not Commerce IR, and neither is a benchmark."""

from dataclasses import replace
from pathlib import Path

import pytest
from benchmark_support import suite
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from agentrank_api.benchmark.repository import BenchmarkRunRepository
from agentrank_api.benchmark.suites import BenchmarkSuiteService
from agentrank_api.commerce.repository import MerchantRepository
from agentrank_api.errors import ConflictError
from agentrank_api.representation.definitions import ValueState
from agentrank_api.representation.fixtures import RepresentationFixtureError, read_ir, read_source
from agentrank_api.representation.service import MerchantRepresentationService

pytestmark = pytest.mark.anyio

SOURCE_PATH = Path("benchmarks/voltedge/source.json")
IR_PATH = Path("benchmarks/voltedge/commerce_ir.json")


async def merchant(session: AsyncSession) -> None:
    source = read_source(SOURCE_PATH)
    await MerchantRepository(session).create(slug=source.merchant_slug, name="VoltEdge")
    await session.commit()


async def test_source_and_ir_are_immutable_versioned_artifacts(session: AsyncSession) -> None:
    await merchant(session)
    service = MerchantRepresentationService(session)
    source_definition = read_source(SOURCE_PATH)
    source = await service.publish_source(source_definition)
    assert (await service.publish_source(source_definition)).id == source.id

    with pytest.raises(ConflictError) as raised:
        await service.publish_source(
            replace(source_definition, policy_text={"shipping": "changed"})
        )
    assert raised.value.reason == "source_definition_changed"

    ir = await service.publish_ir(read_ir(IR_PATH))
    with pytest.raises(DBAPIError, match="immutable"):
        await session.execute(
            text("UPDATE commerce_representation SET producer_version = 'changed' WHERE id = :id"),
            {"id": ir.id},
        )
    await session.rollback()


def test_hashes_are_deterministic_and_include_provenance() -> None:
    source = read_source(SOURCE_PATH)
    reordered = replace(source, policy_text=dict(reversed(list(source.policy_text.items()))))
    assert source.content_hash == reordered.content_hash
    ir = read_ir(IR_PATH)
    different_evidence = replace(
        ir.products[0].title.provenance[0], excerpt="merchant supplied title"
    )
    changed_evidence = replace(
        ir,
        products=(
            replace(
                ir.products[0],
                title=replace(ir.products[0].title, provenance=(different_evidence,)),
            ),
            *ir.products[1:],
        ),
    )
    assert changed_evidence.content_hash != ir.content_hash


def test_ir_keeps_false_and_unknown_distinct_and_hides_review_metadata() -> None:
    ir = read_ir(IR_PATH)
    charger = ir.products[0].variants[0]
    cable = ir.products[1].variants[1]
    assert charger.compatibility["usb-c-pd"].value == ValueState.UNKNOWN
    assert cable.availability.value == ValueState.FALSE
    projection = ir.buyer_projection()
    serialized = str(projection)
    assert "provenance" not in serialized
    assert "review_state" not in serialized
    assert "confidence" not in serialized


def test_fixture_schemas_refuse_benchmark_fields(tmp_path: Path) -> None:
    path = tmp_path / "source.json"
    path.write_text(
        '{"key":"shop","version":1,"merchant_slug":"shop","products":[],"policy_text":{},"expected_outcome":"PURCHASE"}',
        encoding="utf-8",
    )
    with pytest.raises(RepresentationFixtureError, match="unsupported"):
        read_source(path)


async def test_a_benchmark_run_can_pin_the_exact_merchant_representation(
    session: AsyncSession,
) -> None:
    await merchant(session)
    service = MerchantRepresentationService(session)
    await service.publish_source(read_source(SOURCE_PATH))
    representation = await service.publish_ir(read_ir(IR_PATH))
    owner = await MerchantRepository(session).get_by_slug("voltedge")
    assert owner is not None
    published = await BenchmarkSuiteService(session).publish(suite(merchant_slug="voltedge"))
    run = await BenchmarkRunRepository(session).create(
        merchant=owner, suite=published, representation=representation
    )
    await session.commit()
    assert run.representation_id == representation.id
