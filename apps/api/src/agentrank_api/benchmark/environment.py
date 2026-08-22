"""Registering a benchmark world, and putting it back the way the fixture describes it.

A benchmark mission is not a read. A successful one creates a mandate, quotes a checkout, holds
stock, pays for it and takes the units off the shelf permanently. Every one of those is a
mutation of the world the next mission is about to observe.

Left alone, that makes a suite order dependent. Mission N buys the last black charger and
mission N+1, whose oracle says one was available, is marked down for a discovery failure it
never had a chance at. Run the same suite twice and the second run measures a poorer merchant
than the first. Neither result reproduces, and a before and after comparison across them is not
a comparison at all.

The answer here is a prepared world rather than a cleverer runner. Before every mission, and
before the run itself, the merchant's catalog is put back to exactly what the fixture describes
and everything an earlier mission was holding is given back. Each mission then observes the
intended initial state, whatever ran before it and however many times the suite has been run.

Three rules make that safe rather than merely convenient.

Preparation is refused for any merchant that has not been deliberately registered as a
benchmark world. Overwriting a catalog and releasing stock is exactly right for a fixture and
catastrophic for a real merchant, and the difference between the two must not be a command line
argument somebody gets wrong. It is a row, written on purpose, and its absence is a refusal.

Preparation is refused while a payment is still holding stock without a definitive answer. A
COMMITTED reservation is one a payment attempt was admitted against, and releasing it would be
performing an operator abandonment silently: giving the units back while a provider may yet
reveal that the money moved. Nothing here is willing to do that. The refusal names the payment,
and `agentrank_api.cli payments` is where it gets resolved.

And preparation converges rather than deletes. The catalog is seeded from the fixture, which
updates the rows that exist and creates the ones that do not; anything the fixture no longer
names is withdrawn and emptied rather than removed, because a historical mission result holds a
RESTRICT reference to the variant it selected and a benchmark that erased its own evidence
between runs would be a benchmark nobody could audit. Withdrawing is what makes the world
actually be what the fixture describes: without it, publishing a fixture version that drops a
product would leave that product purchasable forever, and every later run would be measured
against a catalog wider than the one somebody authored.

What preparation deliberately does not do is remove the mandates, quotes and payments earlier
missions created. Those are records of what happened and they are what a mission result points
at. They also cannot affect a later mission: a mandate authorizes one purchase and every mission
creates its own, and a checkout and a payment are scoped to the mandate and the quote they
belong to. The only two things an earlier mission can change about a later one's world are the
stock on the shelf and the claims against it, and both are what this puts back.

Two preparations of one world serialize on a transaction scoped advisory lock, taken before any
row lock and keyed on the merchant. Without it two first preparations of a brand new world both
find no variant rows to lock, both seed, and the loser gets an integrity error rather than a
turn. The lock does not make two whole benchmark runs safe against each other, which is a
different claim and is written down in docs/shortcomings.md.
"""

import uuid
from dataclasses import dataclass

from sqlalchemy import or_, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from agentrank_api.benchmark.fixtures import BenchmarkFixture
from agentrank_api.benchmark.models import BenchmarkEnvironment
from agentrank_api.benchmark.repository import (
    BenchmarkEnvironmentRepository,
    BenchmarkRunRepository,
)
from agentrank_api.commerce.catalog_fixture import SeedSummary, seed_catalog
from agentrank_api.commerce.models import Product, Variant
from agentrank_api.commerce.repository import MerchantRepository
from agentrank_api.conflicts import translated_conflicts
from agentrank_api.errors import ConflictError, NotFoundError
from agentrank_api.inventory.models import HOLDING_STATUSES, ReservationStatus
from agentrank_api.inventory.repository import InventoryReservationRepository
from agentrank_api.inventory.service import InventoryReservationService, ReleaseReason

ENVIRONMENT_RESOURCE = "benchmark_environment"

# The refusal `conflicts.py` produces when two registrations of one brand new fixture version
# race. Named here because this service is what turns losing that race back into an answer.
ALREADY_REGISTERED = "environment_already_registered"


@dataclass(frozen=True, slots=True)
class PreparedEnvironment:
    """What putting one world back produced.

    `released_holds` is zero on a clean preparation and is worth reporting when it is not: a
    hold left standing means the mission that took it did not finish, which is a fact about the
    previous run rather than about the catalog.

    `catalog` is the seeding summary, and `catalog.created` is zero on every preparation after
    the first. A non zero value on a later one means rows were missing, which is worth seeing.

    `withdrawn` counts what the fixture no longer names and this preparation took off the shelf.
    Zero on every preparation of an unchanged world, and non zero exactly once after a fixture
    version drops something, which is a fact a report should be able to show rather than one the
    harness performs silently.
    """

    environment: BenchmarkEnvironment
    catalog: SeedSummary
    released_holds: int = 0
    withdrawn: int = 0


class BenchmarkEnvironmentService:
    """Register a benchmark world, and restore one to its intended initial state."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._merchants = MerchantRepository(session)
        self._environments = BenchmarkEnvironmentRepository(session)
        self._runs = BenchmarkRunRepository(session)
        self._reservations = InventoryReservationRepository(session)
        self._inventory = InventoryReservationService(session)

    async def register(self, fixture: BenchmarkFixture) -> BenchmarkEnvironment:
        """Mark the merchant this fixture describes as a benchmark world, or return the
        registration that already exists.

        Three outcomes and no fourth, which is exactly how a suite is published:

        - nothing is registered under this key and version, so the world is registered
        - something is and its content hash matches, so it is returned untouched
        - something is and its content hash differs, so the call is refused

        The third is what stops an edited fixture from rewriting what a historical run was
        measured against. The row cannot be updated, so the only way an existing version could
        come to mean a different world is by being replaced, and there is no path here that
        replaces one.

        The merchant is created if it does not exist. That is the one act of creation this
        service performs, and it is what makes a benchmark world reachable from an empty
        database without anybody first seeding a merchant by hand and hoping the slug matches.

        Two registrations of one merchant's worlds serialize on the same advisory lock
        preparation takes. Without it both can find no merchant, both create one, and the loser
        gets an integrity error on the merchant's slug rather than the registration the winner
        wrote.
        """
        await self._claim(fixture.merchant_slug)
        content_hash = fixture.content_hash
        existing = await self._environments.get(fixture.key, fixture.version)
        if existing is not None:
            _require_same_content(existing, content_hash)
            return existing

        merchant = await self._merchants.get_by_slug(fixture.merchant_slug)
        if merchant is None:
            merchant = await self._merchants.create(
                slug=fixture.merchant_slug, name=fixture.merchant_name
            )

        try:
            async with translated_conflicts(self._session, identifier=fixture.label):
                environment = await self._environments.create(merchant=merchant, fixture=fixture)
        except ConflictError as conflict:
            if conflict.reason != ALREADY_REGISTERED:
                raise
            return await self._registered_by_the_winner(fixture, content_hash)

        await self._session.commit()
        return environment

    async def prepare(
        self, fixture: BenchmarkFixture, *, for_run: uuid.UUID | None = None
    ) -> PreparedEnvironment:
        """Put this world back to exactly what the fixture describes, in one transaction.

        Fail closed three times before anything is written. The world has to be registered under
        this exact fixture key, version and content hash, so a merchant nobody deliberately made
        a benchmark target cannot be overwritten and an edited fixture cannot be applied under an
        identity that no longer describes it. Nothing may be holding stock against a payment that
        has not resolved, because giving those units back is an operator decision with residual
        risk rather than a housekeeping step. And no run other than `for_run` may be executing
        against this merchant.

        `for_run` is the run this preparation is part of, and passing it is what lets a run put
        the world back between its own missions. Every other caller passes nothing and is
        refused while a run is executing, which is the half of exclusivity a unique index on the
        run table cannot state: an operator reseeding the world mid run does not create a second
        run, it destroys the first one's shelf from underneath it.

        The order is the one `agentrank_api.locking` writes down. Every one of this merchant's
        variant rows is locked first, in ascending identifier order, so this serializes against
        any preparation or payment admission touching the same shelf. The holds are released
        next, and the catalog is written last, under locks that were already held.

        Convergent. Preparing a world that is already in its intended state releases nothing,
        creates nothing and rewrites the same values, so the catalog pin a run takes afterwards
        is the same digest every time.
        """
        await self._claim(fixture.merchant_slug)
        environment = await self.require_registered(fixture)
        if environment.merchant_slug != fixture.merchant_slug:
            # Not reachable through the schema: the composite foreign key onto
            # (merchant.id, merchant.slug) refuses a rename while this registration exists, so
            # the slug on the row is the merchant's own. Stated anyway, because the whole of
            # production safety here is that the merchant being overwritten is the registered
            # one rather than whichever shop happens to answer to a name today.
            raise ConflictError(
                "fixture_merchant_changed",
                f"benchmark environment {environment.label} is registered for merchant"
                f" {environment.merchant_slug!r} and this fixture describes"
                f" {fixture.merchant_slug!r}",
                resource=ENVIRONMENT_RESOURCE,
                identifier=environment.label,
            )

        merchant_id = environment.merchant_id
        await self._require_unclaimed(merchant_id, for_run=for_run)
        await self._lock_shelf(merchant_id)
        released = await self._release_holds(merchant_id)
        summary = await seed_catalog(
            self._session,
            # The registered merchant's own slug rather than the fixture's copy of it. They are
            # equal by the check above, and reading it from the row is what makes that provable
            # at the line that does the overwriting.
            slug=environment.merchant_slug,
            name=fixture.merchant_name,
            products=fixture.products,
        )
        withdrawn = await self._withdraw_unnamed(merchant_id, fixture)
        await self._session.commit()
        return PreparedEnvironment(
            environment=environment,
            catalog=summary,
            released_holds=released,
            withdrawn=withdrawn,
        )

    async def require_registered(self, fixture: BenchmarkFixture) -> BenchmarkEnvironment:
        """The registration for this exact fixture, raising rather than returning None.

        The fail closed identity check, and the whole of production safety here. A world that
        was never registered has no row, and a fixture that has been edited since it was
        registered no longer hashes to what the row says. Both refuse, and neither refusal can
        be argued out of by a command line argument.
        """
        environment = await self._environments.get(fixture.key, fixture.version)
        if environment is None:
            raise NotFoundError(ENVIRONMENT_RESOURCE, fixture.label)
        _require_same_content(environment, fixture.content_hash)
        return environment

    async def registered_for(self, merchant_id: uuid.UUID) -> list[BenchmarkEnvironment]:
        """Every world registered for one merchant, oldest first.

        An empty list means this merchant is not a benchmark target, which is the answer for
        every merchant nobody has deliberately registered.
        """
        return await self._environments.list_for_merchant(merchant_id)

    async def _registered_by_the_winner(
        self, fixture: BenchmarkFixture, content_hash: str
    ) -> BenchmarkEnvironment:
        """Resolve a lost registration race by reading what the winner wrote.

        The transaction was already rolled back by the conflict translation, so this read sees
        the committed row. A version that is somehow still absent means the constraint fired
        for a reason nobody can explain, and that is a bug rather than a refusal.
        """
        existing = await self._environments.get(fixture.key, fixture.version)
        if existing is None:
            raise ConflictError(
                ALREADY_REGISTERED,
                f"benchmark environment {fixture.label} could not be registered or read back",
                resource=ENVIRONMENT_RESOURCE,
                identifier=fixture.label,
            )
        _require_same_content(existing, content_hash)
        return existing

    async def _require_unclaimed(
        self, merchant_id: uuid.UUID, *, for_run: uuid.UUID | None
    ) -> None:
        """Refuse to overwrite a world that a run other than this one is executing against.

        A benchmark run is long, so its claim cannot be a held transaction and is a row status
        instead. That leaves one gap a unique index does not cover: this method is the only
        thing that resets a shelf, and nothing in the run table stops somebody calling it while
        a run is halfway through. It is the same damage two concurrent runs would do, arriving
        by a shorter route.

        A run left RUNNING by a process that died blocks this exactly as a live one does, and
        that is the point rather than a side effect. What that run did is unknown; resetting the
        world would release whatever it was holding and destroy the evidence. `benchmark abort`
        is the auditable act that says reuse is safe.
        """
        active = await self._runs.active_run_id(merchant_id=merchant_id)
        if active is None or active == for_run:
            return
        raise ConflictError(
            "run_already_active",
            f"benchmark run {active} is executing against this merchant and its world cannot"
            " be reset until that run is completed or aborted",
            resource=ENVIRONMENT_RESOURCE,
            identifier=str(active),
        )

    async def _claim(self, merchant_slug: str) -> None:
        """Hold this merchant's benchmark world against every other preparation, until commit.

        An advisory lock rather than a row lock, and that is the point: the rows a preparation
        would lock are the ones it may be about to create, so on a brand new world there is
        nothing to serialize on. Two first preparations would both find no variants, both seed,
        and the loser would get an integrity error on a unique SKU instead of a turn.

        Transaction scoped, so it is released by the commit or the rollback that ends this
        operation and there is nothing to leak. Taken before any row lock, so it cannot
        participate in a cycle with the order `agentrank_api.locking` writes down.

        Keyed on the merchant slug rather than on the fixture, because two fixture versions of
        one merchant's world are still one shelf.
        """
        await self._session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
            {"key": f"{ENVIRONMENT_RESOURCE}:{merchant_slug}"},
        )

    async def _withdraw_unnamed(self, merchant_id: uuid.UUID, fixture: BenchmarkFixture) -> int:
        """Take everything the fixture does not name off the shelf, and report how much.

        Withdrawn and emptied rather than deleted. A historical mission result holds a RESTRICT
        reference to the variant it selected, so removing one would either fail or take the
        evidence with it, and a benchmark that erases what it measured is not auditable.
        Deactivating and zeroing achieves what preparation is actually for: nothing a buyer can
        reach that the fixture did not describe.

        This is what makes a fixture version that drops a product mean something. Seeding alone
        only writes what the fixture names, so the dropped product would stay active and stocked,
        would enter the catalog pin, would be discoverable by an executor, and could satisfy a
        mission the fixture's author had removed the answer for.

        Returns the number of variant rows this call actually changed. Zero on every preparation
        of an unchanged world.
        """
        skus = [variant.sku for product in fixture.products for variant in product.variants]
        external_ids = [product.external_id for product in fixture.products]

        # The variant rows are already held by `_lock_shelf`, which locked every one of this
        # merchant's variants rather than only the ones the fixture names.
        stale = (
            await self._session.execute(
                select(Variant.id).where(
                    Variant.merchant_id == merchant_id,
                    Variant.sku.notin_(skus),
                    or_(Variant.is_active.is_(True), Variant.inventory_quantity > 0),
                )
            )
        ).scalars()
        withdrawn = list(stale)
        if withdrawn:
            await self._session.execute(
                update(Variant)
                .where(Variant.id.in_(withdrawn))
                .values(is_active=False, inventory_quantity=0)
            )
        await self._session.execute(
            update(Product)
            .where(
                Product.merchant_id == merchant_id,
                Product.external_id.notin_(external_ids),
                Product.is_active.is_(True),
            )
            .values(is_active=False)
        )
        return len(withdrawn)

    async def _lock_shelf(self, merchant_id: uuid.UUID) -> None:
        """Hold every one of this merchant's variant rows until commit.

        Ascending identifier order, taken by the same repository method every reservation takes
        them with, so a preparation and a payment admission on the same shelf queue rather than
        deadlock. Nothing here reads the stock the lock reports: what this needs is that no
        other transaction is deciding anything about these rows while the fixture is written
        back over them.
        """
        statement = (
            select(Variant.id).where(Variant.merchant_id == merchant_id).order_by(Variant.id)
        )
        variant_ids = list((await self._session.execute(statement)).scalars())
        if variant_ids:
            await self._reservations.lock_variants(merchant_id=merchant_id, variant_ids=variant_ids)

    async def _release_holds(self, merchant_id: uuid.UUID) -> int:
        """Give back everything this merchant is still holding, and refuse to guess.

        An ACTIVE hold belongs to a mission that stopped before it paid: no payment attempt was
        ever admitted against it, because admission commits a hold in the same transaction that
        writes the attempt. Nothing is in doubt, so the units go back and the trail says
        `benchmark_world_prepared`, which is its own reason precisely because this is routine.
        Filing it under `reservation_recovered` would fill the trail with the one code that is
        supposed to mean the execution locking failed.

        A COMMITTED hold is refused outright. Something was admitted against it and has not
        reached a definitive answer, so releasing it would give the stock back under a payment
        that may have gone through. That is an operator decision with residual risk attached,
        it has its own command and its own reason code, and a benchmark preparing its next
        mission must never make it by accident.
        """
        # Read, then re-read each one under its own row lock before deciding anything. The
        # variant locks this operation holds do not serialize it against a release: a decline
        # and an operator abandonment both release stock without ever wanting a variant lock,
        # so a status read outside the reservation lock can be stale by the time it is acted
        # on, and acting on a stale ACTIVE means the reservation guard aborts the whole
        # preparation with an error nobody can explain.
        found = await self._reservations.list_holding_for_merchant(merchant_id)
        holds = [
            locked
            for candidate in found
            if (locked := await self._reservations.get_for_update(candidate.id)) is not None
            and locked.status in HOLDING_STATUSES
        ]
        committed = [hold for hold in holds if hold.status is ReservationStatus.COMMITTED]
        if committed:
            raise ConflictError(
                "payment_in_progress",
                f"benchmark merchant {merchant_id} has {len(committed)} reservations held"
                " against payments that have not resolved. Resolve them with"
                " `agentrank_api.cli payments` before preparing this world again",
                resource=ENVIRONMENT_RESOURCE,
                identifier=str(merchant_id),
            )

        released = 0
        for hold in holds:
            if await self._inventory.release(hold, reason=ReleaseReason.BENCHMARK_WORLD_PREPARED):
                released += 1
        return released


def _require_same_content(environment: BenchmarkEnvironment, content_hash: str) -> None:
    """Refuse a fixture that would give an existing world new meaning.

    The message names both digests on purpose. An author who edited a catalog and forgot to
    bump the version needs to see that the content changed, not merely that something is
    already registered.
    """
    if environment.fixture_hash == content_hash:
        return
    raise ConflictError(
        "fixture_definition_changed",
        f"benchmark environment {environment.label} is registered with content"
        f" {environment.fixture_hash} and this fixture is {content_hash}."
        " Register a new version rather than changing what an existing one measured",
        resource=ENVIRONMENT_RESOURCE,
        identifier=environment.label,
    )
