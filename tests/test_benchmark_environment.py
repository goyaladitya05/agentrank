"""Benchmark world registration and preparation: identity, fail closed refusals, isolation.

The tests that matter most here are the last two groups. A benchmark whose worlds leak into each
other produces order dependent results, and the failure is silent: every number still looks like
a number. So the properties are asserted against real commerce state produced by the real
services rather than against a helper that promises to reset something.
"""

import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from benchmark_support import mission, suite
from commerce_support import CURRENCY, build_shop
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from agentrank_api.benchmark.environment import BenchmarkEnvironmentService
from agentrank_api.benchmark.fixtures import BenchmarkFixture, fixture_content_hash
from agentrank_api.benchmark.repository import (
    BenchmarkEnvironmentRepository,
    BenchmarkRunRepository,
)
from agentrank_api.benchmark.runner import BenchmarkRunService
from agentrank_api.benchmark.suites import BenchmarkSuiteService
from agentrank_api.checkout.execution import CheckoutExecutionService
from agentrank_api.checkout.service import CheckoutItem, CheckoutService, NewCheckout
from agentrank_api.commerce.catalog_fixture import SeedProduct, SeedVariant
from agentrank_api.commerce.repository import CatalogRepository, MerchantRepository
from agentrank_api.constraints.repository import IntentConstraintRepository
from agentrank_api.constraints.rules import ConstraintOperator, IntentConstraintSpec
from agentrank_api.errors import ConflictError, NotFoundError
from agentrank_api.inventory.models import ReservationStatus
from agentrank_api.inventory.repository import InventoryReservationRepository
from agentrank_api.mandates.repository import MandateRepository
from agentrank_api.payments.admission import PaymentAdmissionService

pytestmark = pytest.mark.anyio

SLUG = "test-world"
HOUR = timedelta(hours=1)


def variant(sku: str = "TW-1", *, stock: int = 5, price: int = 100000) -> SeedVariant:
    return SeedVariant(
        sku=sku,
        label="Black",
        price_amount_minor=price,
        currency=CURRENCY,
        inventory_quantity=stock,
        attributes={"color": "black"},
    )


def fixture(
    *variants: SeedVariant,
    key: str = "test-world-catalog",
    version: int = 1,
    slug: str = SLUG,
    title: str = "Charger",
) -> BenchmarkFixture:
    return BenchmarkFixture(
        key=key,
        version=version,
        merchant_slug=slug,
        merchant_name="Test World",
        products=(
            SeedProduct(
                external_id="TW-CHG",
                title=title,
                description=None,
                category="chargers",
                variants=variants or (variant(),),
            ),
        ),
    )


async def stock_of(session: AsyncSession, merchant_id: uuid.UUID, sku: str) -> int:
    found = await CatalogRepository(session).get_variant_by_sku(merchant_id, sku)
    assert found is not None
    return found.inventory_quantity


# Fixture identity.


def test_a_fixture_hashes_its_own_content() -> None:
    assert fixture().content_hash == fixture_content_hash(fixture())
    assert fixture().content_hash.startswith("sha256:")


def test_every_field_a_mission_can_read_is_in_the_identity() -> None:
    """A field an author could change without the version noticing is the one bug here.

    Price, stock, attributes and activity are the obvious ones. Title is in it too, and that is
    deliberate rather than accidental: the merchant's own search matches on prose, so editing a
    title changes what a buyer can find.
    """
    base = fixture()

    moved = {
        "price": fixture(variant(price=200000)),
        "stock": fixture(variant(stock=1)),
        "sku": fixture(variant("TW-2")),
        "title": fixture(title="Charger Pro"),
        "version": fixture(key="test-world-catalog", version=2),
    }
    for name, changed in moved.items():
        assert changed.content_hash != base.content_hash, name

    attributes = replace(variant(), attributes={"color": "blue"})
    assert fixture(attributes).content_hash != base.content_hash
    inactive = replace(variant(), is_active=False)
    assert fixture(inactive).content_hash != base.content_hash


def test_a_fixture_refuses_a_fractional_attribute() -> None:
    """JSONB stores a JSON number as numeric, so a float can come back as a different float.

    That would move the catalog pin of a run against a world nobody edited.
    """
    fractional = replace(variant(), attributes={"length_m": 1.5})

    with pytest.raises(ValueError, match="fractional value"):
        fixture(fractional)


def test_a_fixture_refuses_two_variants_sharing_a_sku() -> None:
    with pytest.raises(ValueError, match="SKUs must be unique"):
        fixture(variant("TW-1"), variant("TW-1"))


# Registration.


async def test_registering_creates_the_merchant_and_records_the_fixture(
    session: AsyncSession,
) -> None:
    defined = fixture()

    environment = await BenchmarkEnvironmentService(session).register(defined)

    merchant = await MerchantRepository(session).get_by_slug(SLUG)
    assert merchant is not None
    assert environment.merchant_id == merchant.id
    assert environment.fixture_key == defined.key
    assert environment.fixture_version == defined.version
    assert environment.fixture_hash == defined.content_hash
    assert environment.label == "test-world-catalog@1"


async def test_registering_the_same_fixture_twice_returns_the_same_world(
    session: AsyncSession,
) -> None:
    service = BenchmarkEnvironmentService(session)

    first = await service.register(fixture())
    second = await service.register(fixture())

    assert first.id == second.id


async def test_registering_an_edited_fixture_under_one_version_is_refused(
    session: AsyncSession,
) -> None:
    """The whole reproducibility guarantee on the target side.

    A historical run names this row. If an edited catalog could take its identity, every result
    measured against it would quietly start meaning something else.
    """
    service = BenchmarkEnvironmentService(session)
    await service.register(fixture())

    with pytest.raises(ConflictError) as raised:
        await service.register(fixture(variant(stock=99)))

    assert raised.value.reason == "fixture_definition_changed"


async def test_a_new_version_of_an_edited_fixture_is_a_new_world(session: AsyncSession) -> None:
    service = BenchmarkEnvironmentService(session)
    first = await service.register(fixture())

    second = await service.register(fixture(variant(stock=99), version=2))

    assert second.id != first.id
    assert second.merchant_id == first.merchant_id


async def test_a_world_cannot_be_registered_for_a_merchant_it_does_not_describe(
    session: AsyncSession,
) -> None:
    """A fixture describes one merchant's catalog, exactly as a suite names one merchant.

    Unreachable through the service, which finds the merchant by the slug the fixture itself
    names, so this is asserted where the rule lives. Preparation overwrites whatever merchant it
    is pointed at, and the one thing that must never be arguable is which merchant that is.
    """
    other = await MerchantRepository(session).create(slug="other-shop", name="Other")
    await session.commit()

    with pytest.raises(ValueError, match="cannot be registered for"):
        await BenchmarkEnvironmentRepository(session).create(merchant=other, fixture=fixture())


# Fail closed preparation.


async def test_preparing_an_unregistered_merchant_is_refused(session: AsyncSession) -> None:
    """The production safety property. Preparation overwrites a catalog and releases stock.

    A merchant nobody deliberately registered as a benchmark target must not be reachable, and
    the refusal has to come before anything is written.
    """
    await build_shop(session, SLUG)

    with pytest.raises(NotFoundError, match="benchmark_environment"):
        await BenchmarkEnvironmentService(session).prepare(fixture())


async def test_preparing_an_edited_fixture_is_refused(session: AsyncSession) -> None:
    """Registered under one identity, applied under another, is exactly the silent edit."""
    service = BenchmarkEnvironmentService(session)
    await service.register(fixture())

    with pytest.raises(ConflictError) as raised:
        await service.prepare(fixture(variant(stock=99)))

    assert raised.value.reason == "fixture_definition_changed"


async def test_preparation_leaves_an_unregistered_merchant_untouched(
    session: AsyncSession,
) -> None:
    shop = await build_shop(session, "real-shop", inventory=7)

    with pytest.raises(NotFoundError):
        await BenchmarkEnvironmentService(session).prepare(fixture(slug="real-shop"))

    await session.rollback()
    assert await stock_of(session, shop.merchant_id, "REAL-SHOP-BLACK") == 7


# Preparation.


async def test_preparing_seeds_the_world_the_fixture_describes(session: AsyncSession) -> None:
    service = BenchmarkEnvironmentService(session)
    await service.register(fixture())

    prepared = await service.prepare(fixture())

    assert prepared.catalog.products == 1
    assert prepared.catalog.variants == 1
    assert prepared.released_holds == 0
    assert await stock_of(session, prepared.environment.merchant_id, "TW-1") == 5


async def test_preparing_puts_consumed_stock_back(session: AsyncSession) -> None:
    """The contamination that matters. A sale takes units off the shelf permanently."""
    service = BenchmarkEnvironmentService(session)
    await service.register(fixture())
    prepared = await service.prepare(fixture())
    merchant_id = prepared.environment.merchant_id

    found = await CatalogRepository(session).get_variant_by_sku(merchant_id, "TW-1")
    assert found is not None
    found.inventory_quantity = 1
    await session.commit()

    await service.prepare(fixture())

    assert await stock_of(session, merchant_id, "TW-1") == 5


async def test_preparing_gives_back_stock_an_unfinished_mission_was_holding(
    session: AsyncSession,
) -> None:
    """A hold left standing is capacity the next mission would silently be short of."""
    service = BenchmarkEnvironmentService(session)
    await service.register(fixture())
    prepared = await service.prepare(fixture())
    merchant_id = prepared.environment.merchant_id
    checkout_id = await _held_quote(session, merchant_id, quantity=2)

    outcome = await service.prepare(fixture())

    assert outcome.released_holds == 1
    reservations = await InventoryReservationRepository(session).list_for_checkout(checkout_id)
    assert [held.status for held in reservations] == [ReservationStatus.RELEASED]


async def test_preparing_refuses_while_a_payment_is_still_holding_stock(
    session: AsyncSession,
) -> None:
    """Releasing a committed hold is an operator abandonment with residual risk attached.

    A benchmark preparing its next mission must never make that decision by accident, so this
    refuses and names the command that resolves it.
    """
    service = BenchmarkEnvironmentService(session)
    await service.register(fixture())
    prepared = await service.prepare(fixture())
    merchant_id = prepared.environment.merchant_id
    checkout_id = await _held_quote(session, merchant_id)
    admission = await PaymentAdmissionService(session).admit_payment(
        checkout_id, merchant_id=merchant_id, idempotency_key="benchmark-hold-1"
    )
    assert admission.attempt is not None, admission.refusal
    await session.commit()

    with pytest.raises(ConflictError) as raised:
        await service.prepare(fixture())

    assert raised.value.reason == "payment_in_progress"


async def _held_quote(
    session: AsyncSession, merchant_id: uuid.UUID, *, quantity: int = 1
) -> uuid.UUID:
    """A quote holding stock in a prepared world, through the real commerce services.

    A mandate and its constraint set are created directly here rather than through the
    benchmark executor, which does not exist yet at this point in the phase.
    """
    at = datetime.now(UTC)
    variant_row = await CatalogRepository(session).get_variant_by_sku(merchant_id, "TW-1")
    assert variant_row is not None
    mandate = await MandateRepository(session).create(
        merchant_id=merchant_id,
        max_total_amount_minor=variant_row.price_amount_minor * quantity,
        currency=CURRENCY,
        valid_from=at - HOUR,
        valid_until=at + HOUR,
    )
    await IntentConstraintRepository(session).create(
        merchant_id=merchant_id,
        mandate_id=mandate.id,
        specs=[IntentConstraintSpec.required_attribute("color", ConstraintOperator.EQ, "black")],
    )
    await session.commit()

    checkout = await CheckoutService(session).create_checkout(
        NewCheckout(
            merchant_id=merchant_id,
            mandate_id=mandate.id,
            items=(CheckoutItem(variant_id=variant_row.id, quantity=quantity),),
            expires_at=at + HOUR,
        )
    )
    readiness = await CheckoutExecutionService(session).prepare_execution(
        checkout.id, merchant_id=merchant_id
    )
    assert readiness.ready
    return checkout.id


# Database invariants.


async def test_a_registered_world_cannot_be_updated_or_deleted(session: AsyncSession) -> None:
    """A historical run says which world produced it. Re-pointing one rewrites what it meant."""
    environment = await BenchmarkEnvironmentService(session).register(fixture())
    # Read before the first rollback, which expires every attribute on the object.
    environment_id = environment.id

    with pytest.raises(DBAPIError, match="published benchmark definitions are immutable"):
        await session.execute(
            text("UPDATE benchmark_environment SET fixture_version = 2 WHERE id = :id"),
            {"id": environment_id},
        )
    await session.rollback()

    with pytest.raises(DBAPIError, match="published benchmark definitions are immutable"):
        await session.execute(
            text("DELETE FROM benchmark_environment WHERE id = :id"), {"id": environment_id}
        )
    await session.rollback()


async def test_one_fixture_version_names_one_world(session: AsyncSession) -> None:
    """Globally, not per merchant. A fixture names the merchant it describes."""
    await BenchmarkEnvironmentService(session).register(fixture())
    other = await MerchantRepository(session).create(slug="other-shop", name="Other")
    await session.flush()

    with pytest.raises(IntegrityError, match="uq_benchmark_environment_version"):
        await session.execute(
            text(
                "INSERT INTO benchmark_environment"
                " (id, merchant_id, fixture_key, fixture_version, fixture_hash)"
                " VALUES (:id, :merchant_id, 'test-world-catalog', 1, :hash)"
            ),
            {
                "id": uuid.uuid7(),
                "merchant_id": other.id,
                "hash": fixture().content_hash,
            },
        )
    await session.rollback()


async def test_a_run_cannot_claim_another_merchants_world(session: AsyncSession) -> None:
    """Merchant isolation on the target side, held by a composite foreign key.

    Knowing an environment identifier is worth nothing to a merchant that does not own it, and
    that is structural rather than a check somebody has to remember to write.
    """
    environment = await BenchmarkEnvironmentService(session).register(fixture())
    environment_id = environment.id
    outsider = await MerchantRepository(session).create(slug="test-merchant", name="Outsider")
    stored = await BenchmarkSuiteService(session).publish(
        suite(mission("one"), merchant_slug="test-merchant")
    )

    with pytest.raises(ValueError, match="belongs to another merchant"):
        await BenchmarkRunRepository(session).create(
            merchant=outsider, suite=stored, environment=environment
        )
    await session.rollback()

    # And the refusal is the database's rather than the repository's, so it holds for a writer
    # that went around the application entirely.
    with pytest.raises(IntegrityError, match="fk_benchmark_run_environment"):
        await session.execute(
            text(
                "INSERT INTO benchmark_run"
                " (id, merchant_id, suite_id, environment_id, status)"
                " VALUES (:id, :merchant_id, :suite_id, :environment_id, 'PENDING')"
            ),
            {
                "id": uuid.uuid7(),
                "merchant_id": outsider.id,
                "suite_id": stored.id,
                "environment_id": environment_id,
            },
        )
    await session.rollback()


async def test_a_run_records_the_world_it_was_measured_against(session: AsyncSession) -> None:
    """The third pin. The suite says which missions and the catalog hash says what was there.

    Neither says which authored target it was supposed to be, which is what makes a historical
    comparison across a fixture edit unattributable without this.
    """
    service = BenchmarkEnvironmentService(session)
    await service.register(fixture(slug="test-merchant", key="test-merchant-catalog"))
    prepared = await service.prepare(fixture(slug="test-merchant", key="test-merchant-catalog"))
    await BenchmarkSuiteService(session).publish(
        suite(mission("one", budget_minor=100000, value_minor=100000))
    )

    run = await BenchmarkRunService(session).start_run(
        suite_key="test-suite",
        suite_version=1,
        merchant_slug="test-merchant",
        environment=prepared.environment,
    )

    assert run.environment_id == prepared.environment.id


async def test_a_run_without_a_registered_world_says_so(session: AsyncSession) -> None:
    """Null means nobody registered a target, never that the target was fine."""
    await build_shop(session, "test-merchant")
    await BenchmarkSuiteService(session).publish(suite(mission("one")))

    run = await BenchmarkRunService(session).start_run(
        suite_key="test-suite", suite_version=1, merchant_slug="test-merchant"
    )

    assert run.environment_id is None
