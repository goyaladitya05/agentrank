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

And the whole thing is ordered. Missions run in suite order, one at a time, so a rerun presents
the same workload the same way.
"""

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agentrank_api.benchmark.catalog import CatalogEntry, catalog_content_hash, facts_for
from agentrank_api.benchmark.definitions import AgentMissionBrief
from agentrank_api.benchmark.evaluation import (
    MissionEvaluation,
    evaluate_mission,
    evaluator_version,
)
from agentrank_api.benchmark.execution import ExecutorIdentity, MissionExecutor
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
from agentrank_api.errors import ConflictError, NotFoundError
from agentrank_api.payments.models import PaymentAttempt, PaymentAttemptStatus

RUN_RESOURCE = "benchmark_run"


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

    async def start_run(
        self,
        *,
        suite_key: str,
        suite_version: int,
        merchant_slug: str,
        environment: BenchmarkEnvironment | None = None,
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
        """
        merchant = await self._merchants.get_by_slug(merchant_slug)
        if merchant is None:
            raise NotFoundError("merchant", merchant_slug)
        suite = await self._suites.get(suite_key, suite_version)
        if suite is None:
            raise NotFoundError("benchmark_suite", f"{suite_key}@{suite_version}")

        entries = await self.catalog(merchant.id)
        run = await self._runs.create(
            merchant=merchant,
            suite=suite,
            environment=environment,
            representation_label=representation_label,
            catalog_hash=catalog_content_hash(entries),
            evaluator_version=evaluator_version(),
        )
        run.status = BenchmarkRunStatus.RUNNING
        run.started_at = datetime.now(UTC)
        await self._session.commit()
        return run

    async def run_suite(
        self,
        executor: MissionExecutor,
        *,
        suite_key: str,
        suite_version: int,
        merchant_slug: str,
        representation_label: str | None = None,
    ) -> BenchmarkRun:
        """Start a run, execute every mission in suite order, and complete it.

        The catalog is read once per mission rather than once per run, because a mission can
        change it: buying the last unit of something is exactly the kind of thing this benchmark
        is for, and marking a later mission against stock an earlier one consumed would be
        marking it against a catalog that no longer exists. The pin on the run is still taken at
        the start, and it describes where the run began.
        """
        run = await self.start_run(
            suite_key=suite_key,
            suite_version=suite_version,
            merchant_slug=merchant_slug,
            representation_label=representation_label,
        )
        suite = await self._suites.get_by_id(run.suite_id)
        assert suite is not None  # start_run resolved it a moment ago

        for mission in suite.missions:
            brief = mission.to_brief()
            observed = await executor(brief, merchant_id=run.merchant_id)
            await self.record_result(run.id, brief.key, observed, merchant_id=run.merchant_id)

        return await self.complete_run(run.id, merchant_id=run.merchant_id)

    async def record_result(
        self,
        run_id: uuid.UUID,
        mission_key: str,
        observed: ObservedResult,
        *,
        merchant_id: uuid.UUID,
    ) -> BenchmarkMissionRun:
        """Mark one mission and write the result, in one transaction.

        The mission run is locked before anything is decided, so two executors reporting the
        same mission queue rather than both reading PENDING. The database refuses the second
        write either way; the lock is what turns that into an ordinary refusal.
        """
        run = await self._locked_run(run_id, merchant_id=merchant_id)
        if run.status is not BenchmarkRunStatus.RUNNING:
            raise ConflictError(
                "run_not_running",
                f"benchmark run {run.id} is {run.status.value.lower()} and records nothing",
                resource=RUN_RESOURCE,
                identifier=str(run.id),
            )

        mission = await self._mission(run.suite_id, mission_key)
        result = await self._runs.get_mission_run(run.id, mission.id, merchant_id=merchant_id)
        if result is None:
            raise NotFoundError("benchmark_mission_run", mission_key)
        if result.status is not MissionRunStatus.PENDING:
            raise ConflictError(
                "mission_already_recorded",
                f"mission {mission_key} in run {run.id} has already been executed",
                resource=RUN_RESOURCE,
                identifier=str(run.id),
            )

        entries = await self.catalog(merchant_id)
        evaluation = evaluate_mission(
            mission.to_definition(),
            observed,
            merchant_id=merchant_id,
            catalog=facts_for(mission.to_brief(), entries, observed.selection),
        )

        # RUNNING first, because the lifecycle is a transition whitelist and an outcome is
        # produced by a transition rather than written. Both statements are in one transaction,
        # so nothing observes the intermediate state.
        now = datetime.now(UTC)
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
        """
        run = await self._loaded_run(run_id, merchant_id=merchant_id)
        unfinished = [
            result.id for result in run.mission_runs if result.status is MissionRunStatus.PENDING
        ]
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
