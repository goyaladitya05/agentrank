"""Whether a diagnostic finding can name the compiler candidate that would answer it.

Phase 4C refused to build this link because the only join available then was an attribute
name, which is a heuristic wearing a reference's clothes. Phase 4D builds it out of identity
instead: a run pins the representation it was measured with, the representation names the
compiler run that produced it, and that run's candidates are addressed by their own
`(run_id, target)` unique key. Every step is a foreign key or a primary key, so a link exists
only where the relationship is provable and is absent everywhere else.

These tests drive the real compiler, the real representation publisher and the real run
service, because the thing worth asserting is the chain rather than the string formatting,
and a chain assembled from hand written rows would not exercise a single one of its links.
"""

import uuid

import pytest
from benchmark_support import mission, suite
from conftest import CredentialIssuer, bearer
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agentrank_api.benchmark.lifecycle import BenchmarkRunStatus
from agentrank_api.benchmark.report import ExecutorReport, ReportedSelection
from agentrank_api.benchmark.runner import BenchmarkRunService
from agentrank_api.benchmark.suites import BenchmarkSuiteService
from agentrank_api.commerce.repository import CatalogRepository, MerchantRepository
from agentrank_api.compiler.service import MerchantCompilerService
from agentrank_api.compiler.targets import variant_attribute_target
from agentrank_api.config import Settings
from agentrank_api.constraints.rules import ConstraintOperator
from agentrank_api.diagnostics.codes import DiagnosticCode
from agentrank_api.diagnostics.service import DiagnosticsService, RunDiagnostics
from agentrank_api.errors import NotFoundError
from agentrank_api.main import create_app
from agentrank_api.mandates.intent import RequiredAttribute
from agentrank_api.payments.fake import FakePaymentProvider
from agentrank_api.representation.definitions import (
    MerchantSourceDefinition,
    SourceProduct,
    SourceVariant,
)
from agentrank_api.representation.models import CommerceRepresentation
from agentrank_api.representation.service import MerchantRepresentationService

pytestmark = pytest.mark.anyio

EXTERNAL_ID = "GAP-CHG"
SKU = "GAP-CHG-BLK"
PRICE = 499900
CURRENCY = "INR"

# The mission asks for a wattage. The merchant's catalog does not publish one, which is the
# finding; the merchant's source text states one, which is what the compiler proposes.
WATTAGE = RequiredAttribute("wattage", 100, ConstraintOperator.GTE)


def source(slug: str) -> MerchantSourceDefinition:
    """A source whose prose states the wattage the catalog leaves out."""
    return MerchantSourceDefinition(
        key="gap-shop-source",
        version=1,
        merchant_slug=slug,
        products=(
            SourceProduct(
                external_id=EXTERNAL_ID,
                title="Gap Shop 100W Charger",
                description="A three-port charger rated to 100W.",
                category="chargers",
                variants=(
                    SourceVariant(
                        sku=SKU,
                        label="Black",
                        price_amount_minor=PRICE,
                        currency=CURRENCY,
                        inventory_quantity=4,
                        merchant_metadata={"finish": "black"},
                    ),
                ),
                merchant_metadata={},
            ),
        ),
        policy_text={},
    )


class GapShop:
    """One merchant whose catalog omits an attribute their source text states."""

    def __init__(
        self,
        merchant_id: uuid.UUID,
        variant_id: uuid.UUID,
        representation: CommerceRepresentation,
        compiler_run_id: uuid.UUID,
    ) -> None:
        self.merchant_id = merchant_id
        self.variant_id = variant_id
        self.representation = representation
        self.compiler_run_id = compiler_run_id


async def build_gap_shop(session: AsyncSession, slug: str) -> GapShop:
    """A merchant, a catalog with the gap, and a published compiler representation."""
    merchant = await MerchantRepository(session).create(slug=slug, name=slug)
    catalog = CatalogRepository(session)
    product = await catalog.create_product(
        merchant_id=merchant.id, external_id=EXTERNAL_ID, title="Charger", category="chargers"
    )
    variant = await catalog.create_variant(
        product=product,
        sku=SKU,
        price_amount_minor=PRICE,
        currency=CURRENCY,
        inventory_quantity=4,
        attributes={"color": "black"},
    )
    await session.commit()

    snapshot = await MerchantRepresentationService(session).publish_source(source(slug))
    compiler = MerchantCompilerService(session)
    compiler_run = await compiler.run(merchant.id, snapshot.id)
    representation = await compiler.publish(merchant.id, compiler_run.id)
    return GapShop(merchant.id, variant.id, representation, compiler_run.id)


async def diagnose_gap_run(
    session: AsyncSession,
    shop: GapShop,
    slug: str,
    *,
    representation: CommerceRepresentation | None,
) -> RunDiagnostics:
    """Run one mission that cannot read the wattage, and diagnose it."""
    await BenchmarkSuiteService(session).publish(
        suite(mission("buy-a-charger", constraints=(WATTAGE,)), merchant_slug=slug)
    )
    service = BenchmarkRunService(session)
    run = await service.start_run(
        suite_key="test-suite",
        suite_version=1,
        merchant_slug=slug,
        representation=representation,
    )
    await service.start_mission(run.id, "buy-a-charger", merchant_id=shop.merchant_id)
    await service.record_result(
        run.id,
        "buy-a-charger",
        ExecutorReport(
            merchant_id=shop.merchant_id,
            selection=ReportedSelection(variant_id=shop.variant_id, quantity=1),
        ),
        merchant_id=shop.merchant_id,
    )
    await service.complete_run(run.id, merchant_id=shop.merchant_id)
    return await DiagnosticsService(session).run_diagnostics(run.id, merchant_id=shop.merchant_id)


class TestExactLinkage:
    async def test_a_pinned_compiler_representation_addresses_its_own_candidate(
        self, session: AsyncSession
    ) -> None:
        shop = await build_gap_shop(session, "gap-shop")
        diagnosis = await diagnose_gap_run(
            session, shop, "gap-shop", representation=shop.representation
        )

        assert diagnosis.compiler_run_id == shop.compiler_run_id
        gap = next(
            finding
            for finding in diagnosis.findings
            if finding.code is DiagnosticCode.ATTRIBUTE_NOT_PUBLISHED
        )
        assert gap.attribute_keys == ("wattage",)
        assert len(gap.compiler_references) == 1
        reference = gap.compiler_references[0]
        assert reference.target == variant_attribute_target(SKU, "wattage")
        assert reference.compiler_run_id == shop.compiler_run_id

        candidates = await MerchantCompilerService(session).candidates(
            shop.merchant_id, shop.compiler_run_id
        )
        addressed = next(
            candidate for candidate in candidates if candidate.id == reference.candidate_id
        )
        assert addressed.target == reference.target

    async def test_a_run_that_pins_no_representation_offers_no_compiler_action(
        self, session: AsyncSession
    ) -> None:
        shop = await build_gap_shop(session, "unpinned-shop")
        diagnosis = await diagnose_gap_run(session, shop, "unpinned-shop", representation=None)

        assert diagnosis.compiler_run_id is None
        gap = next(
            finding
            for finding in diagnosis.findings
            if finding.code is DiagnosticCode.ATTRIBUTE_NOT_PUBLISHED
        )
        # The finding is unchanged and honest. Absence of a link is not absence of a problem.
        assert gap.attribute_keys == ("wattage",)
        assert gap.compiler_references == ()


class TestTenantIsolation:
    async def test_another_merchants_candidate_is_never_reachable(
        self, session: AsyncSession
    ) -> None:
        mine = await build_gap_shop(session, "mine-shop")
        theirs = await build_gap_shop(session, "theirs-shop")
        diagnosis = await diagnose_gap_run(
            session, mine, "mine-shop", representation=mine.representation
        )

        gap = next(
            finding
            for finding in diagnosis.findings
            if finding.code is DiagnosticCode.ATTRIBUTE_NOT_PUBLISHED
        )
        theirs_candidates = await MerchantCompilerService(session).candidates(
            theirs.merchant_id, theirs.compiler_run_id
        )
        foreign = {candidate.id for candidate in theirs_candidates}
        assert all(reference.candidate_id not in foreign for reference in gap.compiler_references)
        assert all(
            reference.compiler_run_id != theirs.compiler_run_id
            for reference in gap.compiler_references
        )

    async def test_a_referenced_compiler_run_refuses_the_wrong_merchant(
        self, session: AsyncSession
    ) -> None:
        mine = await build_gap_shop(session, "reader-shop")
        theirs = await build_gap_shop(session, "writer-shop")

        with pytest.raises(NotFoundError):
            await MerchantCompilerService(session).get_run(mine.merchant_id, theirs.compiler_run_id)


class TestRunLineage:
    async def test_the_run_is_completed_and_its_pin_is_the_published_representation(
        self, session: AsyncSession
    ) -> None:
        shop = await build_gap_shop(session, "lineage-shop")
        diagnosis = await diagnose_gap_run(
            session, shop, "lineage-shop", representation=shop.representation
        )

        assert diagnosis.status == BenchmarkRunStatus.COMPLETED.value
        assert diagnosis.representation_id == shop.representation.id
        assert shop.representation.compiler_run_id == shop.compiler_run_id


class TestWireContract:
    async def test_the_insights_payload_carries_the_addresses_it_can_prove(
        self,
        settings: Settings,
        session: AsyncSession,
        factory: async_sessionmaker[AsyncSession],
        issue_credential: CredentialIssuer,
    ) -> None:
        shop = await build_gap_shop(session, "wire-shop")
        diagnosis = await diagnose_gap_run(
            session, shop, "wire-shop", representation=shop.representation
        )
        token = await issue_credential(shop.merchant_id)
        # Built without the lifespan, exactly as every other API test builds one: starting it
        # would replace this factory with an engine pointed at the developer's database.
        application = create_app(settings, payment_provider=FakePaymentProvider())
        application.state.session_factory = factory
        http = TestClient(application)
        response = http.get(f"/api/v1/insights/runs/{diagnosis.run_id}", headers=bearer(token))

        assert response.status_code == 200
        body = response.json()
        assert body["compiler_run_id"] == str(shop.compiler_run_id)
        gap = next(
            finding
            for finding in body["findings"]
            if finding["code"] == DiagnosticCode.ATTRIBUTE_NOT_PUBLISHED.value
        )
        assert gap["compiler_references"] == [
            {
                "compiler_run_id": str(shop.compiler_run_id),
                "candidate_id": gap["compiler_references"][0]["candidate_id"],
                "target": variant_attribute_target(SKU, "wattage"),
            }
        ]
        outage_free = [
            finding
            for finding in body["findings"]
            if finding["code"] != DiagnosticCode.ATTRIBUTE_NOT_PUBLISHED.value
        ]
        assert all(finding["compiler_references"] == [] for finding in outage_free)
