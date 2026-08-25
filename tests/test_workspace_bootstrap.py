"""Building a merchant's evaluation workspace against a real PostgreSQL.

The properties under test are the ones a pure function cannot hold: that one command written
twice produces one workspace, that a newer source snapshot leaves an older workspace exactly
where it was, that nothing here writes a commerce row, and that the three artifacts a bootstrap
creates either all exist or none of them does.
"""

import uuid
from collections.abc import Callable

import pytest
from sqlalchemy import event, func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from workspace_support import awkward, catalogued, plain, product, source, variant

from agentrank_api.benchmark.models import (
    BenchmarkEnvironment,
    BenchmarkMission,
    BenchmarkSuite,
)
from agentrank_api.commerce.models import Merchant, Product, Variant
from agentrank_api.commerce.repository import CatalogRepository, MerchantRepository
from agentrank_api.errors import ConflictError, NotFoundError
from agentrank_api.representation.definitions import MerchantSourceDefinition
from agentrank_api.representation.models import MerchantSourceSnapshot
from agentrank_api.representation.service import MerchantRepresentationService
from agentrank_api.workspace.definitions import BootstrapConfiguration
from agentrank_api.workspace.models import MerchantEvaluationWorkspace
from agentrank_api.workspace.service import MerchantEvaluationWorkspaceService
from agentrank_api.workspace.world import workspace_fixture

pytestmark = pytest.mark.anyio


async def merchant_with(
    session: AsyncSession, slug: str, definition: MerchantSourceDefinition | None = None
) -> tuple[Merchant, MerchantSourceSnapshot]:
    """A merchant whose only history is one published source snapshot."""
    merchant = await MerchantRepository(session).create(slug=slug, name=slug.title())
    await session.commit()
    snapshot = await MerchantRepresentationService(session).publish_source(
        definition or catalogued(slug)
    )
    return merchant, snapshot


async def refresh_source(
    session: AsyncSession, slug: str, definition: MerchantSourceDefinition
) -> MerchantSourceSnapshot:
    return await MerchantRepresentationService(session).publish_source(definition)


async def count(session: AsyncSession, model: type[object]) -> int:
    return int(await session.scalar(select(func.count()).select_from(model)) or 0)


# Building one


async def test_a_source_snapshot_becomes_a_world_and_a_workload(session: AsyncSession) -> None:
    """The whole phase in one assertion: evidence in, evaluation artifacts out, no files."""
    merchant, snapshot = await merchant_with(session, "bootstrap-shop")
    service = MerchantEvaluationWorkspaceService(session)

    outcome = await service.bootstrap(merchant.id, source_snapshot_id=snapshot.id)

    assert outcome.created is True
    workspace = outcome.workspace
    assert workspace.source_snapshot_id == snapshot.id
    environment = await session.get(BenchmarkEnvironment, workspace.environment_id)
    suite = await session.get(BenchmarkSuite, workspace.suite_id)
    assert environment is not None and suite is not None
    assert environment.merchant_id == merchant.id
    assert environment.fixture_hash == workspace.catalog_hash
    assert suite.definition_hash == workspace.suite_hash
    assert suite.merchant_slug == merchant.slug
    assert await count(session, BenchmarkMission) > 0


async def test_a_bootstrap_writes_no_commerce_row(session: AsyncSession) -> None:
    """The world is described here and materialized by benchmark preparation, which owns the
    shelf lock and the payment guard. Nothing here can overwrite a price or a stock level."""
    merchant, snapshot = await merchant_with(session, "no-commerce-shop")

    await MerchantEvaluationWorkspaceService(session).bootstrap(
        merchant.id, source_snapshot_id=snapshot.id
    )

    assert await count(session, Product) == 0
    assert await count(session, Variant) == 0


async def test_the_stored_fixture_reproduces_the_registered_world(session: AsyncSession) -> None:
    """A world with no file behind it still has to be preparable, so the payload is the world."""
    merchant, snapshot = await merchant_with(session, "fixture-shop")
    service = MerchantEvaluationWorkspaceService(session)

    workspace = (await service.bootstrap(merchant.id, source_snapshot_id=snapshot.id)).workspace

    rebuilt = workspace_fixture(workspace)
    assert rebuilt.content_hash == workspace.catalog_hash
    environment = await session.get(BenchmarkEnvironment, workspace.environment_id)
    assert environment is not None
    assert rebuilt.label == environment.label
    assert rebuilt.merchant_slug == merchant.slug


async def test_a_stored_world_edited_around_this_application_is_refused(
    session: AsyncSession,
) -> None:
    """The digest on the row is what makes the payload trustworthy enough to overwrite a shelf."""
    merchant, snapshot = await merchant_with(session, "tampered-shop")
    workspace = (
        await MerchantEvaluationWorkspaceService(session).bootstrap(
            merchant.id, source_snapshot_id=snapshot.id
        )
    ).workspace
    edited = dict(workspace.catalog_fixture)
    products = [dict(entry) for entry in edited["products"]]
    products[0]["title"] = "Something else"
    edited["products"] = products
    tampered = MerchantEvaluationWorkspace(
        merchant_id=workspace.merchant_id,
        source_snapshot_id=workspace.source_snapshot_id,
        environment_id=workspace.environment_id,
        suite_id=workspace.suite_id,
        generator_version=workspace.generator_version,
        configuration_digest=workspace.configuration_digest,
        catalog_hash=workspace.catalog_hash,
        suite_hash=workspace.suite_hash,
        catalog_fixture=edited,
        composition=workspace.composition,
    )

    with pytest.raises(ConflictError) as refused:
        workspace_fixture(tampered)
    assert refused.value.reason == "workspace_catalog_changed"


@pytest.mark.parametrize("builder", [catalogued, plain, awkward])
async def test_materially_different_merchants_need_no_bespoke_code(
    session: AsyncSession, builder: Callable[[str], MerchantSourceDefinition]
) -> None:
    slug = "shape-shop"
    merchant, snapshot = await merchant_with(session, slug, builder(slug))

    outcome = await MerchantEvaluationWorkspaceService(session).bootstrap(
        merchant.id, source_snapshot_id=snapshot.id
    )

    assert outcome.created is True
    assert await count(session, BenchmarkMission) > 0


# Idempotency


async def test_a_repeated_bootstrap_converges_on_one_workspace(session: AsyncSession) -> None:
    merchant, snapshot = await merchant_with(session, "retry-shop")
    service = MerchantEvaluationWorkspaceService(session)

    first = await service.bootstrap(merchant.id, source_snapshot_id=snapshot.id)
    second = await service.bootstrap(merchant.id, source_snapshot_id=snapshot.id)

    assert first.created is True
    assert second.created is False
    assert first.workspace.id == second.workspace.id
    assert await count(session, MerchantEvaluationWorkspace) == 1
    assert await count(session, BenchmarkEnvironment) == 1
    assert await count(session, BenchmarkSuite) == 1


async def test_a_lost_response_is_recoverable_by_reading(session: AsyncSession) -> None:
    """The caller that never saw an answer reads the workspace rather than building a second."""
    merchant, snapshot = await merchant_with(session, "lost-shop")
    service = MerchantEvaluationWorkspaceService(session)
    built = await service.bootstrap(merchant.id, source_snapshot_id=snapshot.id)

    summary = await service.current_summary(merchant.id)

    assert summary is not None
    assert summary.workspace_id == built.workspace.id


async def test_a_different_configuration_produces_a_distinct_workspace(
    session: AsyncSession,
) -> None:
    merchant, snapshot = await merchant_with(session, "config-shop")
    service = MerchantEvaluationWorkspaceService(session)

    first = await service.bootstrap(merchant.id, source_snapshot_id=snapshot.id)
    second = await service.bootstrap(
        merchant.id,
        source_snapshot_id=snapshot.id,
        configuration=BootstrapConfiguration(mission_budget=4),
    )

    assert first.workspace.id != second.workspace.id
    assert first.workspace.configuration_digest != second.workspace.configuration_digest
    assert first.workspace.suite_id != second.workspace.suite_id
    assert first.workspace.environment_id != second.workspace.environment_id


async def test_a_stale_source_snapshot_is_refused(session: AsyncSession) -> None:
    """A page rendered before a source refresh cannot build a world from superseded evidence."""
    merchant, first = await merchant_with(session, "stale-shop")
    await refresh_source(session, merchant.slug, catalogued(merchant.slug))
    newer = await refresh_source(
        session,
        merchant.slug,
        source(*plain(merchant.slug).products, slug=merchant.slug, version=2),
    )
    assert newer.id != first.id

    with pytest.raises(ConflictError) as refused:
        await MerchantEvaluationWorkspaceService(session).bootstrap(
            merchant.id, source_snapshot_id=first.id
        )
    assert refused.value.reason == "source_superseded"


# History


async def test_newer_evidence_leaves_an_existing_workspace_alone(session: AsyncSession) -> None:
    """The old world stays exactly as it was, and every run pointed at it still means what it
    meant. A newer snapshot is an offer to build a second workspace, never a rewrite."""
    merchant, first = await merchant_with(session, "history-shop")
    service = MerchantEvaluationWorkspaceService(session)
    original = (await service.bootstrap(merchant.id, source_snapshot_id=first.id)).workspace
    before = (original.environment_id, original.suite_id, original.catalog_hash)

    newer = await refresh_source(
        session,
        merchant.slug,
        source(*plain(merchant.slug).products, slug=merchant.slug, version=2),
    )
    second = (await service.bootstrap(merchant.id, source_snapshot_id=newer.id)).workspace

    await session.refresh(original)
    assert (original.environment_id, original.suite_id, original.catalog_hash) == before
    assert second.id != original.id
    assert second.environment_id != original.environment_id
    assert second.suite_id != original.suite_id
    assert (await service.current_summary(merchant.id)) is not None


async def test_a_workspace_cannot_be_updated(session: AsyncSession) -> None:
    """Immutability is the database's rather than this application's. A run points at the world
    and the workload a workspace names, so retargeting one would rewrite what a run measured."""
    merchant, snapshot = await merchant_with(session, "immutable-shop")
    workspace = (
        await MerchantEvaluationWorkspaceService(session).bootstrap(
            merchant.id, source_snapshot_id=snapshot.id
        )
    ).workspace

    with pytest.raises(DBAPIError, match="workspace is historical and is immutable"):
        await session.execute(
            text(
                "UPDATE merchant_evaluation_workspace SET generator_version = 'edited'"
                " WHERE id = :id"
            ),
            {"id": workspace.id},
        )


async def test_a_workspace_cannot_be_deleted(session: AsyncSession) -> None:
    merchant, snapshot = await merchant_with(session, "undeletable-shop")
    workspace = (
        await MerchantEvaluationWorkspaceService(session).bootstrap(
            merchant.id, source_snapshot_id=snapshot.id
        )
    ).workspace

    with pytest.raises(DBAPIError, match="workspace is historical and is immutable"):
        await session.execute(
            text("DELETE FROM merchant_evaluation_workspace WHERE id = :id"),
            {"id": workspace.id},
        )


async def test_the_newest_workspace_is_the_current_one(session: AsyncSession) -> None:
    merchant, first = await merchant_with(session, "current-shop")
    service = MerchantEvaluationWorkspaceService(session)
    await service.bootstrap(merchant.id, source_snapshot_id=first.id)
    newer = await refresh_source(
        session,
        merchant.slug,
        source(*plain(merchant.slug).products, slug=merchant.slug, version=2),
    )
    second = (await service.bootstrap(merchant.id, source_snapshot_id=newer.id)).workspace

    summary = await service.current_summary(merchant.id)

    assert summary is not None
    assert summary.workspace_id == second.id
    history = await service.history(merchant.id)
    assert history[0].workspace_id == second.id


# Reads


async def test_reading_a_history_does_not_query_once_per_workspace(
    session: AsyncSession, catalog_engine: AsyncEngine
) -> None:
    """A page of setups is a fixed number of reads, whatever the page holds.

    Counted rather than asserted in prose. Each summary names three artifacts and a mission
    count, so the obvious implementation is four queries per row, and a merchant with a long
    history would pay for all of them to draw a table of labels.
    """
    merchant, first = await merchant_with(session, "batched-shop")
    service = MerchantEvaluationWorkspaceService(session)
    await service.bootstrap(merchant.id, source_snapshot_id=first.id)
    for version in (2, 3):
        newer = await refresh_source(
            session,
            merchant.slug,
            source(*plain(merchant.slug).products, slug=merchant.slug, version=version),
        )
        await service.bootstrap(merchant.id, source_snapshot_id=newer.id)

    statements: list[str] = []

    def record(
        connection: object,
        cursor: object,
        statement: str,
        parameters: object,
        context: object,
        executemany: object,
    ) -> None:
        statements.append(statement)

    event.listen(catalog_engine.sync_engine, "before_cursor_execute", record)
    try:
        history = await service.history(merchant.id)
    finally:
        event.remove(catalog_engine.sync_engine, "before_cursor_execute", record)

    assert [entry.mission_count > 0 for entry in history] == [True, True, True]
    # The page itself, then one read each for the worlds, the workloads, the snapshots and the
    # mission counts. Five, and the same five for a page of one or a page of fifty.
    assert len(statements) == 5, statements


# Preflight


async def test_a_merchant_with_no_source_is_told_what_is_missing(session: AsyncSession) -> None:
    merchant = await MerchantRepository(session).create(slug="empty-shop", name="Empty")
    await session.commit()

    preflight = await MerchantEvaluationWorkspaceService(session).preflight(merchant.id)

    assert preflight.buildable is False
    assert [blocker.code for blocker in preflight.blockers] == ["merchant_source_unavailable"]
    assert preflight.planned is None


async def test_the_preflight_shows_exactly_what_a_bootstrap_will_build(
    session: AsyncSession,
) -> None:
    """A preview computed by the generator rather than described, so the two cannot disagree."""
    merchant, snapshot = await merchant_with(session, "preview-shop")
    service = MerchantEvaluationWorkspaceService(session)

    preflight = await service.preflight(merchant.id)
    assert preflight.buildable is True
    assert preflight.planned is not None
    planned = preflight.planned

    built = await service.bootstrap(merchant.id, source_snapshot_id=snapshot.id)
    summary = await service.summary_of(merchant.id, built.workspace.id)

    assert summary.mission_count == planned.mission_count
    assert summary.catalog == planned.catalog
    assert summary.composition == planned.composition
    assert summary.unsupported == planned.unsupported


async def test_a_source_that_supports_no_mission_is_a_named_blocker(
    session: AsyncSession,
) -> None:
    merchant, _ = await merchant_with(
        session,
        "unusable-shop",
        source(product("P1", variant("P1-A", stock=0)), slug="unusable-shop"),
    )

    preflight = await MerchantEvaluationWorkspaceService(session).preflight(merchant.id)

    assert [blocker.code for blocker in preflight.blockers] == ["no_purchasable_variant"]
    assert all(blocker.message for blocker in preflight.blockers)


async def test_a_merchant_with_a_catalog_nobody_generated_is_refused(
    session: AsyncSession,
) -> None:
    """Registering a world makes overwriting a catalog legal, so a catalog that came from
    somewhere else is refused rather than quietly adopted."""
    merchant, snapshot = await merchant_with(session, "seeded-shop")
    catalog = CatalogRepository(session)
    existing = await catalog.create_product(
        merchant_id=merchant.id, external_id="LEGACY", title="Legacy product"
    )
    await catalog.create_variant(
        product=existing, sku="LEGACY-A", price_amount_minor=1000, currency="INR"
    )
    await session.commit()

    preflight = await MerchantEvaluationWorkspaceService(session).preflight(merchant.id)
    assert "existing_catalog" in {blocker.code for blocker in preflight.blockers}

    with pytest.raises(ConflictError) as refused:
        await MerchantEvaluationWorkspaceService(session).bootstrap(
            merchant.id, source_snapshot_id=snapshot.id
        )
    assert refused.value.reason == "existing_catalog"


async def test_a_newer_source_is_reported_rather_than_applied(session: AsyncSession) -> None:
    merchant, first = await merchant_with(session, "newer-shop")
    service = MerchantEvaluationWorkspaceService(session)
    await service.bootstrap(merchant.id, source_snapshot_id=first.id)
    await refresh_source(
        session,
        merchant.slug,
        source(*plain(merchant.slug).products, slug=merchant.slug, version=2),
    )

    preflight = await service.preflight(merchant.id)

    assert preflight.source_is_newer_than_the_workspace is True
    assert preflight.current is not None
    assert preflight.current.source_snapshot_id == first.id


# Tenant isolation


async def test_a_workspace_belonging_to_another_merchant_is_unknown(
    session: AsyncSession,
) -> None:
    owner, snapshot = await merchant_with(session, "owner-shop")
    other = await MerchantRepository(session).create(slug="other-shop", name="Other")
    await session.commit()
    workspace = (
        await MerchantEvaluationWorkspaceService(session).bootstrap(
            owner.id, source_snapshot_id=snapshot.id
        )
    ).workspace

    with pytest.raises(NotFoundError):
        await MerchantEvaluationWorkspaceService(session).summary_of(other.id, workspace.id)


async def test_a_bootstrap_cannot_read_another_merchants_source(session: AsyncSession) -> None:
    """The snapshot identifier is checked against the caller's own current source, never used
    to select one."""
    _, owned = await merchant_with(session, "source-owner")
    other, _ = await merchant_with(session, "source-other")

    with pytest.raises(ConflictError) as refused:
        await MerchantEvaluationWorkspaceService(session).bootstrap(
            other.id, source_snapshot_id=owned.id
        )
    assert refused.value.reason == "source_superseded"


async def test_an_unknown_merchant_is_not_found(session: AsyncSession) -> None:
    with pytest.raises(NotFoundError):
        await MerchantEvaluationWorkspaceService(session).preflight(uuid.uuid7())
