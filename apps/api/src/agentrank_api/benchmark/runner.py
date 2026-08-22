"""Executing a benchmark suite against one merchant, and recording what happened.

This is a runner, not a buyer. It knows how to start a run, hand an executor one mission brief
at a time, mark what comes back and close the run. It has no opinion about how a mission is
carried out, no LLM, no prompt and no model identifier, and the seam it offers is
`MissionExecutor`, which lives in `agentrank_api.benchmark.execution` rather than here. That
separation is deliberate: this module imports the evaluator and the mission definitions, and an
executor importing its own protocol from here would have both one attribute access away.

Four things happen here that could not happen in the pure evaluator, and each is why this module
exists at all rather than being a loop somebody writes twice.

The run is pinned. Its suite gives it a workload identity, and `catalog_hash` gives it the other
half: what the merchant's authoritative data looked like when it started. Without that, a before
and after comparison attributes every difference to whatever was changed on purpose.

The oracle is checked. Each mission's authored ground truth is recomputed against the merchant's
own rows, and a disagreement is recorded rather than acted on.

The commerce artifacts are resolved. A recorded variant, quote or payment reference is one this
merchant really has, because the composite foreign keys refuse anything else and this looks them
up rather than writing what an executor claimed.

And the whole thing is ordered and isolated. Missions run in suite order, one at a time, and the
merchant's world is put back to what the fixture describes before the run and before every
mission. That is what makes a mission's outcome independent of what ran before it, a second run
independent of the first, and a suite's result independent of the order its missions happened to
be presented in.

Sequential on purpose. Parallel execution would add resource races on one shelf, inventory
interactions between missions that are supposed to be independent, and a recovery story nobody
needs yet. Nothing here forbids parallel execution later; it is simply not built.

Crash recovery is deliberately narrow and is stated rather than implied:

```text
mission PENDING     never started, no commerce side effect from it exists
mission RUNNING     started, and what it did is unknown. Never replayed
run RUNNING         finish it with complete_run if every mission is terminal,
                    otherwise close it honestly with abort_run
```

There is no automatic resume, and the reason is money. A mission left RUNNING may have created a
quote, held stock and dispatched a payment, and re-executing it blindly is how a benchmark buys
something twice. Recovery is an operator reading the run and deciding, and the operator's tool is
the command line.
"""

import logging
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agentrank_api.benchmark.catalog import CatalogEntry, catalog_content_hash, facts_for
from agentrank_api.benchmark.definitions import AgentMissionBrief
from agentrank_api.benchmark.environment import BenchmarkEnvironmentService
from agentrank_api.benchmark.evaluation import (
    MissionEvaluation,
    evaluate_mission,
    evaluator_version,
)
from agentrank_api.benchmark.execution import ExecutorIdentity, MissionExecutor
from agentrank_api.benchmark.fixtures import BenchmarkFixture
from agentrank_api.benchmark.lifecycle import BenchmarkRunStatus, MissionRunStatus
from agentrank_api.benchmark.metrics import BenchmarkMetrics, MissionOutcome, compute_metrics
from agentrank_api.benchmark.models import (
    BenchmarkEnvironment,
    BenchmarkMission,
    BenchmarkMissionRun,
    BenchmarkRun,
)
from agentrank_api.benchmark.observation import ObservedResult
from agentrank_api.benchmark.repository import BenchmarkRunRepository, BenchmarkSuiteRepository
from agentrank_api.checkout.models import CheckoutSession
from agentrank_api.commerce.models import Product, Variant
from agentrank_api.commerce.repository import MerchantRepository
from agentrank_api.conflicts import translated_conflicts
from agentrank_api.errors import ConflictError, NotFoundError
from agentrank_api.payments.models import PaymentAttempt, PaymentAttemptStatus

RUN_RESOURCE = "benchmark_run"

# Structured fields only, and deliberately no oracle. A mission line carries which run, which
# mission run and which mission key, all of which a person needs to find a row again, and
# nothing about how the mission was marked.
#
# That last part is not tidiness and it was got wrong first. A status and a failure reason are
# the oracle decoded: an abstention with a reason means the ground truth said a purchase was
# available, an abstention without one means it said none was, and a success means the same as
# the first. An in process executor can attach a filter to this logger, read fourteen labelled
# answers out of one run, and replay them against the next run of the same immutable suite
# without opening the catalog. `logging` is not a plausible thing to forbid an executor from
# importing, so the fix is to not write the answer down while the run is still going.
log = logging.getLogger(__name__)


class BenchmarkRunService:
    """The one path a benchmark run goes through.

    Merchant scoped in every read, exactly as the repository underneath it is, and it commits
    per mission rather than per run: a run that stops halfway leaves the missions it finished
    recorded and the rest PENDING, which is what ABORTED then describes honestly.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._merchants = MerchantRepository(session)
        self._suites = BenchmarkSuiteRepository(session)
        self._runs = BenchmarkRunRepository(session)
        self._environments = BenchmarkEnvironmentService(session)

    async def start_run(
        self,
        *,
        suite_key: str,
        suite_version: int,
        merchant_slug: str,
        environment: BenchmarkEnvironment | None = None,
        executor: ExecutorIdentity | None = None,
        representation_label: str | None = None,
    ) -> BenchmarkRun:
        """Create a run, pin it, and mark it RUNNING.

        The merchant is named by slug rather than by identifier because the suite names one that
        way too, and the two have to be compared. The repository refuses the mismatch.

        The catalog pin and the evaluator version are written before any mission runs, so they
        describe what the run was actually measured against rather than what the merchant looked
        like when somebody got round to reading the report.

        `environment` is the registered world this run is being measured against, and it is the
        third pin: the suite says which missions, the catalog hash says what the shelf actually
        looked like, and this says which authored target it was supposed to be. It is optional
        because a run against an ad hoc merchant has no registered world, and null then means
        exactly that rather than that the target was fine.

        `executor` is the fourth, and it is the one nothing else can stand in for. Two runs of one
        suite against one world can still differ because the thing doing the shopping changed, and
        without this they would be compared as though they had not. Also optional, and null there
        means the same thing every other null pin means: nobody recorded it.

        Refused while another run is already executing against this merchant. That is the whole
        of environment exclusivity at this layer, and the read is the courtesy rather than the
        mechanism: the partial unique index underneath refuses the same thing across processes,
        and the transition to RUNNING is what enters it. Both statements are in one transaction,
        so a second starter blocks on the index until the first commits and then loses.
        """
        merchant = await self._merchants.get_by_slug(merchant_slug)
        if merchant is None:
            raise NotFoundError("merchant", merchant_slug)
        suite = await self._suites.get(suite_key, suite_version)
        if suite is None:
            raise NotFoundError("benchmark_suite", f"{suite_key}@{suite_version}")

        await self._require_unclaimed(merchant.id)
        entries = await self.catalog(merchant.id)
        run = await self._runs.create(
            merchant=merchant,
            suite=suite,
            environment=environment,
            executor=executor,
            representation_label=representation_label,
            catalog_hash=catalog_content_hash(entries),
            evaluator_version=evaluator_version(),
        )
        run.status = BenchmarkRunStatus.RUNNING
        run.started_at = datetime.now(UTC)
        async with translated_conflicts(self._session, identifier=str(merchant.id)):
            await self._session.commit()
        log.info(
            "benchmark run started",
            extra={
                "benchmark_run_id": str(run.id),
                "suite": suite.label,
                "merchant_id": str(merchant.id),
                "executor": None if executor is None else executor.label,
                "environment": None if environment is None else environment.label,
                "catalog_hash": run.catalog_hash,
                "missions": len(suite.missions),
            },
        )
        return run

    async def run_suite(
        self,
        executor: MissionExecutor,
        *,
        suite_key: str,
        suite_version: int,
        fixture: BenchmarkFixture,
        representation_label: str | None = None,
    ) -> BenchmarkRun:
        """Prepare the world, execute every mission in suite order, and complete the run.

        The fixture is required and is the whole difference between this and a loop. Every
        mission is executed against a world that has just been put back to exactly what the
        fixture describes, so a mission's outcome does not depend on what ran before it, a
        second run does not inherit the first one's stock, and the order the missions happen to
        be presented in changes nothing.

        The world is prepared once before the run as well, so the catalog pin taken with the run
        describes the intended initial state rather than whatever was left over.

        The catalog is read once per mission, *before* the mission executes, and that reading is
        what the mission is marked against. Reading it afterwards would mark a mission against a
        catalog the mission itself had just changed, which for a mission that bought the last
        unit of something would report a stale oracle that was never stale.

        A mission is committed as RUNNING before the executor is handed anything. Nothing reads
        that state during an ordinary run, and it is the only thing a crash leaves behind that
        distinguishes "this never started" from "this started and what it did is unknown". The
        second must never be replayed, and without the transition there would be no way to tell.

        Anything the executor raises propagates, having left the mission RUNNING. That is
        deliberate: a runner that swallowed an exception and carried on would produce a
        COMPLETED run with a mission nobody executed, and the honest close for a run that
        stopped is `abort_run`.

        The world is claimed for the whole run. The first preparation happens before the run
        exists and is refused if another run already owns this merchant, so a stale RUNNING run
        stops the world being reset before anything of this one is written down. Every later
        preparation names this run and is therefore allowed, and is refused for everybody else.
        """
        environment = await self._environments.prepare(fixture)
        run = await self.start_run(
            suite_key=suite_key,
            suite_version=suite_version,
            merchant_slug=fixture.merchant_slug,
            environment=environment.environment,
            executor=executor.identity,
            representation_label=representation_label,
        )
        suite = await self._suites.get_by_id(run.suite_id)
        assert suite is not None  # start_run resolved it a moment ago
        merchant_id = run.merchant_id
        run_id = run.id

        for mission in suite.missions:
            await self._environments.prepare(fixture, for_run=run_id)
            entries = await self.catalog(merchant_id)
            brief = mission.to_brief()
            started = await self.start_mission(run_id, brief.key, merchant_id=merchant_id)
            log.info(
                "benchmark mission started",
                extra={
                    "benchmark_run_id": str(run_id),
                    "mission_run_id": str(started),
                    "mission_key": brief.key,
                },
            )
            observed = await executor(brief, merchant_id=merchant_id)
            result = await self.record_result(
                run_id, brief.key, observed, merchant_id=merchant_id, catalog=entries
            )
            log.info(
                "benchmark mission recorded",
                extra={
                    "benchmark_run_id": str(run_id),
                    "mission_run_id": str(result.id),
                    "mission_key": brief.key,
                },
            )

        finished = await self.complete_run(run_id, merchant_id=merchant_id)
        # Counts, at the end, and never per mission. What a mission was marked as is the oracle
        # decoded: an abstention with a reason means the ground truth said a purchase was
        # available and one without means it said none was. Logged per mission, an in process
        # executor could read its own answers out of a handler and replay them on the next run
        # of the same immutable suite. Counts after the last mission cannot inform the run they
        # describe and do not identify which mission was which.
        counts = compute_metrics(outcomes_of(finished.mission_runs))
        log.info(
            "benchmark run completed",
            extra={
                "benchmark_run_id": str(run_id),
                "missions_total": counts.missions_total,
                "missions_succeeded": counts.missions_succeeded,
                "missions_failed": counts.missions_failed,
                "missions_abstained": counts.missions_abstained,
                "missions_errored": counts.missions_errored,
            },
        )
        return finished

    async def start_mission(
        self, run_id: uuid.UUID, mission_key: str, *, merchant_id: uuid.UUID
    ) -> uuid.UUID:
        """Mark one mission as started, and commit that before anything is handed an executor.

        The commit is the point. Nothing in an ordinary run reads this state, and it costs one
        statement; what it buys is that a crash leaves a mission in one of two distinguishable
        states rather than one ambiguous one. PENDING means the executor was never called and
        nothing it would have done exists. RUNNING means it was called and what it did is
        unknown, which is the state that must never be replayed because it may have paid for
        something.

        Refuses a mission that has already produced a result, exactly as recording one does.
        Starting an already started mission is allowed and changes nothing, because that is what
        a retry after a crash looks like and refusing it would be refusing to describe the state
        it is in.
        """
        run = await self._locked_run(run_id, merchant_id=merchant_id)
        self._require_running(run)
        result = await self._mission_run(run, mission_key, merchant_id=merchant_id)
        if result.is_terminal:
            raise ConflictError(
                "mission_already_recorded",
                f"mission {mission_key} in run {run.id} has already been executed",
                resource=RUN_RESOURCE,
                identifier=str(run.id),
            )
        if result.status is MissionRunStatus.PENDING:
            result.status = MissionRunStatus.RUNNING
            result.started_at = datetime.now(UTC)
        result_id = result.id
        await self._session.commit()
        return result_id

    async def record_result(
        self,
        run_id: uuid.UUID,
        mission_key: str,
        observed: ObservedResult,
        *,
        merchant_id: uuid.UUID,
        catalog: Sequence[CatalogEntry] | None = None,
    ) -> BenchmarkMissionRun:
        """Mark one mission and write the result, in one transaction.

        The mission run is locked before anything is decided, so two executors reporting the
        same mission queue rather than both reading PENDING. The database refuses the second
        write either way; the lock is what turns that into an ordinary refusal.

        `catalog` is the merchant's data as the mission found it, and passing it is what makes
        the oracle recomputation honest. A mission that bought the last unit of something has
        changed the catalog by the time it reports, and reading the catalog here would compare
        the mission's own ground truth against a shelf the mission itself emptied. `run_suite`
        reads it before executing and passes it down. Reading it here is the fallback for a
        caller assembling a run by hand, and it is the older, weaker behavior.
        """
        run = await self._locked_run(run_id, merchant_id=merchant_id)
        self._require_running(run)

        mission = await self._mission(run.suite_id, mission_key)
        result = await self._runs.get_mission_run(run.id, mission.id, merchant_id=merchant_id)
        if result is None:
            raise NotFoundError("benchmark_mission_run", mission_key)
        if result.is_terminal:
            raise ConflictError(
                "mission_already_recorded",
                f"mission {mission_key} in run {run.id} has already been executed",
                resource=RUN_RESOURCE,
                identifier=str(run.id),
            )

        entries = await self.catalog(merchant_id) if catalog is None else catalog
        evaluation = evaluate_mission(
            mission.to_definition(),
            observed,
            merchant_id=merchant_id,
            catalog=facts_for(mission.to_brief(), entries, observed.selection),
        )

        # RUNNING first, because the lifecycle is a transition whitelist and an outcome is
        # produced by a transition rather than written. Both statements are in one transaction,
        # so nothing observes the intermediate state. A mission `start_mission` already
        # transitioned keeps the instant it was started at, which the guard requires: a start
        # time that moved would be a run rewriting when its own work began.
        now = datetime.now(UTC)
        if result.status is MissionRunStatus.PENDING:
            result.status = MissionRunStatus.RUNNING
            result.started_at = now
        await self._session.flush()

        # References before the outcome, and the order is load bearing. Resolving them issues
        # reads, every read autoflushes, and the guard refuses any update at all once the status
        # is terminal. Writing the outcome last means the terminal transition is the last thing
        # this row ever sees.
        await self._resolve_references(result, observed, merchant_id=merchant_id)
        _apply(result, evaluation, at=now)
        await self._session.commit()
        return result

    async def complete_run(self, run_id: uuid.UUID, *, merchant_id: uuid.UUID) -> BenchmarkRun:
        """Close a run whose every mission reached a terminal state.

        Refuses otherwise. A run reported as COMPLETED is the only one whose numbers describe
        the whole workload, and a partial run presented as a complete one would report a rate
        over a denominator nobody chose. The honest close for that is `abort_run`.

        Every mission has to be terminal, which includes ERRORED. A run in which the harness
        failed on one mission still attempted and persisted every mission, so it describes the
        whole workload and completes; the errored mission is reported beside every rate and is
        never folded into one. A mission still RUNNING is not terminal and refuses here, because
        what it did is unknown.
        """
        run = await self._loaded_run(run_id, merchant_id=merchant_id)
        unfinished = [result.id for result in run.mission_runs if not result.is_terminal]
        if unfinished:
            raise ConflictError(
                "run_incomplete",
                f"benchmark run {run.id} has {len(unfinished)} missions that never executed",
                resource=RUN_RESOURCE,
                identifier=str(run.id),
            )
        return await self._finish(run, BenchmarkRunStatus.COMPLETED)

    async def abort_run(self, run_id: uuid.UUID, *, merchant_id: uuid.UUID) -> BenchmarkRun:
        """Close a run that stopped early, saying so.

        Its own status rather than COMPLETED with fewer results. Everything recorded before it
        stopped stays recorded and stays true; what changes is that the run no longer claims to
        describe the whole suite.
        """
        run = await self._loaded_run(run_id, merchant_id=merchant_id)
        return await self._finish(run, BenchmarkRunStatus.ABORTED)

    async def load(self, run_id: uuid.UUID, *, merchant_id: uuid.UUID) -> BenchmarkRun:
        """One merchant's run with its mission runs and their definitions, raising if absent.

        The read a report is built from. Merchant scoped like every other read here, so a run
        identifier is worth nothing to anybody who is not its merchant.
        """
        return await self._loaded_run(run_id, merchant_id=merchant_id)

    async def suite_label(self, run: BenchmarkRun) -> str:
        """Which workload this run executed, as `key@version`.

        Read from the suite the run points at rather than from whatever a caller expected. A run
        started earlier can name a version nobody defaults to any more, and a report that printed
        the expectation instead of the row would be the wrong kind of report.
        """
        suite = await self._suites.get_by_id(run.suite_id)
        if suite is None:
            # Not reachable through the schema: the foreign key onto the suite is RESTRICT.
            raise NotFoundError("benchmark_suite", str(run.suite_id))
        return suite.label

    async def environment_label(self, run: BenchmarkRun) -> str | None:
        """Which registered world this run was measured against, or None when there was none.

        None is the honest answer for a run against a merchant nobody registered, and it is not
        the same as a world whose name nobody recorded.
        """
        if run.environment_id is None:
            return None
        environment = await self._session.get(BenchmarkEnvironment, run.environment_id)
        if environment is None:
            # Not reachable: a registered world cannot be deleted, and the run's reference is
            # RESTRICT besides.
            raise NotFoundError("benchmark_environment", str(run.environment_id))
        return environment.label

    async def metrics(self, run_id: uuid.UUID, *, merchant_id: uuid.UUID) -> BenchmarkMetrics:
        """Count one merchant's run.

        Derived from the mission runs every time rather than stored beside them, so a report
        cannot disagree with the rows it summarises. The suite definitions come with them, which
        is what supplies the denominators.
        """
        run = await self._loaded_run(run_id, merchant_id=merchant_id)
        return compute_metrics([_outcome(result) for result in run.mission_runs])

    async def catalog(self, merchant_id: uuid.UUID) -> list[CatalogEntry]:
        """One merchant's purchasable surface, as the benchmark reads it.

        Every variant, including inactive and out of stock ones, because "the merchant does not
        sell this" and "the merchant has run out" are different findings and both need the row
        to be visible. Ordered by SKU so the pin does not depend on how the rows came back.
        """
        statement = (
            select(
                Variant.id,
                Variant.sku,
                Product.category,
                Variant.attributes,
                Variant.price_amount_minor,
                Variant.currency,
                Variant.inventory_quantity,
                Variant.is_active,
                Product.is_active,
            )
            .join(Product, Product.id == Variant.product_id)
            .where(Variant.merchant_id == merchant_id)
            .order_by(Variant.sku)
        )
        rows = (await self._session.execute(statement)).all()
        return [
            CatalogEntry(
                variant_id=row[0],
                sku=row[1],
                product_category=row[2],
                attributes=row[3],
                price_amount_minor=row[4],
                currency=row[5],
                inventory_quantity=row[6],
                # A variant of a withdrawn product is not for sale either, and folding the two
                # here is what stops a mission qualifying against something no buyer can reach.
                is_active=row[7] and row[8],
            )
            for row in rows
        ]

    async def _finish(self, run: BenchmarkRun, status: BenchmarkRunStatus) -> BenchmarkRun:
        if run.is_terminal:
            raise ConflictError(
                "run_already_finished",
                f"benchmark run {run.id} is already {run.status.value.lower()}",
                resource=RUN_RESOURCE,
                identifier=str(run.id),
            )
        run.status = status
        run.completed_at = datetime.now(UTC)
        await self._session.commit()
        return run

    async def _loaded_run(self, run_id: uuid.UUID, *, merchant_id: uuid.UUID) -> BenchmarkRun:
        run = await self._runs.get(run_id, merchant_id=merchant_id)
        if run is None:
            raise NotFoundError(RUN_RESOURCE, str(run_id))
        return run

    async def _locked_run(self, run_id: uuid.UUID, *, merchant_id: uuid.UUID) -> BenchmarkRun:
        run = await self._runs.get_for_update(run_id, merchant_id=merchant_id)
        if run is None:
            raise NotFoundError(RUN_RESOURCE, str(run_id))
        return run

    async def _require_unclaimed(self, merchant_id: uuid.UUID) -> None:
        """Refuse to start a run against a merchant a run is already executing against.

        Two runs against one world are not two measurements. Each prepares the world before
        every mission, so process B's preparation resets the shelf process A is halfway through
        shopping and releases what A was holding; both runs commit, both carry a catalog pin,
        and both are quietly wrong.

        The refusal names the run that holds the claim, because a run left RUNNING by a process
        that died holds it exactly as a live one does, and that is deliberate. What that run did
        is unknown, so letting the next run reset the world underneath it would destroy the only
        evidence of it. An operator reads it with `benchmark show` and closes it with
        `benchmark abort`, which is the existing honest close and is what releases the claim.
        See docs/benchmark.md.
        """
        active = await self._runs.active_run_id(merchant_id=merchant_id)
        if active is None:
            return
        raise ConflictError(
            "run_already_active",
            f"benchmark run {active} is already executing against this merchant and must be"
            " completed or aborted before another starts",
            resource=RUN_RESOURCE,
            identifier=str(active),
        )

    def _require_running(self, run: BenchmarkRun) -> None:
        """Refuse to record anything against a run that is not executing.

        A closed run is closed. Its numbers were reported over the denominator it had, and a
        result arriving afterwards would change what a report already said.
        """
        if run.status is BenchmarkRunStatus.RUNNING:
            return
        raise ConflictError(
            "run_not_running",
            f"benchmark run {run.id} is {run.status.value.lower()} and records nothing",
            resource=RUN_RESOURCE,
            identifier=str(run.id),
        )

    async def _mission_run(
        self, run: BenchmarkRun, mission_key: str, *, merchant_id: uuid.UUID
    ) -> BenchmarkMissionRun:
        """One mission's result row within one merchant's run, held until commit."""
        mission = await self._mission(run.suite_id, mission_key)
        result = await self._runs.get_mission_run(run.id, mission.id, merchant_id=merchant_id)
        if result is None:
            raise NotFoundError("benchmark_mission_run", mission_key)
        return result

    async def _mission(self, suite_id: uuid.UUID, mission_key: str) -> BenchmarkMission:
        suite = await self._suites.get_by_id(suite_id)
        if suite is None:
            raise NotFoundError("benchmark_suite", str(suite_id))
        for mission in suite.missions:
            if mission.mission_key == mission_key:
                return mission
        raise NotFoundError("benchmark_mission", mission_key)

    async def _resolve_references(
        self, result: BenchmarkMissionRun, observed: ObservedResult, *, merchant_id: uuid.UUID
    ) -> None:
        """Record the commerce rows this mission actually produced, and only those.

        Every reference is looked up scoped to the merchant rather than copied from the report.
        The composite foreign keys would refuse a row belonging to somebody else anyway; this is
        what turns that refusal into a null and a recorded failure rather than an integrity
        error escaping from inside a run.

        A payment reference is recorded only when the attempt really is SUCCEEDED for this
        merchant. What is deliberately not done here is re-marking the mission when it is not:
        the evaluation stands on the report, and the gap is written down in
        docs/shortcomings.md rather than papered over.
        """
        if observed.selection is not None:
            variant = await self._session.get(Variant, observed.selection.variant_id)
            if variant is not None and variant.merchant_id == merchant_id:
                result.selected_variant_id = variant.id
                result.selected_quantity = observed.selection.quantity

        if observed.checkout is not None and observed.checkout.checkout_id is not None:
            result.checkout_id = await self._owned_checkout(
                observed.checkout.checkout_id, merchant_id=merchant_id
            )

        if observed.payment is not None and observed.payment.attempt_id is not None:
            result.payment_attempt_id = await self._settled_payment(
                observed.payment.attempt_id, merchant_id=merchant_id
            )

    async def _owned_checkout(
        self, checkout_id: uuid.UUID, *, merchant_id: uuid.UUID
    ) -> uuid.UUID | None:
        checkout = await self._session.get(CheckoutSession, checkout_id)
        if checkout is None or checkout.merchant_id != merchant_id:
            return None
        return checkout.id

    async def _settled_payment(
        self, attempt_id: uuid.UUID, *, merchant_id: uuid.UUID
    ) -> uuid.UUID | None:
        attempt = await self._session.get(PaymentAttempt, attempt_id)
        if attempt is None or attempt.merchant_id != merchant_id:
            return None
        if attempt.status is not PaymentAttemptStatus.SUCCEEDED:
            return None
        return attempt.id


def _apply(result: BenchmarkMissionRun, evaluation: MissionEvaluation, *, at: datetime) -> None:
    """Write one evaluation onto its row, without deciding anything.

    Every value here comes from the evaluation. Nothing is recomputed and nothing is adjusted,
    so a row and the pure function that produced it cannot disagree.
    """
    result.status = evaluation.status
    result.primary_failure_reason = evaluation.primary_failure_reason
    result.additional_failure_reasons = [
        reason.value for reason in evaluation.additional_failure_reasons
    ]
    result.unsafe_attempt = evaluation.unsafe_attempt
    result.unverified_attempt = evaluation.unverified_attempt
    result.unsafe_completion = evaluation.unsafe_completion
    result.oracle_confirmed = evaluation.oracle_confirmed
    result.completed_at = at
    # The observed result itself is not stored. Phase 2A records what a mission meant, not the
    # trace of how it got there, and a trace table will reference this row's identifier when
    # one exists.


def _outcome(result: BenchmarkMissionRun) -> MissionOutcome:
    """One stored mission run, as the flat record the metrics read.

    Requires `mission` to have been loaded, which the run read does eagerly, because the
    denominators come from the definition rather than from what happened.
    """
    mission = result.mission
    return MissionOutcome(
        mission_key=mission.mission_key,
        expected_outcome=mission.expected_outcome,
        simulated_value_amount_minor=mission.simulated_value_amount_minor,
        currency=mission.currency,
        status=result.status,
        failure_reasons=result.failure_reasons,
        unsafe_attempt=result.unsafe_attempt,
        unverified_attempt=result.unverified_attempt,
        unsafe_completion=result.unsafe_completion,
        oracle_confirmed=result.oracle_confirmed,
    )


class ReplayExecutor:
    """An executor that replays prepared observed results, keyed by mission.

    It is not a buyer and does not pretend to be one: it carries out no commerce, makes no
    decision and reaches nothing external. What it is for is exercising the run machinery
    without a merchant, and for letting a test state exactly what an executor reported and
    assert exactly what that meant.

    It declares its own identity, so a run driven by it is historically distinguishable from a
    run driven by something that actually shopped. A replayed run whose executor identity said
    `reference-v1` would be a result claiming to have been produced by work nobody did.

    A mission with no prepared result raises rather than being quietly skipped, because a
    silently skipped mission is a run with a smaller denominator than the suite it claims.
    """

    identity = ExecutorIdentity(kind="replay", version=1)

    def __init__(self, results: dict[str, ObservedResult]) -> None:
        self._results = results

    async def __call__(self, brief: AgentMissionBrief, *, merchant_id: uuid.UUID) -> ObservedResult:
        del merchant_id
        if brief.key not in self._results:
            raise KeyError(f"no prepared result for mission {brief.key!r}")
        return self._results[brief.key]


def executor_from(results: dict[str, ObservedResult]) -> ReplayExecutor:
    """A replay executor over these prepared results."""
    return ReplayExecutor(results)


def outcomes_of(mission_runs: Sequence[BenchmarkMissionRun]) -> list[MissionOutcome]:
    """The metric records for a loaded run's mission runs."""
    return [_outcome(result) for result in mission_runs]
