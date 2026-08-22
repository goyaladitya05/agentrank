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
from agentrank_api.benchmark.evidence import CommerceEvidence
from agentrank_api.benchmark.execution import ExecutorIdentity, MissionExecutor
from agentrank_api.benchmark.faults import ExecutionFault, FaultOrigin
from agentrank_api.benchmark.fixtures import BenchmarkFixture
from agentrank_api.benchmark.lifecycle import BenchmarkRunStatus, MissionRunStatus
from agentrank_api.benchmark.metrics import BenchmarkMetrics, MissionOutcome, compute_metrics
from agentrank_api.benchmark.models import (
    BenchmarkEnvironment,
    BenchmarkMission,
    BenchmarkMissionRun,
    BenchmarkRun,
)
from agentrank_api.benchmark.mutation import BenchmarkRunCapability
from agentrank_api.benchmark.observation import ObservedResult
from agentrank_api.benchmark.report import ExecutorReport
from agentrank_api.benchmark.repository import BenchmarkRunRepository, BenchmarkSuiteRepository
from agentrank_api.benchmark.substantiation import CommerceSubstantiation
from agentrank_api.benchmark.tools import ExecutionWitness
from agentrank_api.checkout.models import CheckoutSession
from agentrank_api.commerce.models import Product, Variant
from agentrank_api.commerce.repository import MerchantRepository
from agentrank_api.conflicts import translated_conflicts
from agentrank_api.errors import ConflictError, NotFoundError
from agentrank_api.payments.models import (
    TERMINAL_STATUSES,
    PaymentAttempt,
    PaymentAttemptStatus,
)
from agentrank_api.payments.repository import PaymentAttemptRepository

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
        self._attempts = PaymentAttemptRepository(session)
        self._substantiation = CommerceSubstantiation(session)

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

        Refused while another run is already executing against this merchant. The partial unique
        index underneath is what makes that true across processes, and the transition to RUNNING
        is what enters it.

        The advisory lock in front of the read is what makes the answer the same every time. Two
        starters racing on the index alone get whatever the index gives them, which is an
        integrity error when they wait and a cancelled statement when a `lock_timeout` is set,
        and neither is a refusal naming the run an operator has to close. Holding the lock first
        means the loser reads the winner's committed run and is refused by name.
        """
        merchant = await self._merchants.get_by_slug(merchant_slug)
        if merchant is None:
            raise NotFoundError("merchant", merchant_slug)
        suite = await self._suites.get(suite_key, suite_version)
        if suite is None:
            raise NotFoundError("benchmark_suite", f"{suite_key}@{suite_version}")

        # The advisory lock before the read, so a second starter waits here rather than racing
        # the index. The same lock preparation takes, keyed on the merchant slug, released by
        # this transaction's commit.
        await self._environments.claim(merchant.slug)
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
        witness: ExecutionWitness | None = None,
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

        An untrusted executor that raises without dispatching payment is normalized to a trusted
        AGENT fault and recorded. A witness is required for that normalization: without a
        boundary record the trusted in-process reference path still propagates, because it has no
        evidence that no payment was attempted. If a witness saw a payment request, trusted
        substantiation decides whether it reached a terminal state; an unresolved one stays
        RUNNING and stops the run rather than risking a duplicate charge.

        The world is claimed for the whole run. The first preparation happens before the run
        exists and is refused if another run already owns this merchant, so a stale RUNNING run
        stops the world being reset before anything of this one is written down. Every later
        preparation names this run and is therefore allowed, and is refused for everybody else.

        `witness` is trusted evidence about how each mission's execution actually went, gathered
        on this side of whatever boundary the executor is behind. It is what decides whether an
        interruption was the merchant's or the harness's, and it is optional because a replayed
        run has no boundary to gather evidence at. None means no interruption is attributed at
        all, which is honest: with nothing watching, there is nothing to attribute from, and the
        alternative is believing the executor.
        """
        run = await self.start_suite(
            suite_key=suite_key,
            suite_version=suite_version,
            fixture=fixture,
            executor=executor.identity,
            representation_label=representation_label,
        )
        return await self.execute_started_suite(
            run.id,
            executor,
            merchant_id=run.merchant_id,
            fixture=fixture,
            witness=witness,
        )

    async def start_suite(
        self,
        *,
        suite_key: str,
        suite_version: int,
        fixture: BenchmarkFixture,
        executor: ExecutorIdentity,
        representation_label: str | None = None,
    ) -> BenchmarkRun:
        """Prepare a benchmark world and persist its RUNNING claim before a buyer is provisioned.

        The split from ``run_suite`` lets the loopback path mint its short lived merchant
        credential only after a specific run owns the world.  That credential is then bound to
        this run in the database, rather than being an ordinary key that happens to be used
        while a benchmark is active.
        """
        environment = await self._environments.prepare(fixture)
        return await self.start_run(
            suite_key=suite_key,
            suite_version=suite_version,
            merchant_slug=fixture.merchant_slug,
            environment=environment.environment,
            executor=executor,
            representation_label=representation_label,
        )

    async def execute_started_suite(
        self,
        run_id: uuid.UUID,
        executor: MissionExecutor,
        *,
        merchant_id: uuid.UUID,
        fixture: BenchmarkFixture,
        witness: ExecutionWitness | None = None,
    ) -> BenchmarkRun:
        """Execute the still-running suite whose world was prepared by ``start_suite``.

        This is deliberately not a general resume operation.  It is called immediately by the
        same trusted orchestration that started the run, and it only accepts a RUNNING row whose
        registered environment is the fixture supplied.  A process crash still leaves the run
        RUNNING and requires the existing explicit abort and recovery path.
        """
        run = await self._loaded_run(run_id, merchant_id=merchant_id)
        self._require_running(run)
        environment = await self._environments.require_registered(fixture)
        if run.environment_id != environment.id or run.merchant_id != environment.merchant_id:
            raise ConflictError(
                "run_environment_mismatch",
                f"benchmark run {run.id} was not started for fixture {fixture.label}",
                resource=RUN_RESOURCE,
                identifier=str(run.id),
            )
        suite = await self._suites.get_by_id(run.suite_id)
        assert suite is not None  # start_run resolved it a moment ago
        merchant_id = run.merchant_id
        _bind_benchmark_run(executor, BenchmarkRunCapability(merchant_id, run.id))

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
            if witness is not None:
                witness.begin()
            since = await self._attempts.clock()
            # The read above opened a transaction that would otherwise stay open for as long as
            # the executor takes, which for a model is a long time to hold one for nothing.
            await self._session.commit()
            fault: ExecutionFault | None = None
            try:
                report = await executor(brief, merchant_id=merchant_id)
            except Exception as failed:
                if witness is None:
                    raise
                fault = witness.fault()
                if fault is None:
                    fault = ExecutionFault(
                        origin=FaultOrigin.AGENT,
                        detail=f"the executor raised {type(failed).__name__}",
                    )
                report = ExecutorReport(merchant_id=merchant_id)
            if fault is None and witness is not None:
                fault = witness.fault()
            observed = await self._substantiate(
                report,
                brief=brief,
                merchant_id=merchant_id,
                catalog=entries,
                evidence=None if witness is None else witness.evidence(),
                since=since,
            )
            _require_payment_accounted(brief.key, observed, fault, witness)
            result = await self._record(
                run_id,
                brief.key,
                observed,
                merchant_id=merchant_id,
                catalog=entries,
                fault=fault,
            )
            log.info(
                "benchmark mission recorded",
                extra={
                    "benchmark_run_id": str(run_id),
                    "mission_run_id": str(result.id),
                    "mission_key": brief.key,
                },
            )

        finished = await self.complete_run(run.id, merchant_id=merchant_id)
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
        report: ExecutorReport,
        *,
        merchant_id: uuid.UUID,
        catalog: Sequence[CatalogEntry] | None = None,
        fault: ExecutionFault | None = None,
        evidence: CommerceEvidence | None = None,
        since: datetime | None = None,
    ) -> BenchmarkMissionRun:
        """Substantiate one executor's report against trusted state, then mark and write it.

        This takes a report and not an observation, and that is the shape of the whole boundary
        rather than a signature preference. There is no way through this service to record a
        mission from facts somebody else assembled, so a caller cannot hand in a quoted total, a
        payment status or an authorization decision that nothing established.

        `since` bounds the payment sweep to this mission. `run_suite` reads the database's own
        clock immediately before the executor runs and passes it, which is the honest bound. The
        fallback used here is the mission run's own `started_at`, which is this application's
        clock rather than the database's and is the weaker of the two.

        `evidence` is what the trusted tool boundary saw the merchant answer. None means nobody
        was watching, which is reported as no authorization rather than as an allowed one.
        """
        run = await self._loaded_run(run_id, merchant_id=merchant_id)
        mission = await self._mission(run.suite_id, mission_key)
        started = await self._runs.get_mission_run(run.id, mission.id, merchant_id=merchant_id)
        entries = await self.catalog(merchant_id) if catalog is None else catalog
        observed = await self._substantiate(
            report,
            brief=mission.to_brief(),
            merchant_id=merchant_id,
            catalog=entries,
            evidence=evidence,
            since=since if since is not None else _started_at(started),
        )
        return await self._record(
            run_id,
            mission_key,
            observed,
            merchant_id=merchant_id,
            catalog=entries,
            fault=fault,
        )

    async def _substantiate(
        self,
        report: ExecutorReport,
        *,
        brief: AgentMissionBrief,
        merchant_id: uuid.UUID,
        catalog: Sequence[CatalogEntry],
        evidence: CommerceEvidence | None,
        since: datetime | None,
    ) -> ObservedResult:
        """What trusted state says happened, from what the executor said it did."""
        return await self._substantiation.observe(
            report,
            merchant_id=merchant_id,
            brief=brief,
            catalog=catalog,
            evidence=evidence,
            since=since,
        )

    async def _record(
        self,
        run_id: uuid.UUID,
        mission_key: str,
        observed: ObservedResult,
        *,
        merchant_id: uuid.UUID,
        catalog: Sequence[CatalogEntry] | None = None,
        fault: ExecutionFault | None = None,
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

        `fault` is what trusted code observed at the tool boundary, and it is passed rather than
        read out of the report for the reason the whole of Phase 2B-R exists: an executor that
        could put its own mission into ERRORED would be marking the one status that carries no
        failure reason and counts the mission's value as not measured.
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
            fault=fault,
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
        await self._locked_run(run_id, merchant_id=merchant_id)
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
        await self._locked_run(run_id, merchant_id=merchant_id)
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
        """Close a run, having read its status under a row lock.

        The lock is taken by the callers and it is not decoration. Two closes racing on an
        unlocked read both saw a run that was not terminal, and the second one's UPDATE was
        refused by the lifecycle trigger with a raw database error rather than by the guard
        below with a refusal a caller can act on. That is not far fetched: an operator running
        `benchmark abort` on a run that looks stuck, at the moment `run_suite` reaches
        `complete_run`, is exactly it.
        """
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


def _started_at(result: BenchmarkMissionRun | None) -> datetime | None:
    """When a mission run says it began, or None when there is no row to ask."""
    return None if result is None else result.started_at


def _require_payment_accounted(
    mission_key: str,
    observed: ObservedResult,
    fault: ExecutionFault | None,
    witness: ExecutionWitness | None,
) -> None:
    """Refuse to record a mission that dispatched a payment and cannot say what became of it.

    Three facts have to hold together for this to fire: something failed, the payment call was
    actually made, and no settled payment can be found. All three come from trusted state now.
    The payment is substantiated from the payment table rather than read out of the report, so a
    mission that dispatched a payment which resolved is recorded with the outcome it reached, and
    one whose payment is still ADMITTED, IN_FLIGHT or UNKNOWN stops the run exactly as one with
    no payment at all does.

    Recording an unresolved payment would be worse than stopping. ERRORED says the harness could
    not carry the mission out and moves the mission's value out of lost demand, which is the
    wrong thing to say about a mission that may have bought something, and carrying on would let
    the next preparation try to release a hold under a payment nobody has resolved.

    So the mission stays RUNNING, which is the one state that means "this started and what it
    did is unknown", and the run stops. That state is never replayed, which is the rule that
    keeps a benchmark from buying the same thing twice. An operator closes the run with
    `benchmark abort`, which releases the world claim, and resolves the payment through
    `agentrank_api.cli payments`.
    """
    if witness is None or fault is None:
        return
    if not witness.payment_attempted():
        return
    if observed.authorization is not None and not observed.authorization.allowed:
        # The trusted payment response established that the authorization layer refused before a
        # provider request could exist. A worker dying after that answer is an unsafe attempt or
        # an agent failure, not an unknown payment.
        return
    if observed.payment is not None and observed.payment.status in TERMINAL_STATUSES:
        return
    reached = "none" if observed.payment is None else observed.payment.status.value
    raise ConflictError(
        "payment_unaccounted",
        f"mission {mission_key} dispatched a payment that reached {reached}, after"
        f" {fault.detail}. Resolve the payment before closing this run",
        resource=RUN_RESOURCE,
        identifier=mission_key,
    )


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
    """An executor that replays prepared reports, keyed by mission.

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

    def __init__(self, results: dict[str, ExecutorReport]) -> None:
        self._results = results

    async def __call__(self, brief: AgentMissionBrief, *, merchant_id: uuid.UUID) -> ExecutorReport:
        del merchant_id
        if brief.key not in self._results:
            raise KeyError(f"no prepared result for mission {brief.key!r}")
        return self._results[brief.key]


def executor_from(results: dict[str, ExecutorReport]) -> ReplayExecutor:
    """A replay executor over these prepared reports.

    What it replays is a report and not an observation, which is the property worth stating: a
    replayed run goes through the same substantiation a real one does, so a prepared report
    claiming a payment nothing produced is marked exactly as an executor claiming one would be.
    """
    return ReplayExecutor(results)


def _bind_benchmark_run(executor: MissionExecutor, capability: BenchmarkRunCapability) -> None:
    """Hand a trusted in-process buyer its persisted mutation capability, when it has one.

    The optional method is intentionally absent from ``MissionExecutor``.  An executor only
    needs the mission brief protocol; the trusted reference surface is the sole implementation
    that opens application services directly and therefore the sole one that needs this binding.
    An isolated worker receives the same authority through its database-bound merchant
    credential instead.
    """
    binder = getattr(executor, "bind_benchmark_run", None)
    if binder is not None:
        binder(capability)


def outcomes_of(mission_runs: Sequence[BenchmarkMissionRun]) -> list[MissionOutcome]:
    """The metric records for a loaded run's mission runs."""
    return [_outcome(result) for result in mission_runs]
