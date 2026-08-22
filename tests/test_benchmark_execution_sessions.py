"""Who owns which database session during a benchmark run, and what that buys.

The runner and the buyer used to share one. That made a mission's commerce part of the run's own
transaction sequence, and three things followed from it. An executor that left the transaction in
an aborted state broke the run service's next call, so a run that stopped that way had to be
closed from a fresh process. A surface holding one session for a whole run reads its own stale
copies of rows that world preparation has since rewritten, because a committed session does not
expire what it has already loaded. And an in process benchmark measured transaction boundaries no
buyer over the wire could ever be given, which is the opposite of what the benchmark is for.

Ownership is now explicit: the run service uses the session it was constructed with, and
`MerchantBuyerSurface` opens one per operation and closes it, exactly as `get_session` does per
HTTP request. These tests assert the consequences rather than the arrangement, because the
arrangement is a constructor argument and the consequences are what a reader depends on.
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agentrank_api.benchmark.buyer import MerchantBuyerSurface
from agentrank_api.benchmark.definitions import (
    AgentMissionBrief,
    BenchmarkMissionDefinition,
    BenchmarkSuiteDefinition,
    ExpectedOutcome,
    MissionOracle,
)
from agentrank_api.benchmark.environment import BenchmarkEnvironmentService
from agentrank_api.benchmark.fixtures import BenchmarkFixture
from agentrank_api.benchmark.lifecycle import BenchmarkRunStatus
from agentrank_api.benchmark.reference_executor import ReferenceMissionExecutor
from agentrank_api.benchmark.runner import BenchmarkRunService
from agentrank_api.benchmark.suites import BenchmarkSuiteService
from agentrank_api.commerce.catalog_fixture import SeedProduct, SeedVariant
from agentrank_api.commerce.schemas import ProductSearchRequest, ProductSearchResponse
from agentrank_api.constraints.rules import ConstraintOperator
from agentrank_api.errors import ConflictError
from agentrank_api.mandates.intent import AllowedCategory, MaxTotalAmount, RequiredAttribute
from agentrank_api.mandates.models import SpendingMandate
from agentrank_api.mandates.schemas import BuyerIntentInput, CreateMandateRequest
from agentrank_api.payments.fake import FakePaymentProvider

pytestmark = pytest.mark.anyio

CURRENCY = "INR"
PRICE = 100000
SLUG = "session-ownership-shop"
SUITE_KEY = "session-ownership-suite"
SKU = "SOS-BLK"


def world(*, stock: int = 3, price: int = PRICE, version: int = 1) -> BenchmarkFixture:
    return BenchmarkFixture(
        key="session-ownership-catalog",
        version=version,
        merchant_slug=SLUG,
        merchant_name="Session Ownership Shop",
        products=(
            SeedProduct(
                external_id="SOS-CHG",
                title="Charger",
                description=None,
                category="chargers",
                variants=(
                    SeedVariant(
                        sku=SKU,
                        label="Black",
                        price_amount_minor=price,
                        currency=CURRENCY,
                        inventory_quantity=stock,
                        attributes={"color": "black"},
                    ),
                ),
            ),
        ),
    )


WORLD = world()


def suite_of(*keys: str) -> BenchmarkSuiteDefinition:
    return BenchmarkSuiteDefinition(
        key=SUITE_KEY,
        version=1,
        merchant_slug=SLUG,
        name="Session ownership suite",
        missions=tuple(
            BenchmarkMissionDefinition(
                brief=AgentMissionBrief(
                    key=key,
                    objective="Buy one black charger.",
                    budget=MaxTotalAmount(amount_minor=PRICE, currency=CURRENCY),
                    hard_constraints=(
                        AllowedCategory("chargers"),
                        RequiredAttribute("color", "black", ConstraintOperator.EQ),
                    ),
                ),
                oracle=MissionOracle(
                    expected_outcome=ExpectedOutcome.PURCHASE_AVAILABLE,
                    simulated_value_amount_minor=PRICE,
                ),
            )
            for key in keys
        ),
    )


async def prepared(session: AsyncSession, fixture: BenchmarkFixture = WORLD) -> uuid.UUID:
    environments = BenchmarkEnvironmentService(session)
    environment = await environments.register(fixture)
    await environments.prepare(fixture)
    return environment.merchant_id


def buyer(
    sessions: async_sessionmaker[AsyncSession], merchant_id: uuid.UUID
) -> MerchantBuyerSurface:
    return MerchantBuyerSurface(sessions, merchant_id=merchant_id, provider=FakePaymentProvider())


def mandate_request() -> CreateMandateRequest:
    return CreateMandateRequest(
        max_total_amount_minor=PRICE,
        currency=CURRENCY,
        valid_until=datetime.now(UTC) + timedelta(hours=1),
        intent=BuyerIntentInput(description="Buy one black charger."),
    )


# The buyer's work is not inside the runner's transaction.


async def test_the_buyer_commits_outside_the_callers_transaction(
    session: AsyncSession, factory: async_sessionmaker[AsyncSession]
) -> None:
    """A mandate the buyer created survives the caller's rollback.

    Sharing a session would put the two in one transaction, and a caller that rolled back for its
    own reasons would silently discard commerce the mission really carried out.
    """
    merchant_id = await prepared(session)
    surface = buyer(factory, merchant_id)

    # An open transaction on the caller's session, deliberately, so that sharing would be visible.
    await session.execute(select(SpendingMandate))
    created = await surface.authorize_spending(mandate_request())
    await session.rollback()

    async with factory() as reader:
        found = await reader.get(SpendingMandate, created.id)
    assert found is not None


async def test_a_broken_buyer_transaction_leaves_the_runner_able_to_record(
    session: AsyncSession, factory: async_sessionmaker[AsyncSession]
) -> None:
    """The shortcoming this closes, driven end to end.

    The surface below poisons its own transaction and then refuses, which is what a service that
    hit a database error and raised would leave behind. On a shared session, the run service's
    next statement is refused by PostgreSQL and the run cannot be recorded or closed from the
    process that started it. On its own session, the poisoned transaction is discarded with it
    and the run completes.
    """
    merchant_id = await prepared(session)
    await BenchmarkSuiteService(session).publish(suite_of("one"))

    class Poisoning(MerchantBuyerSurface):
        async def search_products(self, request: ProductSearchRequest) -> ProductSearchResponse:
            del request
            async with self._sessions() as poisoned:
                with pytest.raises(DBAPIError):
                    await poisoned.execute(text("SELECT 1 / 0"))
                raise ConflictError("catalog_unavailable", "the catalog could not be read")

    finished = await BenchmarkRunService(session).run_suite(
        ReferenceMissionExecutor(
            Poisoning(factory, merchant_id=merchant_id, provider=FakePaymentProvider())
        ),
        suite_key=SUITE_KEY,
        suite_version=1,
        fixture=WORLD,
    )

    assert finished.status is BenchmarkRunStatus.COMPLETED


# Each operation reads the world as it now is.


async def test_the_buyer_sees_a_world_that_was_put_back_after_its_last_read(
    session: AsyncSession, factory: async_sessionmaker[AsyncSession]
) -> None:
    """The staleness a session held across a run would have.

    A session that has already loaded a variant keeps its loaded values through a commit, so a
    surface holding one across a whole run answers mission two with mission one's shelf. One
    session per operation is what makes every mission's read a fresh one.
    """
    merchant_id = await prepared(session, world(stock=3))
    surface = buyer(factory, merchant_id)
    before = await surface.search_products(ProductSearchRequest(limit=10))

    restocked = world(stock=7, version=2)
    environments = BenchmarkEnvironmentService(session)
    await environments.register(restocked)
    await environments.prepare(restocked)
    after = await surface.search_products(ProductSearchRequest(limit=10))

    assert before.results[0].eligible_variants[0].inventory_quantity == 3
    assert after.results[0].eligible_variants[0].inventory_quantity == 7


async def test_a_purchase_is_visible_to_the_runner_that_did_not_make_it(
    session: AsyncSession, factory: async_sessionmaker[AsyncSession]
) -> None:
    """The other direction, and it is what makes a recorded result substantiated.

    The runner resolves the variant, the quote and the payment attempt a mission claims against
    its own session. Those rows were written on the buyer's connections, so a run recording them
    is reading committed state across a connection boundary rather than its own uncommitted work.
    """
    merchant_id = await prepared(session)
    await BenchmarkSuiteService(session).publish(suite_of("one"))

    finished = await BenchmarkRunService(session).run_suite(
        ReferenceMissionExecutor(buyer(factory, merchant_id)),
        suite_key=SUITE_KEY,
        suite_version=1,
        fixture=WORLD,
    )

    result = finished.mission_runs[0]
    assert result.selected_variant_id is not None
    assert result.checkout_id is not None
    assert result.payment_attempt_id is not None
