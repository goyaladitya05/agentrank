"""The buyer surface over real HTTP, against a real server, with a real merchant credential.

Nothing here is mocked. A uvicorn server binds a loopback port, a credential is minted by the
real service, and every request goes over a socket and through the real authentication path. That
is the point: the reason this surface exists is that an in process one is not a boundary, and a
test that reached the application through an in memory transport would be asserting a boundary
that is not there.

Two properties are being asserted. The first is that a buyer can do everything it needs to over
the endpoints that already exist, which is what makes this transport a real substitution rather
than a second API. The second is that a refusal survives the trip: an executor tells a variant
the merchant does not sell from one it has run out of by reading `NotFoundError` against
`ConflictError`, so a transport that flattened both into one status would change what a buyer can
see while every test of the executor stayed green.
"""

import uuid
from collections.abc import AsyncIterator
from dataclasses import fields

import httpx2
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agentrank_api.auth.service import MerchantCredentialService
from agentrank_api.auth.tokens import TokenMarker
from agentrank_api.benchmark.definitions import AgentMissionBrief
from agentrank_api.benchmark.endpoint import (
    CREDENTIAL_LABEL,
    LocalCommerceEndpoint,
    issued_credential,
)
from agentrank_api.benchmark.environment import BenchmarkEnvironmentService
from agentrank_api.benchmark.faults import FaultOrigin
from agentrank_api.benchmark.fixtures import BenchmarkFixture
from agentrank_api.benchmark.http_buyer import (
    HttpBuyerCommerceSurface,
    MerchantSurfaceError,
    authenticated_client,
)
from agentrank_api.benchmark.reference_executor import ReferenceMissionExecutor
from agentrank_api.benchmark.report import (
    AbstentionCode,
)
from agentrank_api.benchmark.tools import MeasuredBuyerSurface, ToolLedger
from agentrank_api.commerce.catalog_fixture import SeedProduct, SeedVariant
from agentrank_api.commerce.repository import CatalogRepository, MerchantRepository
from agentrank_api.commerce.schemas import ProductSearchRequest
from agentrank_api.config import Settings
from agentrank_api.constraints.rules import ConstraintOperator
from agentrank_api.errors import AuthenticationError, NotFoundError
from agentrank_api.mandates.intent import AllowedCategory, MaxTotalAmount, RequiredAttribute
from agentrank_api.payments.fake import FakePaymentProvider
from agentrank_api.payments.models import PaymentAttempt, PaymentAttemptStatus

pytestmark = pytest.mark.anyio

CURRENCY = "INR"
PRICE = 100000
SLUG = "http-buyer-shop"
OTHER_SLUG = "http-buyer-other-shop"
SKU = "HB-BLK"

CHARGERS = AllowedCategory("chargers")
BLACK = RequiredAttribute("color", "black", ConstraintOperator.EQ)


def world(slug: str = SLUG, sku: str = SKU) -> BenchmarkFixture:
    return BenchmarkFixture(
        key=f"{slug}-catalog",
        version=1,
        merchant_slug=slug,
        merchant_name=slug,
        products=(
            SeedProduct(
                external_id=f"{slug}-chg",
                title="Charger",
                description=None,
                category="chargers",
                variants=(
                    SeedVariant(
                        sku=sku,
                        label="Black",
                        price_amount_minor=PRICE,
                        currency=CURRENCY,
                        inventory_quantity=3,
                        attributes={"color": "black"},
                    ),
                ),
            ),
        ),
    )


def brief(key: str = "one") -> AgentMissionBrief:
    return AgentMissionBrief(
        key=key,
        objective="Buy one black charger.",
        budget=MaxTotalAmount(amount_minor=PRICE, currency=CURRENCY),
        hard_constraints=(CHARGERS, BLACK),
    )


@pytest.fixture
async def endpoint(catalog_settings: Settings) -> AsyncIterator[LocalCommerceEndpoint]:
    """The commerce API on a loopback port, with the provider this test can inspect."""
    async with LocalCommerceEndpoint(catalog_settings, provider=FakePaymentProvider()) as running:
        yield running


async def prepared(session: AsyncSession, fixture: BenchmarkFixture) -> uuid.UUID:
    environments = BenchmarkEnvironmentService(session)
    environment = await environments.register(fixture)
    await environments.prepare(fixture)
    return environment.merchant_id


async def buyer(
    session: AsyncSession, endpoint: LocalCommerceEndpoint, merchant_id: uuid.UUID
) -> tuple[httpx2.AsyncClient, HttpBuyerCommerceSurface]:
    """A surface holding a freshly minted credential for this merchant."""
    issued = await MerchantCredentialService(session).issue(
        merchant_id=merchant_id, label=CREDENTIAL_LABEL, marker=TokenMarker.DEVELOPMENT
    )
    client = authenticated_client(endpoint.base_url, issued.token)
    return client, HttpBuyerCommerceSurface(client, merchant_id=merchant_id)


# A whole mission, over the wire.


async def test_a_reference_mission_completes_entirely_over_http(
    session: AsyncSession, endpoint: LocalCommerceEndpoint
) -> None:
    """Every one of the eight operations, over a socket, ending in a real payment.

    The executor is the same object the in process path uses and it cannot tell which transport
    it was handed, which is the substitution this surface exists to make possible.
    """
    merchant_id = await prepared(session, world())
    client, surface = await buyer(session, endpoint, merchant_id)

    async with client:
        report = await ReferenceMissionExecutor(surface)(brief(), merchant_id=merchant_id)

    assert report.selection is not None
    assert report.checkout is not None and report.checkout.checkout_id is not None
    assert report.payment is not None
    session.expire_all()
    # The purchase is asserted on the merchant's own rows rather than on what the buyer said,
    # which is the only thing that is evidence: the report names a payment and the payment table
    # says whether money moved.
    attempt = await session.get(PaymentAttempt, report.payment.attempt_id)
    assert attempt is not None and attempt.status is PaymentAttemptStatus.SUCCEEDED
    variant = await CatalogRepository(session).get_variant_by_sku(merchant_id, SKU)
    assert variant is not None and variant.inventory_quantity == 2


async def test_a_mission_with_nothing_acceptable_declines_over_http(
    session: AsyncSession, endpoint: LocalCommerceEndpoint
) -> None:
    """The control shape, so the transport is not only tested on the path that buys."""
    merchant_id = await prepared(session, world())
    client, surface = await buyer(session, endpoint, merchant_id)

    async with client:
        observed = await ReferenceMissionExecutor(surface)(
            AgentMissionBrief(
                key="unaffordable",
                objective="Buy one black charger for almost nothing.",
                budget=MaxTotalAmount(amount_minor=1, currency=CURRENCY),
                hard_constraints=(CHARGERS, BLACK),
            ),
            merchant_id=merchant_id,
        )

    assert observed.abstention is not None
    assert observed.abstention.code is AbstentionCode.BUDGET_INSUFFICIENT
    assert observed.selection is None


# Refusals survive the trip.


async def test_a_missing_product_arrives_as_a_not_found(
    session: AsyncSession, endpoint: LocalCommerceEndpoint
) -> None:
    """An executor tells a merchant's no from a merchant's failure by the exception it gets."""
    merchant_id = await prepared(session, world())
    client, surface = await buyer(session, endpoint, merchant_id)

    async with client:
        with pytest.raises(NotFoundError) as raised:
            await surface.get_product(uuid.uuid7())

    assert raised.value.resource == "product"


async def test_another_merchants_product_is_not_found_rather_than_forbidden(
    session: AsyncSession, endpoint: LocalCommerceEndpoint
) -> None:
    """Merchant scoping over the wire is the Phase 1H property, unchanged.

    A benchmark credential is an ordinary merchant credential. It cannot read somebody else's
    catalog, and the refusal says nothing about whether the resource exists.
    """
    mine = await prepared(session, world())
    theirs = await prepared(session, world(OTHER_SLUG, "HB-OTHER"))
    stranger = await CatalogRepository(session).get_variant_by_sku(theirs, "HB-OTHER")
    assert stranger is not None
    client, surface = await buyer(session, endpoint, mine)

    async with client:
        with pytest.raises(NotFoundError) as raised:
            await surface.get_product(stranger.product_id)

    assert raised.value.resource == "product"


async def test_a_search_never_returns_another_merchants_catalog(
    session: AsyncSession, endpoint: LocalCommerceEndpoint
) -> None:
    mine = await prepared(session, world())
    await prepared(session, world(OTHER_SLUG, "HB-OTHER"))
    client, surface = await buyer(session, endpoint, mine)

    async with client:
        found = await surface.search_products(ProductSearchRequest(limit=50))

    skus = {variant.sku for result in found.results for variant in result.eligible_variants}
    assert skus == {SKU}


# The credential itself.


async def test_a_revoked_credential_stops_working_immediately(
    session: AsyncSession, endpoint: LocalCommerceEndpoint
) -> None:
    """What `issued_credential` relies on when it revokes in a `finally`."""
    merchant_id = await prepared(session, world())
    credentials = MerchantCredentialService(session)

    async with issued_credential(
        credentials, merchant_id=merchant_id, marker=TokenMarker.DEVELOPMENT
    ) as token:
        client = authenticated_client(endpoint.base_url, token)
        surface = HttpBuyerCommerceSurface(client, merchant_id=merchant_id)
        async with client:
            found = await surface.search_products(ProductSearchRequest(limit=10))
            assert found.count == 1

    after = authenticated_client(endpoint.base_url, token)
    surface = HttpBuyerCommerceSurface(after, merchant_id=merchant_id)
    async with after:
        with pytest.raises(AuthenticationError):
            await surface.search_products(ProductSearchRequest(limit=10))


async def test_a_credential_is_scoped_to_one_merchant_and_says_nothing_wider(
    session: AsyncSession,
) -> None:
    """A benchmark credential is not a superuser token, and there is no such thing to issue."""
    merchant_id = await prepared(session, world())
    credentials = MerchantCredentialService(session)

    async with issued_credential(
        credentials, merchant_id=merchant_id, marker=TokenMarker.DEVELOPMENT
    ) as token:
        authenticated = await credentials.authenticate(token)

    assert authenticated is not None
    assert authenticated.merchant_id == merchant_id
    assert {field.name for field in fields(authenticated)} == {"merchant_id", "credential_id"}


# What a surface that does not answer looks like.


async def test_a_surface_that_cannot_be_reached_is_a_merchant_fault(
    session: AsyncSession, factory: async_sessionmaker[AsyncSession], unused_port: int
) -> None:
    """A refused connection is not a business answer, and the boundary attributes it as such."""
    merchant_id = await prepared(session, world())
    ledger = ToolLedger()
    client = authenticated_client(
        f"http://127.0.0.1:{unused_port}", "ar_dev_" + "0" * 32 + "_" + "0" * 64
    )
    surface = MeasuredBuyerSurface(
        HttpBuyerCommerceSurface(client, merchant_id=merchant_id), ledger
    )

    async with client:
        with pytest.raises(MerchantSurfaceError):
            await surface.search_products(ProductSearchRequest(limit=10))

    fault = ledger.fault()
    assert fault is not None
    assert fault.origin is FaultOrigin.MERCHANT


async def test_no_benchmark_route_is_reachable_over_the_endpoint(
    endpoint: LocalCommerceEndpoint,
) -> None:
    """The oracle has no HTTP surface, and this is what says so about the running server.

    Publishing a suite, starting a run and reading a result have deliberately never been
    endpoints. An executor with a credential and a base URL therefore has no request it can make
    that touches a run, a mission definition or an expected outcome.
    """
    async with httpx2.AsyncClient(base_url=endpoint.base_url) as client:
        published = (await client.get("/openapi.json")).json()

    paths = list(published["paths"])
    assert not [path for path in paths if "benchmark" in path or "mission" in path]
    assert not [
        name for name in published.get("components", {}).get("schemas", {}) if "Mission" in name
    ]


async def test_the_merchant_a_surface_believes_in_is_the_one_it_was_built_with(
    session: AsyncSession, endpoint: LocalCommerceEndpoint
) -> None:
    """There is no whoami route, so the identifier is stated rather than discovered.

    What actually decides scope is the credential, and the executor refuses a mission for a
    merchant its surface does not name, so the two are checked against each other before
    anything is bought.
    """
    merchant_id = await prepared(session, world())
    client, surface = await buyer(session, endpoint, merchant_id)

    async with client:
        assert surface.merchant_id == merchant_id
        with pytest.raises(ValueError, match="shops at merchant"):
            await ReferenceMissionExecutor(surface)(brief(), merchant_id=uuid.uuid7())


async def test_a_merchant_slug_lookup_confirms_the_endpoint_serves_the_same_database(
    session: AsyncSession, endpoint: LocalCommerceEndpoint
) -> None:
    """The server builds its own engine from the same settings, so it must see the same rows."""
    merchant_id = await prepared(session, world())
    merchant = await MerchantRepository(session).get_by_slug(SLUG)
    assert merchant is not None and merchant.id == merchant_id

    client, surface = await buyer(session, endpoint, merchant_id)
    async with client:
        found = await surface.search_products(ProductSearchRequest(limit=10))

    assert found.count == 1
