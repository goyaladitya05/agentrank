"""Turning one merchant's frozen source snapshot into an evaluation workspace, once.

Two operations and no third. `preflight` says what a bootstrap would build and what stops it,
and `bootstrap` builds it. Everything else on this surface is a read.

What a bootstrap actually writes is three rows in one transaction:

```text
benchmark_environment            this merchant is a benchmark world under this fixture identity
benchmark_suite + missions       the generated workload, immutable and content addressed
merchant_evaluation_workspace    that those two came from this snapshot under this configuration
```

It writes no commerce row at all, and that is a property rather than an omission. A product
row, a variant row, a price and a stock level are what the commerce runtime decides, and this
never touches one: the world is materialized by the existing benchmark preparation, which
already owns the shelf lock, the payment guard and the refusal to reset a world a run is using.
So "bootstrap cannot alter authoritative commerce state" is true because there is no statement
here that could.

It calls no model and no external service. Every fact in the generated world and every oracle in
the generated workload is computed from the merchant's own frozen evidence, so this is one short
transaction with nothing in it that can hang.

Retry, duplicate and concurrency are one mechanism rather than three. Everything happens under
the same per-merchant advisory lock a benchmark world registration takes, so two bootstraps of
one merchant serialize instead of racing on a version number. Inside it, a workspace already
written for this merchant, this snapshot and this configuration is returned rather than rebuilt,
which makes a repeat, a retry after a lost response and a concurrent duplicate the same answer.
A unique constraint holds the same rule across processes for anything that gets past the lock.

The version numbers the generated world and workload carry are allocated under that lock as one
past the highest that exists for the key. The keys are derived from the merchant slug, so two
merchants can never collide, and the lock is what stops one merchant colliding with itself.

Two things are refused rather than absorbed, and both are about not destroying something.

A merchant whose catalog rows or whose registered benchmark world came from somewhere else is
refused. Registering a world is what makes overwriting a catalog legal, and a merchant who
already has one has a world somebody authored on purpose; generating a second beside it would
leave two answers to which world a launch should use, and preparing it would overwrite a catalog
this generator did not describe.

And a merchant with an evaluation queued or running is refused. Their launch froze the world it
is going to execute against, so a newer world appearing underneath it is not a race this can win
politely: the dispatcher would find a world the launch did not name and settle the launch as a
mismatch, destroying a request the merchant made.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from agentrank_api.benchmark.environment import BenchmarkEnvironmentService
from agentrank_api.benchmark.evaluation_launch import (
    PENDING_STATUSES,
    BenchmarkEvaluationLaunch,
)
from agentrank_api.benchmark.identity import suite_content_hash
from agentrank_api.benchmark.models import BenchmarkEnvironment, BenchmarkSuite
from agentrank_api.benchmark.repository import (
    BenchmarkEnvironmentRepository,
    BenchmarkRunRepository,
    BenchmarkSuiteRepository,
)
from agentrank_api.commerce.models import Merchant, Product
from agentrank_api.conflicts import translated_conflicts
from agentrank_api.errors import ConflictError, NotFoundError
from agentrank_api.representation.fixtures import parse_source
from agentrank_api.representation.intake import MerchantSourceIntakeService
from agentrank_api.representation.models import MerchantSourceSnapshot
from agentrank_api.workspace.definitions import (
    BootstrapBlocker,
    BootstrapConfiguration,
    BootstrapRefusedError,
    FamilyComposition,
    MissionFamily,
    UnsupportedFamily,
    workspace_key,
)
from agentrank_api.workspace.generation import (
    SUITE_KEY_SUFFIX,
    GeneratedSuite,
    generate_suite,
)
from agentrank_api.workspace.models import MerchantEvaluationWorkspace
from agentrank_api.workspace.projection import (
    CATALOG_KEY_SUFFIX,
    CatalogSummary,
    EvaluationCatalog,
    project_catalog,
)
from agentrank_api.workspace.repository import MerchantEvaluationWorkspaceRepository

RESOURCE = "merchant_evaluation_workspace"

# The refusal `conflicts.py` produces when two bootstraps of one merchant, one snapshot and one
# configuration reach the constraint together. Named here because this service is what turns
# losing that race back into the workspace the winner wrote.
WORKSPACE_ALREADY_BUILT = "workspace_already_built"


@dataclass(frozen=True, slots=True)
class PlannedWorkspace:
    """What a bootstrap would build from the merchant's evidence as it stands right now.

    Produced by running the generator rather than by describing it, so the mission count and the
    composition a merchant reads before committing are the ones they get. Nothing is written and
    nothing is spent to produce this.
    """

    catalog: CatalogSummary
    mission_count: int
    composition: tuple[FamilyComposition, ...]
    unsupported: tuple[UnsupportedFamily, ...]
    omitted_fields: tuple[str, ...]
    configuration: BootstrapConfiguration


@dataclass(frozen=True, slots=True)
class WorkspaceSummary:
    """One built workspace, as its identity and its shape rather than its content.

    Deliberately without the generated catalog and without a single mission. A console overview
    that rendered either would be loading a whole world to draw a table of counts, and a mission
    carries an expected outcome that no merchant-facing surface has any business publishing.
    """

    workspace_id: uuid.UUID
    created_at: datetime
    source_snapshot_id: uuid.UUID
    source_snapshot_label: str
    environment_id: uuid.UUID
    environment_label: str
    suite_id: uuid.UUID
    suite_label: str
    mission_count: int
    catalog: CatalogSummary
    composition: tuple[FamilyComposition, ...]
    unsupported: tuple[UnsupportedFamily, ...]
    generator_version: str
    configuration_digest: str
    catalog_hash: str
    suite_hash: str


@dataclass(frozen=True, slots=True)
class WorkspacePreflight:
    """Where a merchant's evaluation setup has got to, and what a bootstrap would do next.

    `current` is the workspace a launch would use, and `current_source_snapshot_id` is the
    evidence the merchant is publishing now. When those disagree the merchant has newer evidence
    than the world they are set up to be measured against, which is a fact to show them rather
    than something to act on: a newer snapshot never silently rebuilds a world, and every
    historical run stays pointed at exactly what it executed.
    """

    current_source_snapshot_id: uuid.UUID | None
    current_source_snapshot_label: str | None
    current: WorkspaceSummary | None
    planned: PlannedWorkspace | None
    blockers: tuple[BootstrapBlocker, ...]

    @property
    def buildable(self) -> bool:
        """Whether asking for a bootstrap right now would produce one."""
        return not self.blockers and self.planned is not None

    @property
    def source_is_newer_than_the_workspace(self) -> bool:
        """Whether the merchant has published evidence their current world was not built from."""
        if self.current is None or self.current_source_snapshot_id is None:
            return False
        return self.current.source_snapshot_id != self.current_source_snapshot_id


@dataclass(frozen=True, slots=True)
class BootstrapOutcome:
    """What one bootstrap command did.

    `created` is false when the command resolved to a workspace that already existed, which is
    what a retry after a lost response and a concurrent duplicate both produce. It is reported
    rather than hidden, for the same reason a source submission reports whether it wrote a
    snapshot: a caller should be able to tell "this built your setup" from "this was already
    your setup".
    """

    workspace: MerchantEvaluationWorkspace
    created: bool


class MerchantEvaluationWorkspaceService:
    """Read a merchant's evaluation setup, and build one from their frozen source evidence."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._workspaces = MerchantEvaluationWorkspaceRepository(session)
        self._sources = MerchantSourceIntakeService(session)
        self._environments = BenchmarkEnvironmentRepository(session)
        self._environment_service = BenchmarkEnvironmentService(session)
        self._suites = BenchmarkSuiteRepository(session)
        self._runs = BenchmarkRunRepository(session)

    async def preflight(
        self, merchant_id: uuid.UUID, *, configuration: BootstrapConfiguration | None = None
    ) -> WorkspacePreflight:
        """What this merchant's setup is now, what a bootstrap would build, and what stops it.

        Reads only. Nothing is written, nothing is registered and no model is called, so a
        console can render this on every page load.
        """
        settings = configuration or BootstrapConfiguration()
        merchant = await self._merchant(merchant_id)
        current = await self._workspaces.current(merchant_id)
        summary = None if current is None else await self._summary(current)
        snapshot = await self._sources.current(merchant_id)

        if snapshot is None:
            return WorkspacePreflight(
                current_source_snapshot_id=None,
                current_source_snapshot_label=None,
                current=summary,
                planned=None,
                blockers=(_NO_SOURCE,),
            )

        blockers = list(await self._state_blockers(merchant_id, snapshot=snapshot))
        planned: PlannedWorkspace | None = None
        try:
            catalog, suite = _build(merchant, snapshot, configuration=settings, version=1)
        except BootstrapRefusedError as refused:
            blockers.append(refused.blocker)
        else:
            planned = PlannedWorkspace(
                catalog=catalog.summary,
                mission_count=suite.mission_count,
                composition=suite.composition,
                unsupported=suite.unsupported,
                omitted_fields=catalog.omitted_fields,
                configuration=settings,
            )
        return WorkspacePreflight(
            current_source_snapshot_id=snapshot.id,
            current_source_snapshot_label=snapshot.label,
            current=summary,
            planned=planned,
            blockers=tuple(blockers),
        )

    async def bootstrap(
        self,
        merchant_id: uuid.UUID,
        *,
        source_snapshot_id: uuid.UUID,
        configuration: BootstrapConfiguration | None = None,
    ) -> BootstrapOutcome:
        """Build this merchant's evaluation workspace, or answer with the one already built.

        `source_snapshot_id` is the evidence the caller was shown, and it is checked against the
        merchant's current snapshot rather than used to select one. A browser that could name a
        snapshot could build a world from evidence the merchant has already superseded, and a
        page rendered before a source refresh would silently do exactly that.

        Everything after the lock is one transaction. Either all three rows exist afterwards or
        none of them does, so there is no half-created world for a run to pin and no orphan
        workspace pointing at a suite that was never published.
        """
        settings = configuration or BootstrapConfiguration()
        merchant = await self._merchant(merchant_id)
        # The same advisory lock a benchmark world registration and a world preparation take.
        # Sharing it rather than inventing a second key is what makes this serialize against an
        # operator seeding an authored world for the same merchant at the same moment.
        await self._environment_service.claim(merchant.slug)

        snapshot = await self._sources.current(merchant_id)
        if snapshot is None:
            raise ConflictError(
                _NO_SOURCE.code,
                _NO_SOURCE.message,
                resource=RESOURCE,
                identifier=str(merchant_id),
            )
        if snapshot.id != source_snapshot_id:
            raise ConflictError(
                "source_superseded",
                "Newer merchant information has been published since this page was loaded."
                " Reload to build an evaluation setup from your current source.",
                resource=RESOURCE,
                identifier=str(snapshot.id),
            )

        existing = await self._workspaces.by_identity(
            merchant_id,
            source_snapshot_id=snapshot.id,
            configuration_digest=settings.digest,
        )
        if existing is not None:
            return BootstrapOutcome(workspace=existing, created=False)

        for blocker in await self._state_blockers(merchant_id, snapshot=snapshot):
            raise ConflictError(
                blocker.code, blocker.message, resource=RESOURCE, identifier=str(merchant_id)
            )

        try:
            catalog, suite = _build(
                merchant,
                snapshot,
                configuration=settings,
                version=await self._next_version(merchant.slug),
            )
        except BootstrapRefusedError as refused:
            raise ConflictError(
                refused.blocker.code,
                refused.blocker.message,
                resource=RESOURCE,
                identifier=str(snapshot.id),
            ) from refused

        try:
            async with translated_conflicts(self._session, identifier=str(merchant_id)):
                environment = await self._environments.create(
                    merchant=merchant, fixture=catalog.fixture
                )
                published = await self._suites.create(suite.definition)
                workspace = await self._workspaces.create(
                    merchant_id=merchant_id,
                    source_snapshot_id=snapshot.id,
                    environment_id=environment.id,
                    suite_id=published.id,
                    generator_version=settings.generator_version,
                    configuration_digest=settings.digest,
                    catalog_hash=catalog.fixture.content_hash,
                    suite_hash=suite_content_hash(suite.definition),
                    catalog_fixture=catalog.fixture.to_payload(),
                    composition=suite.to_payload(),
                )
        except ConflictError as conflict:
            if conflict.reason != WORKSPACE_ALREADY_BUILT:
                raise
            # Unreachable while every writer takes the advisory lock above, and answered anyway.
            # The transaction was rolled back by the translation, so this read sees whatever the
            # winner committed and the command converges rather than reporting a failure for a
            # workspace that exists.
            return BootstrapOutcome(
                workspace=await self._require_identity(merchant_id, snapshot.id, settings.digest),
                created=False,
            )
        await self._session.commit()
        return BootstrapOutcome(workspace=workspace, created=True)

    async def _require_identity(
        self, merchant_id: uuid.UUID, source_snapshot_id: uuid.UUID, digest: str
    ) -> MerchantEvaluationWorkspace:
        """The workspace a lost race wrote, raising rather than returning None."""
        existing = await self._workspaces.by_identity(
            merchant_id, source_snapshot_id=source_snapshot_id, configuration_digest=digest
        )
        if existing is None:  # pragma: no cover
            raise ConflictError(
                WORKSPACE_ALREADY_BUILT,
                "This evaluation setup could not be built or read back.",
                resource=RESOURCE,
                identifier=str(merchant_id),
            )
        return existing

    async def current_source_snapshot_id(self, merchant_id: uuid.UUID) -> uuid.UUID | None:
        """Which snapshot a bootstrap would read, without loading the document in it.

        An operator naming only a merchant slug needs this to call `bootstrap`, which takes the
        snapshot the caller was shown rather than selecting one itself. The service checks it
        again under its own lock, so reading it here is a convenience and never the authority.
        """
        identity = await self._sources.current_identity(merchant_id)
        return None if identity is None else identity.snapshot_id

    async def current_summary(self, merchant_id: uuid.UUID) -> WorkspaceSummary | None:
        """The merchant's current workspace as counts and identities, or None."""
        current = await self._workspaces.current(merchant_id)
        return None if current is None else await self._summary(current)

    async def summary_of(self, merchant_id: uuid.UUID, workspace_id: uuid.UUID) -> WorkspaceSummary:
        """One workspace this merchant owns. Somebody else's identifier is an unknown one."""
        found = (
            await self._session.execute(
                select(MerchantEvaluationWorkspace).where(
                    MerchantEvaluationWorkspace.id == workspace_id,
                    MerchantEvaluationWorkspace.merchant_id == merchant_id,
                )
            )
        ).scalar_one_or_none()
        if found is None:
            raise NotFoundError(RESOURCE, str(workspace_id))
        return await self._summary(found)

    async def history(self, merchant_id: uuid.UUID, *, limit: int = 10) -> list[WorkspaceSummary]:
        """This merchant's workspaces, newest first, bounded."""
        rows = list(
            (
                await self._session.execute(
                    select(MerchantEvaluationWorkspace)
                    .where(MerchantEvaluationWorkspace.merchant_id == merchant_id)
                    .order_by(MerchantEvaluationWorkspace.write_order.desc())
                    .limit(limit)
                )
            ).scalars()
        )
        return [await self._summary(row) for row in rows]

    async def _summary(self, workspace: MerchantEvaluationWorkspace) -> WorkspaceSummary:
        """One stored workspace, read back with the labels a merchant sees it under."""
        environment = await self._session.get(BenchmarkEnvironment, workspace.environment_id)
        suite = await self._session.get(BenchmarkSuite, workspace.suite_id)
        snapshot = await self._session.get(MerchantSourceSnapshot, workspace.source_snapshot_id)
        if environment is None or suite is None or snapshot is None:  # pragma: no cover
            # Held impossible by three RESTRICT foreign keys and by the immutability triggers on
            # all three tables. Stated so that a lineage nobody can explain is a named error
            # rather than an attribute access on None.
            raise ConflictError(
                "workspace_lineage_unreadable",
                "This evaluation workspace names artifacts that cannot be read.",
                resource=RESOURCE,
                identifier=str(workspace.id),
            )
        catalog = _stored_catalog(workspace)
        composition, unsupported = _stored_composition(workspace)
        return WorkspaceSummary(
            workspace_id=workspace.id,
            created_at=workspace.created_at,
            source_snapshot_id=workspace.source_snapshot_id,
            source_snapshot_label=snapshot.label,
            environment_id=environment.id,
            environment_label=environment.label,
            suite_id=suite.id,
            suite_label=suite.label,
            mission_count=await self._workspaces.mission_count(suite.id),
            catalog=catalog,
            composition=composition,
            unsupported=unsupported,
            generator_version=workspace.generator_version,
            configuration_digest=workspace.configuration_digest,
            catalog_hash=workspace.catalog_hash,
            suite_hash=workspace.suite_hash,
        )

    async def _state_blockers(
        self, merchant_id: uuid.UUID, *, snapshot: MerchantSourceSnapshot
    ) -> tuple[BootstrapBlocker, ...]:
        """Everything about this merchant's current state that stops a bootstrap.

        Deliberately not including a workspace that already exists for this snapshot. A repeat
        of one command converges on the workspace it already produced, and a deliberately
        different configuration is a second workspace rather than a refusal; what the console
        does with that is decide whether to offer the button, which is a rendering decision and
        not this method's.
        """
        blockers: list[BootstrapBlocker] = []
        generated = await self._workspaces.environment_ids(merchant_id)
        registered = await self._environments.list_for_merchant(merchant_id)
        if any(environment.id not in generated for environment in registered):
            blockers.append(_FOREIGN_WORLD)
        elif not generated and await self._has_catalog(merchant_id):
            blockers.append(_EXISTING_CATALOG)

        if await self._pending_launch(merchant_id) is not None:
            blockers.append(_EVALUATION_PENDING)
        elif await self._runs.active_run_id(merchant_id=merchant_id) is not None:
            blockers.append(_RUN_ACTIVE)
        return tuple(blockers)

    async def _has_catalog(self, merchant_id: uuid.UUID) -> bool:
        """Whether this merchant already has commerce rows nothing here produced.

        Existence rather than a count. What matters is whether preparing a generated world would
        write over a catalog that came from somewhere else, and one row is enough for that to be
        true.
        """
        return (
            await self._session.execute(
                select(Product.id).where(Product.merchant_id == merchant_id).limit(1)
            )
        ).scalar_one_or_none() is not None

    async def _pending_launch(self, merchant_id: uuid.UUID) -> BenchmarkEvaluationLaunch | None:
        return (
            await self._session.execute(
                select(BenchmarkEvaluationLaunch).where(
                    BenchmarkEvaluationLaunch.merchant_id == merchant_id,
                    BenchmarkEvaluationLaunch.status.in_(sorted(PENDING_STATUSES)),
                )
            )
        ).scalar_one_or_none()

    async def _next_version(self, merchant_slug: str) -> int:
        """One past the highest version this merchant's generated keys already carry.

        Read under the advisory lock the caller holds, so two bootstraps of one merchant cannot
        both allocate the same number. The keys are derived from the merchant slug, so no other
        merchant can be allocating against them at all, and the fixture and the suite share one
        number so that a workspace's two artifacts are named the same way.
        """
        catalog_key = workspace_key(merchant_slug, CATALOG_KEY_SUFFIX)
        suite_key = workspace_key(merchant_slug, SUITE_KEY_SUFFIX)
        highest_world = (
            await self._session.execute(
                select(func.max(BenchmarkEnvironment.fixture_version)).where(
                    BenchmarkEnvironment.fixture_key == catalog_key
                )
            )
        ).scalar_one()
        highest_suite = (
            await self._session.execute(
                select(func.max(BenchmarkSuite.version)).where(
                    BenchmarkSuite.suite_key == suite_key
                )
            )
        ).scalar_one()
        return max(highest_world or 0, highest_suite or 0) + 1

    async def _merchant(self, merchant_id: uuid.UUID) -> Merchant:
        merchant = await self._session.get(Merchant, merchant_id)
        if merchant is None:
            raise NotFoundError("merchant", str(merchant_id))
        return merchant


def _build(
    merchant: Merchant,
    snapshot: MerchantSourceSnapshot,
    *,
    configuration: BootstrapConfiguration,
    version: int,
) -> tuple[EvaluationCatalog, GeneratedSuite]:
    """The whole deterministic half, in one place and with no session anywhere near it.

    Called by the preflight with a provisional version and by the bootstrap with the allocated
    one. The version is part of an artifact's identity and nothing else, so a preview built
    under version one describes exactly the world and workload a bootstrap will register.
    """
    try:
        source = parse_source(snapshot.payload)
    except ValueError as unreadable:
        raise BootstrapRefusedError(
            BootstrapBlocker(
                "source_unreadable",
                "AgentRank could not read your current merchant information as a source"
                " document. Submit your source again.",
            )
        ) from unreadable
    catalog = project_catalog(
        source,
        merchant_slug=merchant.slug,
        merchant_name=merchant.name,
        version=version,
    )
    suite = generate_suite(
        catalog,
        merchant_slug=merchant.slug,
        version=version,
        configuration=configuration,
    )
    return catalog, suite


def _stored_catalog(workspace: MerchantEvaluationWorkspace) -> CatalogSummary:
    """The generated world's shape, counted from the payload this workspace stored.

    Counted here rather than by rebuilding a `BenchmarkFixture`, because this is a summary for a
    console and reconstructing a validated domain object to produce five integers would be work
    nobody asked for. The payload is immutable and its digest is on the row beside it.
    """
    products = workspace.catalog_fixture.get("products", [])
    variants = [variant for product in products for variant in product.get("variants", [])]
    return CatalogSummary(
        products=len(products),
        variants=len(variants),
        purchasable_variants=sum(
            1
            for variant in variants
            if variant.get("is_active", True) and variant.get("inventory_quantity", 0) > 0
        ),
        currencies=tuple(sorted({str(variant["currency"]) for variant in variants})),
        categories=tuple(
            sorted(
                {
                    str(product["category"])
                    for product in products
                    if product.get("category") is not None
                }
            )
        ),
    )


def _stored_composition(
    workspace: MerchantEvaluationWorkspace,
) -> tuple[tuple[FamilyComposition, ...], tuple[UnsupportedFamily, ...]]:
    """The composition this workspace recorded, read back as the domain types it was built as.

    A family this build no longer knows is dropped rather than rendered as a raw string. A
    workspace generated by an older build stays readable, and nothing renders a member of an
    enumeration this build cannot name.
    """
    known = {family.value: family for family in MissionFamily}
    stored: dict[str, Any] = workspace.composition
    families = tuple(
        FamilyComposition(
            family=known[entry["family"]],
            missions=int(entry["missions"]),
            purchase_available=int(entry["purchase_available"]),
            no_acceptable_purchase=int(entry["no_acceptable_purchase"]),
        )
        for entry in stored.get("families", [])
        if entry.get("family") in known
    )
    unsupported = tuple(
        UnsupportedFamily(family=known[entry["family"]], reason=str(entry["reason"]))
        for entry in stored.get("unsupported", [])
        if entry.get("family") in known
    )
    return families, unsupported


_NO_SOURCE = BootstrapBlocker(
    "merchant_source_unavailable",
    "AgentRank has no record of your merchant information yet, so there is nothing to build an"
    " evaluation setup from. Add your merchant source first.",
)

_FOREIGN_WORLD = BootstrapBlocker(
    "existing_benchmark_world",
    "This merchant already has a benchmark world that AgentRank did not generate from your"
    " merchant information. Your operator decides which world an evaluation should use; this is"
    " not something to fix from here.",
)

_EXISTING_CATALOG = BootstrapBlocker(
    "existing_catalog",
    "This merchant already has a product catalog in AgentRank that no evaluation setup"
    " produced. Building an evaluation world would overwrite it, so it is refused. Your"
    " operator can see why.",
)

_EVALUATION_PENDING = BootstrapBlocker(
    "evaluation_already_pending",
    "An evaluation is already queued or running for this merchant. It is measuring the setup you"
    " have now, so wait for it to finish before building a new one.",
)

_RUN_ACTIVE = BootstrapBlocker(
    "run_already_active",
    "A benchmark run is executing against this merchant's world. Wait for it to finish before"
    " building a new evaluation setup.",
)
