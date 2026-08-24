"""Admitting and settling a merchant's request to measure a published representation again.

The product loop this closes is: diagnostics show a problem, the merchant reviews compiler
facts, the merchant publishes a new agent-ready representation, and then the merchant asks for
the benchmark to be run again. The last step is a separate command on purpose. Publishing
writes an artifact and spends nothing; launching spends model quota and takes as long as a
suite takes, and a workflow that started one as a side effect of the other would be spending on
the merchant's behalf without being asked.

Two halves live here and they run in different processes.

The merchant half is `MerchantReevaluationService`. It answers what a launch would evaluate,
and it admits one. Admission resolves every methodology-critical identity server side from the
merchant its credential authenticated, freezes it on the row, and commits before answering.
Nothing is executed: the answer is a queued launch, which is exactly what an honest response
can promise when the work has not started.

The worker half is `ReevaluationWorkerService`. It claims a queued launch, binds the benchmark
run it produced, and settles the launch when the run finishes. It never reads a browser session
and never takes a merchant identity from anything but the persisted row, because by the time it
runs there is no request and no session left to trust.

What the browser may say is a representation identifier and a request key. Everything else,
including which merchant this is, comes from the credential and from the database.
"""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from agentrank_api.benchmark.environment import BenchmarkEnvironmentService
from agentrank_api.benchmark.execution import REFERENCE_ISOLATED_KIND
from agentrank_api.benchmark.lifecycle import TERMINAL_MISSION_STATUSES, BenchmarkRunStatus
from agentrank_api.benchmark.llm import (
    GEMINI_PROVIDER,
    OPENAI_PROVIDER,
    AgentConfiguration,
    executor_kind,
)
from agentrank_api.benchmark.models import (
    BenchmarkEnvironment,
    BenchmarkMission,
    BenchmarkMissionRun,
    BenchmarkRun,
    BenchmarkSuite,
)
from agentrank_api.benchmark.reevaluation import (
    BenchmarkReevaluation,
    BuyerProfile,
    ReevaluationStatus,
)
from agentrank_api.benchmark.repository import BenchmarkRunRepository, BenchmarkSuiteRepository
from agentrank_api.commerce.models import Merchant
from agentrank_api.compiler.models import CompilerRun
from agentrank_api.config import Settings
from agentrank_api.conflicts import translated_conflicts
from agentrank_api.errors import ConflictError, NotFoundError
from agentrank_api.representation.definitions import RepresentationProducer
from agentrank_api.representation.models import CommerceRepresentation

RESOURCE = "benchmark_reevaluation"

# The exact models a launch requests when a provider credential is configured. Constants rather
# than settings, because a run records the model it requested and a value nobody can see in the
# repository is a value nobody can check a historical run against. OpenAI first when both are
# configured, so the choice is stated rather than incidental.
OPENAI_MODEL = "gpt-5.6-terra"
GEMINI_MODEL = "gemini-3.7-flash"

# Which executor kind each profile records on the run it produces. The model buyer's kind is
# derived from its provider the same way the operator command line derives it, so a run launched
# from the console and a run launched from a shell are the same measurement.
REFERENCE_EXECUTOR_KIND = REFERENCE_ISOLATED_KIND


@dataclass(frozen=True, slots=True)
class BuyerPlan:
    """Which buyer a launch would use, resolved from configuration and frozen at admission.

    `configuration` is None for the reference buyer and that is not a gap. The reference buyer
    has no provider, no model, no prompt and no sampling policy, so there is nothing to freeze,
    and a column holding an invented one would be a configuration nothing reads.
    """

    profile: BuyerProfile
    executor_kind: str
    configuration: AgentConfiguration | None

    @property
    def provider(self) -> str | None:
        return None if self.configuration is None else self.configuration.provider

    @property
    def requested_model(self) -> str | None:
        return None if self.configuration is None else self.configuration.requested_model


def resolve_buyer(settings: Settings) -> BuyerPlan:
    """The buyer this deployment can actually run, named honestly.

    A merchant re-evaluation is only evidence about an agent-ready representation when an agent
    reads it, and only the model buyer is given the representation as its discovery surface.
    With no provider credential configured there is no model buyer, so the launch falls back to
    the deterministic reference buyer and every surface says so in those words: it is not an AI
    agent, it reads structured commerce fields a storefront does not publish, and the run it
    produces pins no representation because it never saw one.
    """
    if settings.openai is not None:
        configuration = AgentConfiguration(provider=OPENAI_PROVIDER, requested_model=OPENAI_MODEL)
        return BuyerPlan(BuyerProfile.AI_BUYER, executor_kind(configuration), configuration)
    if settings.gemini is not None:
        configuration = AgentConfiguration(provider=GEMINI_PROVIDER, requested_model=GEMINI_MODEL)
        return BuyerPlan(BuyerProfile.AI_BUYER, executor_kind(configuration), configuration)
    return BuyerPlan(BuyerProfile.REFERENCE_BUYER, REFERENCE_EXECUTOR_KIND, None)


@dataclass(frozen=True, slots=True)
class LaunchBlocker:
    """One reason a re-evaluation cannot be launched right now, with a merchant sentence."""

    code: str
    message: str


@dataclass(frozen=True, slots=True)
class ReevaluationPlan:
    """Everything a merchant should know before spending a benchmark run.

    Deliberately without a currency figure. AgentRank has no trustworthy provider pricing data,
    and a number invented from one would be the most confident thing on the page. What is here
    instead is what will actually be executed and the bounds it is executed under, which is
    checkable.
    """

    representation_id: uuid.UUID | None
    representation_label: str | None
    compiler_run_id: uuid.UUID | None
    source_snapshot_id: uuid.UUID | None
    suite_id: uuid.UUID | None
    suite_label: str | None
    suite_definition_hash: str | None
    mission_count: int | None
    environment_id: uuid.UUID | None
    environment_label: str | None
    buyer_profile: BuyerProfile
    executor_kind: str
    provider: str | None
    requested_model: str | None
    max_model_turns: int | None
    max_tool_calls: int | None
    mission_deadline_seconds: float | None
    baseline_run_id: uuid.UUID | None
    baseline_run_completed_at: datetime | None
    blockers: tuple[LaunchBlocker, ...]
    pending_reevaluation_id: uuid.UUID | None

    @property
    def launchable(self) -> bool:
        return not self.blockers


@dataclass(frozen=True, slots=True)
class _Labels:
    """Everything a page of launches needs, read once rather than once per launch."""

    suite_labels: dict[uuid.UUID, str]
    mission_counts: dict[uuid.UUID, int]
    environment_labels: dict[uuid.UUID, str]
    representation_labels: dict[uuid.UUID, str]
    run_statuses: dict[uuid.UUID, str]
    missions_finished: dict[uuid.UUID, int]


@dataclass(frozen=True, slots=True)
class ReevaluationDetail:
    """One launch as a merchant reads it: what was frozen, and where execution has got to.

    Progress is counts of missions this run has actually finished, never a percentage and never
    an estimate of time remaining. AgentRank knows how many missions a suite has and how many of
    them reached a terminal state, and a bar moving on a timer would be inventing the rest.
    """

    reevaluation_id: uuid.UUID
    status: ReevaluationStatus
    failure_code: str | None
    requested_at: datetime
    started_at: datetime | None
    settled_at: datetime | None
    representation_id: uuid.UUID
    representation_label: str
    compiler_run_id: uuid.UUID
    suite_id: uuid.UUID
    suite_label: str
    mission_count: int
    environment_label: str
    buyer_profile: BuyerProfile
    executor_kind: str
    provider: str | None
    requested_model: str | None
    buyer_configuration_digest: str | None
    run_id: uuid.UUID | None
    run_status: str | None
    missions_completed: int | None
    baseline_run_id: uuid.UUID | None


@dataclass(frozen=True, slots=True)
class _Resolved:
    """The identities a launch freezes, once every one of them exists."""

    merchant: Merchant
    representation: CommerceRepresentation
    compiler_run: CompilerRun
    suite: BenchmarkSuite
    environment: BenchmarkEnvironment
    baseline: BenchmarkRun | None


class MerchantReevaluationService:
    """The merchant-facing half: what a launch would do, and admitting one."""

    def __init__(self, session: AsyncSession, settings: Settings) -> None:
        self._session = session
        self._settings = settings
        self._suites = BenchmarkSuiteRepository(session)
        self._runs = BenchmarkRunRepository(session)
        self._environments = BenchmarkEnvironmentService(session)

    async def plan(self, merchant_id: uuid.UUID) -> ReevaluationPlan:
        """What a launch would evaluate now, and what stops it if anything does."""
        buyer = resolve_buyer(self._settings)
        blockers: list[LaunchBlocker] = []

        representation = await self._current_representation(merchant_id)
        compiler_run = None
        if representation is None:
            blockers.append(
                LaunchBlocker(
                    "no_published_representation",
                    "Publish an agent-ready representation before requesting a re-evaluation.",
                )
            )
        else:
            compiler_run = await self._compiler_run(merchant_id, representation)

        suite = await self._current_suite(merchant_id)
        if suite is None:
            blockers.append(
                LaunchBlocker(
                    "benchmark_suite_unavailable",
                    "No benchmark suite is published for this merchant, so there is nothing to"
                    " run. Your operator publishes one from the benchmark command line.",
                )
            )

        environment = await self._current_environment(merchant_id)
        if environment is None:
            blockers.append(
                LaunchBlocker(
                    "benchmark_world_unregistered",
                    "This merchant has no registered benchmark world, so a run has no catalog to"
                    " be put back to. Your operator registers one from the benchmark command"
                    " line.",
                )
            )

        pending = await self._pending(merchant_id)
        if pending is not None:
            blockers.append(
                LaunchBlocker(
                    "reevaluation_already_pending",
                    "A re-evaluation is already queued or running for this merchant. Wait for it"
                    " to finish before starting another.",
                )
            )
        active = await self._runs.active_run_id(merchant_id=merchant_id)
        if active is not None and (pending is None or pending.run_id != active):
            blockers.append(
                LaunchBlocker(
                    "run_already_active",
                    "A benchmark run is already executing against this merchant's world. Only one"
                    " run may own that world at a time.",
                )
            )

        baseline = None if suite is None else await self._baseline(merchant_id, suite.id)
        configuration = buyer.configuration
        return ReevaluationPlan(
            representation_id=None if representation is None else representation.id,
            representation_label=None if representation is None else representation.label,
            compiler_run_id=None if compiler_run is None else compiler_run.id,
            source_snapshot_id=(
                None if representation is None else representation.source_snapshot_id
            ),
            suite_id=None if suite is None else suite.id,
            suite_label=None if suite is None else suite.label,
            suite_definition_hash=None if suite is None else suite.definition_hash,
            mission_count=None if suite is None else len(suite.missions),
            environment_id=None if environment is None else environment.id,
            environment_label=None if environment is None else environment.label,
            buyer_profile=buyer.profile,
            executor_kind=buyer.executor_kind,
            provider=buyer.provider,
            requested_model=buyer.requested_model,
            max_model_turns=None if configuration is None else configuration.max_model_turns,
            max_tool_calls=None if configuration is None else configuration.max_tool_calls,
            mission_deadline_seconds=(
                None if configuration is None else configuration.deadline_seconds
            ),
            baseline_run_id=None if baseline is None else baseline.id,
            baseline_run_completed_at=None if baseline is None else baseline.completed_at,
            blockers=tuple(blockers),
            pending_reevaluation_id=None if pending is None else pending.id,
        )

    async def request(
        self, merchant_id: uuid.UUID, *, representation_id: uuid.UUID, request_key: str
    ) -> BenchmarkReevaluation:
        """Admit one launch, or answer with the one this request key already produced.

        The advisory lock on this merchant's benchmark world is taken first, which is what makes
        two simultaneous launches produce one deterministic refusal rather than whatever the
        unique index happens to give them. The index is still what makes the invariant true
        across processes; this makes the answer the same every time.

        Nothing is executed here and nothing external is called, so this whole method is one
        short transaction. What comes back is a queued launch, which is the only thing an honest
        answer can promise while the work has not started.
        """
        merchant = await self._merchant(merchant_id)
        existing = await self._by_request_key(merchant_id, request_key)
        if existing is not None:
            return self._same_request(existing, representation_id)

        await self._environments.claim(merchant.slug)
        # Read again under the lock: a concurrent identical submit may have committed between
        # the read above and the lock, and answering with its launch is the whole point of a
        # request key.
        existing = await self._by_request_key(merchant_id, request_key)
        if existing is not None:
            return self._same_request(existing, representation_id)

        resolved = await self._require_launchable(merchant, representation_id)
        buyer = resolve_buyer(self._settings)
        launch = BenchmarkReevaluation(
            merchant_id=merchant.id,
            request_key=request_key,
            representation_id=resolved.representation.id,
            compiler_run_id=resolved.compiler_run.id,
            suite_id=resolved.suite.id,
            environment_id=resolved.environment.id,
            buyer_profile=buyer.profile,
            buyer_configuration=(
                None if buyer.configuration is None else buyer.configuration.payload()
            ),
            buyer_configuration_digest=(
                None if buyer.configuration is None else buyer.configuration.configuration_digest
            ),
            executor_kind=buyer.executor_kind,
            status=ReevaluationStatus.QUEUED,
            baseline_run_id=None if resolved.baseline is None else resolved.baseline.id,
        )
        self._session.add(launch)
        try:
            async with translated_conflicts(self._session, identifier=str(merchant.id)):
                await self._session.commit()
        except IntegrityError:
            # The advisory lock makes this unreachable between two callers holding it, and the
            # index is what would refuse a caller that somehow did not. Re-reading the key is
            # the deterministic answer either way.
            await self._session.rollback()
            duplicate = await self._by_request_key(merchant_id, request_key)
            if duplicate is not None:
                return self._same_request(duplicate, representation_id)
            raise
        return launch

    async def detail(
        self, merchant_id: uuid.UUID, reevaluation_id: uuid.UUID
    ) -> ReevaluationDetail:
        """One launch with its frozen identity resolved to labels and its honest progress."""
        launch = await self.get(merchant_id, reevaluation_id)
        return self._detail(launch, await self._resolve([launch]))

    async def details(self, merchant_id: uuid.UUID, *, limit: int = 10) -> list[ReevaluationDetail]:
        launches = await self.recent(merchant_id, limit=limit)
        resolved = await self._resolve(launches)
        return [self._detail(launch, resolved) for launch in launches]

    async def _resolve(self, launches: Sequence[BenchmarkReevaluation]) -> _Labels:
        """Everything a page of launches needs to be described, in a fixed number of statements.

        Batched rather than per launch. The launch list is a merchant's history and the launch
        page re-reads itself while a run is executing, so a read that grew a handful of
        statements per row would be the console's most wasteful query pattern by a wide margin.

        Mission counts are aggregates rather than loaded rows. Neither the suite's shape nor a
        run's progress needs the rows themselves, and loading fourteen missions and fourteen
        results to produce two integers is the same defect one level down.
        """
        if not launches:
            return _Labels({}, {}, {}, {}, {}, {})
        merchant_ids = {launch.merchant_id for launch in launches}
        suite_ids = {launch.suite_id for launch in launches}
        environment_ids = {launch.environment_id for launch in launches}
        representation_ids = {launch.representation_id for launch in launches}
        run_ids = {launch.run_id for launch in launches if launch.run_id is not None}

        suites = await self._session.execute(
            select(BenchmarkSuite.id, BenchmarkSuite.suite_key, BenchmarkSuite.version).where(
                BenchmarkSuite.id.in_(suite_ids)
            )
        )
        missions = await self._session.execute(
            select(BenchmarkMission.suite_id, func.count())
            .where(BenchmarkMission.suite_id.in_(suite_ids))
            .group_by(BenchmarkMission.suite_id)
        )
        environments = await self._session.execute(
            select(BenchmarkEnvironment).where(
                BenchmarkEnvironment.id.in_(environment_ids),
                BenchmarkEnvironment.merchant_id.in_(merchant_ids),
            )
        )
        representations = await self._session.execute(
            select(CommerceRepresentation).where(
                CommerceRepresentation.id.in_(representation_ids),
                CommerceRepresentation.merchant_id.in_(merchant_ids),
            )
        )
        run_statuses: dict[uuid.UUID, str] = {}
        finished: dict[uuid.UUID, int] = {}
        if run_ids:
            runs = await self._session.execute(
                select(BenchmarkRun.id, BenchmarkRun.status).where(
                    BenchmarkRun.id.in_(run_ids), BenchmarkRun.merchant_id.in_(merchant_ids)
                )
            )
            run_statuses = {row.id: row.status.value for row in runs.all()}
            terminal = await self._session.execute(
                select(BenchmarkMissionRun.run_id, func.count())
                .where(
                    BenchmarkMissionRun.run_id.in_(run_ids),
                    BenchmarkMissionRun.merchant_id.in_(merchant_ids),
                    BenchmarkMissionRun.status.in_(sorted(TERMINAL_MISSION_STATUSES)),
                )
                .group_by(BenchmarkMissionRun.run_id)
            )
            finished = {row[0]: row[1] for row in terminal.all()}
        return _Labels(
            suite_labels={row.id: f"{row.suite_key}@{row.version}" for row in suites.all()},
            mission_counts={row[0]: row[1] for row in missions.all()},
            environment_labels={row.id: row.label for row in environments.scalars()},
            representation_labels={row.id: row.label for row in representations.scalars()},
            run_statuses=run_statuses,
            missions_finished=finished,
        )

    def _detail(self, launch: BenchmarkReevaluation, labels: _Labels) -> ReevaluationDetail:
        configuration = (
            None
            if launch.buyer_configuration is None
            else AgentConfiguration.from_payload(launch.buyer_configuration)
        )
        return ReevaluationDetail(
            reevaluation_id=launch.id,
            status=launch.status,
            failure_code=launch.failure_code,
            requested_at=launch.requested_at,
            started_at=launch.started_at,
            settled_at=launch.settled_at,
            representation_id=launch.representation_id,
            representation_label=labels.representation_labels.get(launch.representation_id, ""),
            compiler_run_id=launch.compiler_run_id,
            suite_id=launch.suite_id,
            suite_label=labels.suite_labels.get(launch.suite_id, ""),
            mission_count=labels.mission_counts.get(launch.suite_id, 0),
            environment_label=labels.environment_labels.get(launch.environment_id, ""),
            buyer_profile=launch.buyer_profile,
            executor_kind=launch.executor_kind,
            provider=None if configuration is None else configuration.provider,
            requested_model=None if configuration is None else configuration.requested_model,
            buyer_configuration_digest=launch.buyer_configuration_digest,
            run_id=launch.run_id,
            run_status=(None if launch.run_id is None else labels.run_statuses.get(launch.run_id)),
            # Zero rather than None once a run exists: a run always has as many mission runs as
            # its suite has missions, so none of them being terminal yet is a real count.
            missions_completed=(
                None if launch.run_id is None else labels.missions_finished.get(launch.run_id, 0)
            ),
            baseline_run_id=launch.baseline_run_id,
        )

    async def get(
        self, merchant_id: uuid.UUID, reevaluation_id: uuid.UUID
    ) -> BenchmarkReevaluation:
        launch = (
            await self._session.execute(
                select(BenchmarkReevaluation).where(
                    BenchmarkReevaluation.id == reevaluation_id,
                    BenchmarkReevaluation.merchant_id == merchant_id,
                )
            )
        ).scalar_one_or_none()
        if launch is None:
            raise NotFoundError(RESOURCE, str(reevaluation_id))
        return launch

    async def recent(
        self, merchant_id: uuid.UUID, *, limit: int = 10
    ) -> list[BenchmarkReevaluation]:
        rows = await self._session.execute(
            select(BenchmarkReevaluation)
            .where(BenchmarkReevaluation.merchant_id == merchant_id)
            .order_by(BenchmarkReevaluation.requested_at.desc(), BenchmarkReevaluation.id.desc())
            .limit(limit)
        )
        return list(rows.scalars())

    def _same_request(
        self, existing: BenchmarkReevaluation, representation_id: uuid.UUID
    ) -> BenchmarkReevaluation:
        """The launch this request key already produced, or a refusal that it asked for another.

        A retry after a lost response repeats the key and the representation and gets its own
        launch back. A different representation under a key that has already been used is a
        different command wearing an old name, and answering with the old launch would tell a
        merchant something was queued that was not.
        """
        if existing.representation_id != representation_id:
            raise ConflictError(
                "reevaluation_request_key_reused",
                "This request has already launched a re-evaluation of a different representation.",
                resource=RESOURCE,
                identifier=str(existing.id),
            )
        return existing

    async def _require_launchable(
        self, merchant: Merchant, representation_id: uuid.UUID
    ) -> _Resolved:
        """Resolve every frozen identity, refusing by name when one is missing or stale."""
        current = await self._current_representation(merchant.id)
        if current is None:
            raise ConflictError(
                "no_published_representation",
                "This merchant has no published agent-ready representation to evaluate.",
                resource=RESOURCE,
                identifier=str(merchant.id),
            )
        if current.id != representation_id:
            # Either somebody published a newer representation while this page was open, or the
            # browser named an older one. Both are the same refusal: a re-evaluation measures
            # what is published now, and running an artifact the merchant is no longer publishing
            # would produce evidence about nothing they can act on.
            raise ConflictError(
                "representation_superseded",
                "A newer agent-ready representation has been published since this page was loaded.",
                resource="commerce_representation",
                identifier=str(current.id),
            )
        compiler_run = await self._compiler_run(merchant.id, current)
        if compiler_run is None:
            raise ConflictError(
                "no_published_representation",
                "The published representation names no compiler run.",
                resource=RESOURCE,
                identifier=str(merchant.id),
            )
        suite = await self._current_suite(merchant.id)
        if suite is None:
            raise ConflictError(
                "benchmark_suite_unavailable",
                "No benchmark suite is published for this merchant.",
                resource="benchmark_suite",
                identifier=merchant.slug,
            )
        environment = await self._current_environment(merchant.id)
        if environment is None:
            raise ConflictError(
                "benchmark_world_unregistered",
                "This merchant has no registered benchmark world.",
                resource="benchmark_environment",
                identifier=merchant.slug,
            )
        pending = await self._pending(merchant.id)
        if pending is not None:
            raise ConflictError(
                "reevaluation_already_pending",
                "A re-evaluation is already queued or running for this merchant.",
                resource=RESOURCE,
                identifier=str(pending.id),
            )
        active = await self._runs.active_run_id(merchant_id=merchant.id)
        if active is not None:
            raise ConflictError(
                "run_already_active",
                f"benchmark run {active} is executing against this merchant's world",
                resource="benchmark_run",
                identifier=str(active),
            )
        return _Resolved(
            merchant=merchant,
            representation=current,
            compiler_run=compiler_run,
            suite=suite,
            environment=environment,
            baseline=await self._baseline(merchant.id, suite.id),
        )

    async def _merchant(self, merchant_id: uuid.UUID) -> Merchant:
        merchant = (
            await self._session.execute(select(Merchant).where(Merchant.id == merchant_id))
        ).scalar_one_or_none()
        if merchant is None:
            raise NotFoundError("merchant", str(merchant_id))
        return merchant

    async def _current_representation(
        self, merchant_id: uuid.UUID
    ) -> CommerceRepresentation | None:
        """The compiler-produced representation this merchant is publishing now.

        Newest by identifier, which is time ordered here because these are version 7 UUIDs. The
        same rule the overview's representation state uses, so the console never shows one
        artifact and launches another.
        """
        return (
            await self._session.execute(
                select(CommerceRepresentation)
                .where(
                    CommerceRepresentation.merchant_id == merchant_id,
                    CommerceRepresentation.producer == RepresentationProducer.COMPILER,
                )
                .order_by(CommerceRepresentation.id.desc())
                .limit(1)
            )
        ).scalar_one_or_none()

    async def _compiler_run(
        self, merchant_id: uuid.UUID, representation: CommerceRepresentation
    ) -> CompilerRun | None:
        if representation.compiler_run_id is None:
            return None
        return (
            await self._session.execute(
                select(CompilerRun).where(
                    CompilerRun.id == representation.compiler_run_id,
                    CompilerRun.merchant_id == merchant_id,
                )
            )
        ).scalar_one_or_none()

    async def _current_suite(self, merchant_id: uuid.UUID) -> BenchmarkSuite | None:
        """The most recently published suite authored against this merchant.

        Suites are global immutable templates and a merchant does not own one, so there is no
        field saying which is theirs. The rule is stated rather than inferred: newest published
        suite whose authored merchant slug is this merchant's, and the preflight names it so the
        merchant reads which workload they are about to spend on.
        """
        merchant = await self._merchant(merchant_id)
        row = (
            await self._session.execute(
                select(BenchmarkSuite.id)
                .where(BenchmarkSuite.merchant_slug == merchant.slug)
                .order_by(BenchmarkSuite.created_at.desc(), BenchmarkSuite.id.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        return None if row is None else await self._suites.get_by_id(row)

    async def _current_environment(self, merchant_id: uuid.UUID) -> BenchmarkEnvironment | None:
        return (
            await self._session.execute(
                select(BenchmarkEnvironment)
                .where(BenchmarkEnvironment.merchant_id == merchant_id)
                .order_by(BenchmarkEnvironment.created_at.desc(), BenchmarkEnvironment.id.desc())
                .limit(1)
            )
        ).scalar_one_or_none()

    async def _pending(self, merchant_id: uuid.UUID) -> BenchmarkReevaluation | None:
        return (
            await self._session.execute(
                select(BenchmarkReevaluation).where(
                    BenchmarkReevaluation.merchant_id == merchant_id,
                    BenchmarkReevaluation.status.in_(
                        [ReevaluationStatus.QUEUED, ReevaluationStatus.EXECUTING]
                    ),
                )
            )
        ).scalar_one_or_none()

    async def _baseline(self, merchant_id: uuid.UUID, suite_id: uuid.UUID) -> BenchmarkRun | None:
        """The prior run this launch is to be read against, frozen before anything executes.

        The most recent completed run of the same suite, and nothing else. Holding the suite
        fixed is the one comparison rule this repository has always insisted on, and choosing
        the baseline afterwards would let a reader pick the prior run that flattered the result.
        None is an ordinary answer: a merchant's first run has nothing to be compared with.
        """
        return (
            await self._session.execute(
                select(BenchmarkRun)
                .where(
                    BenchmarkRun.merchant_id == merchant_id,
                    BenchmarkRun.suite_id == suite_id,
                    BenchmarkRun.status == BenchmarkRunStatus.COMPLETED,
                )
                .order_by(BenchmarkRun.completed_at.desc(), BenchmarkRun.id.desc())
                .limit(1)
            )
        ).scalar_one_or_none()

    async def _by_request_key(
        self, merchant_id: uuid.UUID, request_key: str
    ) -> BenchmarkReevaluation | None:
        return (
            await self._session.execute(
                select(BenchmarkReevaluation).where(
                    BenchmarkReevaluation.merchant_id == merchant_id,
                    BenchmarkReevaluation.request_key == request_key,
                )
            )
        ).scalar_one_or_none()


class ReevaluationWorkerService:
    """The execution half: claim a queued launch, bind its run, and settle it.

    Nothing here reads a browser session or accepts a merchant identity from a caller who is not
    the database. By the time this runs the request that admitted the launch is long gone, and
    the row is the only trusted statement of what was asked for.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def claim_next(self, merchant_id: uuid.UUID) -> BenchmarkReevaluation | None:
        """The oldest queued launch for one merchant, locked for this transaction.

        `SKIP LOCKED` so a second worker takes the next one instead of waiting on a row it is
        never going to get. The launch is not transitioned here: it stays QUEUED until a run
        actually exists, so a worker that dies between claiming and starting leaves a launch
        that is still honestly queued.
        """
        return (
            await self._session.execute(
                select(BenchmarkReevaluation)
                .where(
                    BenchmarkReevaluation.merchant_id == merchant_id,
                    BenchmarkReevaluation.status == ReevaluationStatus.QUEUED,
                )
                .order_by(BenchmarkReevaluation.requested_at, BenchmarkReevaluation.id)
                .limit(1)
                .with_for_update(skip_locked=True)
            )
        ).scalar_one_or_none()

    async def bind_run(self, reevaluation_id: uuid.UUID, run_id: uuid.UUID) -> None:
        """Record that this launch produced exactly one benchmark run."""
        launch = await self._locked(reevaluation_id)
        if launch.status is not ReevaluationStatus.QUEUED:
            raise ConflictError(
                "reevaluation_not_queued",
                f"benchmark re-evaluation {launch.id} is {launch.status.value}",
                resource=RESOURCE,
                identifier=str(launch.id),
            )
        launch.run_id = run_id
        launch.started_at = datetime.now(UTC)
        launch.status = ReevaluationStatus.EXECUTING
        await self._session.commit()

    async def settle_completed(self, reevaluation_id: uuid.UUID) -> None:
        """Settle a launch whose bound run reached COMPLETED.

        The database checks the run agrees before this is allowed to land, so this status can
        never say a run finished that did not.
        """
        launch = await self._locked(reevaluation_id)
        if launch.status is not ReevaluationStatus.EXECUTING:
            return
        launch.status = ReevaluationStatus.COMPLETED
        launch.settled_at = datetime.now(UTC)
        await self._session.commit()

    async def settle_failed(self, reevaluation_id: uuid.UUID, *, failure_code: str) -> None:
        """Settle a launch that cannot produce a finished run, in our own vocabulary.

        The code is one of this repository's, never a provider's words and never an exception's
        text. A merchant reading it is reading a fact about the launch rather than a stack.
        """
        launch = await self._locked(reevaluation_id)
        if launch.status not in {ReevaluationStatus.QUEUED, ReevaluationStatus.EXECUTING}:
            return
        launch.status = ReevaluationStatus.FAILED
        launch.failure_code = failure_code
        launch.settled_at = datetime.now(UTC)
        await self._session.commit()

    async def settle_for_aborted_run(self, run_id: uuid.UUID) -> BenchmarkReevaluation | None:
        """Close the launch an aborted run belonged to, if it belonged to one.

        An aborted run is an operator saying that a stopped execution is not coming back. The
        launch behind it is then over too, and leaving it EXECUTING would hold this merchant's
        one pending slot against a run nobody is going to finish.
        """
        launch = (
            await self._session.execute(
                select(BenchmarkReevaluation)
                .where(BenchmarkReevaluation.run_id == run_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if launch is None or launch.status is not ReevaluationStatus.EXECUTING:
            return launch
        launch.status = ReevaluationStatus.FAILED
        launch.failure_code = "run_aborted"
        launch.settled_at = datetime.now(UTC)
        await self._session.commit()
        return launch

    async def _locked(self, reevaluation_id: uuid.UUID) -> BenchmarkReevaluation:
        launch = (
            await self._session.execute(
                select(BenchmarkReevaluation)
                .where(BenchmarkReevaluation.id == reevaluation_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if launch is None:
            raise NotFoundError(RESOURCE, str(reevaluation_id))
        return launch
