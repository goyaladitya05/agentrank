"""Admitting and settling a merchant's request to run one benchmark evaluation.

Two commands land here and the server decides which one a merchant is making, because which one
it is follows from what they have rather than from what a browser asks for.

```text
INITIAL        the merchant has published no agent-ready representation and has no completed
               benchmark run, so there is nothing to measure again and no evidence at all. The
               honest thing to measure is the merchant as they are: the ordinary storefront
               discovery boundary, and their own information as recorded in their current
               source snapshot. Nothing is compiled and nothing is compared
REEVALUATION   the merchant is publishing an agent-ready representation, so the thing to
               measure is that artifact, against whichever prior run of the same suite the
               launch freezes
```

A merchant who has published always gets the second, whether or not they have ever run a
benchmark, which is the Phase 4D behaviour exactly. A merchant who has not published and has a
completed run already gets the second too, blocked, and is told to publish: an initial
evaluation is the bootstrap out of having no evidence, not a second way to measure a storefront
whenever somebody feels like it.

The last step is a separate command on purpose either way. Publishing writes an artifact and
spends nothing, and provisioning a merchant measures nothing; launching spends model quota and
takes as long as a suite takes, and a workflow that started one as a side effect of the other
would be spending on the merchant's behalf without being asked.

Two halves live here and they run in different processes.

The merchant half is `MerchantEvaluationLaunchService`. It answers what a launch would evaluate,
and it admits one. Admission resolves every methodology-critical identity server side from the
merchant its credential authenticated, checks it against the digest of the plan the merchant was
actually shown, freezes it on the row, and commits before answering. Nothing is executed: the
answer is a queued launch, which is exactly what an honest response can promise when the work has
not started.

The worker half is `EvaluationLaunchWorkerService`. It claims a queued launch, binds the benchmark
run it produced, and settles the launch when the run finishes. It never reads a browser session
and never takes a merchant identity from anything but the persisted row, because by the time it
runs there is no request and no session left to trust.

What the browser may say is which purpose it was shown, the representation it was shown if that
purpose has one, a request key and the digest of the preflight it read. None of them selects
anything; each is checked against what the server resolves. Everything else, including which
merchant this is, comes from the credential and from the database.
"""

import hashlib
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from agentrank_api.benchmark.environment import BenchmarkEnvironmentService
from agentrank_api.benchmark.evaluation_launch import (
    BenchmarkEvaluationLaunch,
    BuyerProfile,
    EvaluationLaunchStatus,
    EvaluationPurpose,
)
from agentrank_api.benchmark.execution import REFERENCE_ISOLATED_KIND
from agentrank_api.benchmark.identity import canonical_json
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
from agentrank_api.benchmark.repository import BenchmarkRunRepository, BenchmarkSuiteRepository
from agentrank_api.commerce.models import Merchant
from agentrank_api.compiler.models import CompilerRun
from agentrank_api.config import Settings
from agentrank_api.conflicts import translated_conflicts
from agentrank_api.errors import ConflictError, NotFoundError
from agentrank_api.representation.definitions import RepresentationProducer
from agentrank_api.representation.intake import MerchantSourceIntakeService, SourceIdentity
from agentrank_api.representation.models import CommerceRepresentation, MerchantSourceSnapshot

RESOURCE = "benchmark_evaluation_launch"

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

    An evaluation is only evidence about an autonomous agent when an agent does the shopping,
    and only the model buyer is given a discovery surface at all: the pinned representation for
    a re-evaluation, the ordinary storefront for an initial evaluation. With no provider
    credential configured there is no model buyer, so the launch falls back to the deterministic
    reference buyer and every surface says so in those words: it is not an AI agent, it reads
    structured commerce fields a storefront does not publish, and what it measures is that the
    benchmark path works rather than how readable this merchant is.
    """
    if settings.openai is not None:
        configuration = AgentConfiguration(provider=OPENAI_PROVIDER, requested_model=OPENAI_MODEL)
        return BuyerPlan(BuyerProfile.AI_BUYER, executor_kind(configuration), configuration)
    if settings.gemini is not None:
        configuration = AgentConfiguration(provider=GEMINI_PROVIDER, requested_model=GEMINI_MODEL)
        return BuyerPlan(BuyerProfile.AI_BUYER, executor_kind(configuration), configuration)
    return BuyerPlan(BuyerProfile.REFERENCE_BUYER, REFERENCE_EXECUTOR_KIND, None)


def _delivers_representation(purpose: EvaluationPurpose, buyer: BuyerPlan) -> bool:
    """Whether the run this launch would produce will pin a Commerce IR representation.

    The dispatcher's rule, stated once here so the preflight predicts what will actually happen.
    Only the model buyer receives a discovery surface at all, and only a re-evaluation gives it
    the agent-ready one; a first evaluation gives it the ordinary storefront, and the reference
    buyer is given neither.
    """
    return purpose is EvaluationPurpose.REEVALUATION and buyer.profile is BuyerProfile.AI_BUYER


@dataclass(frozen=True, slots=True)
class LaunchBlocker:
    """One reason an evaluation cannot be launched right now, with a merchant sentence."""

    code: str
    message: str


@dataclass(frozen=True, slots=True)
class EvaluationPlan:
    """Everything a merchant should know before spending a benchmark run.

    Deliberately without a currency figure. AgentRank has no trustworthy provider pricing data,
    and a number invented from one would be the most confident thing on the page. What is here
    instead is what will actually be executed and the bounds it is executed under, which is
    checkable.
    """

    purpose: EvaluationPurpose
    representation_id: uuid.UUID | None
    representation_label: str | None
    compiler_run_id: uuid.UUID | None
    source_snapshot_id: uuid.UUID | None
    # Named only when the source is what is being measured. A re-evaluation's source identifier
    # is the provenance of the artifact under test rather than the artifact, and the
    # representation's own label already names that, so resolving a second label for it would be
    # a query for something nobody reads.
    source_snapshot_label: str | None
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
    # Whether the prior run's buyer read the same kind of surface this launch's will. False is
    # the honest warning a merchant needs before spending: the comparison engine refuses to draw
    # a before and after across the storefront and agent-ready arms, so this launch would produce
    # a result with no reading beside it.
    #
    # A prediction of one input to that engine's rule and never a second copy of the rule. What
    # is comparable is still decided after the fact, from the runs, by the engine.
    baseline_surface_matches: bool | None
    blockers: tuple[LaunchBlocker, ...]
    pending_launch_id: uuid.UUID | None

    @property
    def launchable(self) -> bool:
        return not self.blockers

    @property
    def digest(self) -> str:
        """A labelled digest over everything this preflight tells the merchant will be used.

        The launch carries it back and admission refuses a mismatch. Only the representation was
        guarded before, which left the suite, the world and the buyer free to be re-resolved
        between the page render and the submit: a newly published suite version, a newly
        registered fixture, or a provider credential appearing or disappearing between two API
        processes would all have been frozen silently. The values were recorded, so nothing was
        corrupted, but a merchant was never told they had changed, and this makes the refusal
        the same shape the representation already had.

        Blockers and the pending launch are deliberately outside it. They are state rather than
        identity, they are refused on their own terms, and folding them in would turn every
        ordinary refusal into an unexplained digest mismatch.
        """
        return "sha256:" + hashlib.sha256(canonical_json(self._identity()).encode()).hexdigest()

    def _identity(self) -> dict[str, Any]:
        """Exactly the fields a merchant reads off the preflight before committing."""
        return {
            "purpose": self.purpose.value,
            "representation_id": _text(self.representation_id),
            "compiler_run_id": _text(self.compiler_run_id),
            "source_snapshot_id": _text(self.source_snapshot_id),
            "source_snapshot_label": self.source_snapshot_label,
            "suite_id": _text(self.suite_id),
            "suite_label": self.suite_label,
            "suite_definition_hash": self.suite_definition_hash,
            "mission_count": self.mission_count,
            "environment_id": _text(self.environment_id),
            "environment_label": self.environment_label,
            "buyer_profile": self.buyer_profile.value,
            "executor_kind": self.executor_kind,
            "provider": self.provider,
            "requested_model": self.requested_model,
            "max_model_turns": self.max_model_turns,
            "max_tool_calls": self.max_tool_calls,
            "mission_deadline_seconds": self.mission_deadline_seconds,
            "baseline_run_id": _text(self.baseline_run_id),
            "baseline_surface_matches": self.baseline_surface_matches,
        }


def _text(value: uuid.UUID | None) -> str | None:
    return None if value is None else str(value)


@dataclass(frozen=True, slots=True)
class _Labels:
    """Everything a page of launches needs, read once rather than once per launch."""

    suite_labels: dict[uuid.UUID, str]
    mission_counts: dict[uuid.UUID, int]
    environment_labels: dict[uuid.UUID, str]
    representation_labels: dict[uuid.UUID, str]
    source_labels: dict[uuid.UUID, str]
    run_statuses: dict[uuid.UUID, str]
    missions_finished: dict[uuid.UUID, int]


@dataclass(frozen=True, slots=True)
class EvaluationLaunchDetail:
    """One launch as a merchant reads it: what was frozen, and where execution has got to.

    Progress is counts of missions this run has actually finished, never a percentage and never
    an estimate of time remaining. AgentRank knows how many missions a suite has and how many of
    them reached a terminal state, and a bar moving on a timer would be inventing the rest.
    """

    launch_id: uuid.UUID
    status: EvaluationLaunchStatus
    failure_code: str | None
    requested_at: datetime
    started_at: datetime | None
    settled_at: datetime | None
    purpose: EvaluationPurpose
    representation_id: uuid.UUID | None
    representation_label: str | None
    compiler_run_id: uuid.UUID | None
    source_snapshot_id: uuid.UUID | None
    source_snapshot_label: str | None
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


class MerchantEvaluationLaunchService:
    """The merchant-facing half: what a launch would do, and admitting one."""

    def __init__(self, session: AsyncSession, settings: Settings) -> None:
        self._session = session
        self._settings = settings
        self._suites = BenchmarkSuiteRepository(session)
        self._runs = BenchmarkRunRepository(session)
        self._environments = BenchmarkEnvironmentService(session)
        self._sources = MerchantSourceIntakeService(session)

    async def plan(self, merchant_id: uuid.UUID) -> EvaluationPlan:
        """What a launch would evaluate now, and what stops it if anything does.

        The reads are ordered, and the order is about one thing. PostgreSQL gives each statement
        here its own snapshot, and the advisory lock admission holds serializes other launches
        rather than benchmark runs, so a run reaching COMPLETED partway through this method is a
        real interleaving. Whether this merchant has any completed evidence is therefore the
        last of the state reads: a run that finished earlier is seen and makes this a
        re-evaluation, and one still executing is refused for being active. What is left is the
        window between that read and the insert, which no ordering closes. See
        docs/shortcomings.md.

        The blockers are still appended in their own fixed order, because the first of them is
        the refusal a merchant is given and that should not depend on which read happened first.
        """
        buyer = resolve_buyer(self._settings)
        blockers: list[LaunchBlocker] = []

        representation = await self._current_representation(merchant_id)
        suite = await self._current_suite(merchant_id)
        environment = await self._current_environment(merchant_id)
        pending = await self._pending(merchant_id)
        active = await self._runs.active_run_id(merchant_id=merchant_id)
        purpose = await self._purpose(merchant_id, representation)
        compiler_run = None
        source: SourceIdentity | None = None
        if purpose is EvaluationPurpose.INITIAL:
            source = await self._sources.current_identity(merchant_id)
            if source is None:
                blockers.append(
                    LaunchBlocker(
                        "merchant_source_unavailable",
                        "AgentRank has no record of your merchant information yet, so there is"
                        " nothing to evaluate you against. Add your merchant source first.",
                    )
                )
        elif representation is None:
            blockers.append(
                LaunchBlocker(
                    "no_published_representation",
                    "Publish an agent-ready representation before requesting an evaluation of one.",
                )
            )
        else:
            compiler_run = await self._compiler_run(merchant_id, representation)
            if compiler_run is None:
                # Held impossible by a check constraint and a RESTRICT foreign key, and still a
                # named refusal rather than an assertion: a merchant who has published is never
                # told to publish, and a broken lineage is not a 500.
                blockers.append(
                    LaunchBlocker(
                        "representation_lineage_unreadable",
                        "The published representation names a compiler run that cannot be read."
                        " Your operator can see why; this is not something to fix from here.",
                    )
                )

        if suite is None:
            blockers.append(
                LaunchBlocker(
                    "benchmark_suite_unavailable",
                    "No benchmark suite is published for this merchant, so there is nothing to"
                    " run. Your operator publishes one from the benchmark command line.",
                )
            )

        if environment is None:
            blockers.append(
                LaunchBlocker(
                    "benchmark_world_unregistered",
                    "This merchant has no registered benchmark world, so a run has no catalog to"
                    " be put back to. Your operator registers one from the benchmark command"
                    " line.",
                )
            )

        if pending is not None:
            blockers.append(
                LaunchBlocker(
                    "evaluation_already_pending",
                    "An evaluation is already queued or running for this merchant. Wait for it"
                    " to finish before starting another.",
                )
            )
        if active is not None and (pending is None or pending.run_id != active):
            blockers.append(
                LaunchBlocker(
                    "run_already_active",
                    "A benchmark run is already executing against this merchant's world. Only one"
                    " run may own that world at a time.",
                )
            )

        # An initial evaluation is admitted only while the merchant has no completed run, so
        # there is nothing for one to be read against and the schema refuses to hold anything
        # anyway. Not resolved rather than resolved and discarded: a merchant's first evaluation
        # has no before, and this is where that is true rather than somewhere it is filtered.
        baseline = (
            None
            if suite is None or purpose is EvaluationPurpose.INITIAL
            else await self._baseline(merchant_id, suite.id)
        )
        configuration = buyer.configuration
        return EvaluationPlan(
            purpose=purpose,
            representation_id=None if representation is None else representation.id,
            representation_label=None if representation is None else representation.label,
            compiler_run_id=None if compiler_run is None else compiler_run.id,
            source_snapshot_id=(
                source.snapshot_id
                if source is not None
                else (None if representation is None else representation.source_snapshot_id)
            ),
            source_snapshot_label=None if source is None else source.label,
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
            baseline_surface_matches=(
                None
                if baseline is None
                else (baseline.representation_id is not None)
                == _delivers_representation(purpose, buyer)
            ),
            blockers=tuple(blockers),
            pending_launch_id=None if pending is None else pending.id,
        )

    async def request(
        self,
        merchant_id: uuid.UUID,
        *,
        purpose: EvaluationPurpose,
        representation_id: uuid.UUID | None,
        request_key: str,
        plan_digest: str,
    ) -> BenchmarkEvaluationLaunch:
        """Admit one launch, or answer with the one this request key already produced.

        The advisory lock on this merchant's benchmark world is taken first, which is what makes
        two simultaneous launches produce one deterministic refusal rather than whatever the
        unique index happens to give them. The index is still what makes the invariant true
        across processes; this makes the answer the same every time.

        What is frozen is the plan resolved under that lock, and it is frozen only after being
        checked against the plan the merchant was actually shown. Nothing is executed here and
        nothing external is called, so this whole method is one short transaction. What comes
        back is a queued launch, which is the only thing an honest answer can promise while the
        work has not started.

        The purpose is checked, never taken. A caller states which command it believes it is
        making, and a caller that believes it is measuring a published representation is refused
        rather than quietly given a measurement of the storefront, or the other way round.
        """
        merchant = await self._merchant(merchant_id)
        existing = await self._by_request_key(merchant_id, request_key)
        if existing is not None:
            return self._same_request(existing, purpose, representation_id)

        await self._environments.claim(merchant.slug)
        # Read again under the lock: a concurrent identical submit may have committed between
        # the read above and the lock, and answering with its launch is the whole point of a
        # request key.
        existing = await self._by_request_key(merchant_id, request_key)
        if existing is not None:
            return self._same_request(existing, purpose, representation_id)

        plan = await self.plan(merchant_id)
        self._require_launchable(plan, merchant, purpose, representation_id, plan_digest)
        buyer = resolve_buyer(self._settings)
        initial = plan.purpose is EvaluationPurpose.INITIAL
        # A launchable plan resolved every identity its own purpose needs.
        assert initial or plan.representation_id is not None
        assert initial or plan.compiler_run_id is not None
        assert not initial or plan.source_snapshot_id is not None
        assert plan.suite_id is not None
        assert plan.environment_id is not None
        launch = BenchmarkEvaluationLaunch(
            merchant_id=merchant.id,
            request_key=request_key,
            purpose=plan.purpose,
            representation_id=None if initial else plan.representation_id,
            compiler_run_id=None if initial else plan.compiler_run_id,
            source_snapshot_id=plan.source_snapshot_id if initial else None,
            suite_id=plan.suite_id,
            environment_id=plan.environment_id,
            buyer_profile=buyer.profile,
            buyer_configuration=(
                None if buyer.configuration is None else buyer.configuration.payload()
            ),
            buyer_configuration_digest=(
                None if buyer.configuration is None else buyer.configuration.configuration_digest
            ),
            executor_kind=buyer.executor_kind,
            status=EvaluationLaunchStatus.QUEUED,
            baseline_run_id=plan.baseline_run_id,
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
                return self._same_request(duplicate, purpose, representation_id)
            raise
        return launch

    def _require_launchable(
        self,
        plan: EvaluationPlan,
        merchant: Merchant,
        purpose: EvaluationPurpose,
        representation_id: uuid.UUID | None,
        plan_digest: str,
    ) -> None:
        """Refuse a launch this merchant cannot have, or one they were shown differently.

        Blockers first and by their own name, because a merchant with nothing published needs to
        be told that rather than told a digest disagreed. The purpose second, because which
        command this is decides what everything else means and a mismatch there is not a stale
        field, it is a different request. The representation third, because for a re-evaluation
        it is the artifact the whole command is about and deserves its own sentence. Everything
        else the preflight showed is covered by the digest at once: what a merchant committed to
        is the whole plan they read, and any part of it moving underneath them is one refusal.
        """
        blocker = next(iter(plan.blockers), None)
        if blocker is not None:
            raise ConflictError(
                blocker.code,
                blocker.message,
                resource=RESOURCE,
                identifier=str(merchant.id),
            )
        if plan.purpose is not purpose:
            # The merchant published between the page render and the submit, or their first
            # evaluation finished. Either way the command they read is no longer the command
            # this would make, and running the other one would measure something they did not
            # ask about.
            raise ConflictError(
                "evaluation_purpose_superseded",
                "What AgentRank would evaluate for this merchant has changed since this page was"
                " loaded.",
                resource=RESOURCE,
                identifier=str(merchant.id),
            )
        if plan.purpose is EvaluationPurpose.INITIAL:
            if representation_id is not None:
                raise ConflictError(
                    "initial_evaluation_names_no_representation",
                    "A first evaluation measures your current merchant-facing state, so it"
                    " cannot name a published representation.",
                    resource=RESOURCE,
                    identifier=str(merchant.id),
                )
        elif plan.representation_id != representation_id:
            # Either somebody published a newer representation while this page was open, or the
            # browser named an older one. Both are the same refusal: a re-evaluation measures
            # what is published now, and running an artifact the merchant is no longer publishing
            # would produce evidence about nothing they can act on.
            raise ConflictError(
                "representation_superseded",
                "A newer agent-ready representation has been published since this page was loaded.",
                resource="commerce_representation",
                identifier=str(plan.representation_id),
            )
        if plan.digest != plan_digest:
            raise ConflictError(
                "preflight_superseded",
                "What this evaluation would run has changed since this page was loaded.",
                resource=RESOURCE,
                identifier=str(merchant.id),
            )

    async def detail(self, merchant_id: uuid.UUID, launch_id: uuid.UUID) -> EvaluationLaunchDetail:
        """One launch with its frozen identity resolved to labels and its honest progress."""
        launch = await self.get(merchant_id, launch_id)
        return self._detail(launch, await self._resolve([launch]))

    async def details(
        self, merchant_id: uuid.UUID, *, limit: int = 10
    ) -> list[EvaluationLaunchDetail]:
        launches = await self.recent(merchant_id, limit=limit)
        resolved = await self._resolve(launches)
        return [self._detail(launch, resolved) for launch in launches]

    async def _resolve(self, launches: Sequence[BenchmarkEvaluationLaunch]) -> _Labels:
        """Everything a page of launches needs to be described, in a fixed number of statements.

        Batched rather than per launch. The launch list is a merchant's history and the launch
        page re-reads itself while a run is executing, so a read that grew a handful of
        statements per row would be the console's most wasteful query pattern by a wide margin.

        Mission counts are aggregates rather than loaded rows. Neither the suite's shape nor a
        run's progress needs the rows themselves, and loading fourteen missions and fourteen
        results to produce two integers is the same defect one level down.
        """
        if not launches:
            return _Labels({}, {}, {}, {}, {}, {}, {})
        merchant_ids = {launch.merchant_id for launch in launches}
        suite_ids = {launch.suite_id for launch in launches}
        environment_ids = {launch.environment_id for launch in launches}
        representation_ids = {
            launch.representation_id for launch in launches if launch.representation_id is not None
        }
        source_ids = {
            launch.source_snapshot_id
            for launch in launches
            if launch.source_snapshot_id is not None
        }
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
        # Key, version and identifier only. A source document is up to a hundred and twenty
        # eight kilobytes of JSONB, and a launch list that loaded one per row to print a label
        # would be the most expensive read in the console by a wide margin.
        source_labels: dict[uuid.UUID, str] = {}
        if source_ids:
            sources = await self._session.execute(
                select(
                    MerchantSourceSnapshot.id,
                    MerchantSourceSnapshot.source_key,
                    MerchantSourceSnapshot.source_version,
                ).where(
                    MerchantSourceSnapshot.id.in_(source_ids),
                    MerchantSourceSnapshot.merchant_id.in_(merchant_ids),
                )
            )
            source_labels = {
                row.id: f"{row.source_key}@{row.source_version}" for row in sources.all()
            }
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
            source_labels=source_labels,
            run_statuses=run_statuses,
            missions_finished=finished,
        )

    def _detail(self, launch: BenchmarkEvaluationLaunch, labels: _Labels) -> EvaluationLaunchDetail:
        configuration = (
            None
            if launch.buyer_configuration is None
            else AgentConfiguration.from_payload(launch.buyer_configuration)
        )
        return EvaluationLaunchDetail(
            launch_id=launch.id,
            purpose=launch.purpose,
            status=launch.status,
            failure_code=launch.failure_code,
            requested_at=launch.requested_at,
            started_at=launch.started_at,
            settled_at=launch.settled_at,
            representation_id=launch.representation_id,
            representation_label=(
                None
                if launch.representation_id is None
                else labels.representation_labels.get(launch.representation_id, "")
            ),
            compiler_run_id=launch.compiler_run_id,
            source_snapshot_id=launch.source_snapshot_id,
            source_snapshot_label=(
                None
                if launch.source_snapshot_id is None
                else labels.source_labels.get(launch.source_snapshot_id, "")
            ),
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

    async def get(self, merchant_id: uuid.UUID, launch_id: uuid.UUID) -> BenchmarkEvaluationLaunch:
        launch = (
            await self._session.execute(
                select(BenchmarkEvaluationLaunch).where(
                    BenchmarkEvaluationLaunch.id == launch_id,
                    BenchmarkEvaluationLaunch.merchant_id == merchant_id,
                )
            )
        ).scalar_one_or_none()
        if launch is None:
            raise NotFoundError(RESOURCE, str(launch_id))
        return launch

    async def recent(
        self, merchant_id: uuid.UUID, *, limit: int = 10
    ) -> list[BenchmarkEvaluationLaunch]:
        rows = await self._session.execute(
            select(BenchmarkEvaluationLaunch)
            .where(BenchmarkEvaluationLaunch.merchant_id == merchant_id)
            .order_by(
                BenchmarkEvaluationLaunch.requested_at.desc(), BenchmarkEvaluationLaunch.id.desc()
            )
            .limit(limit)
        )
        return list(rows.scalars())

    def _same_request(
        self,
        existing: BenchmarkEvaluationLaunch,
        purpose: EvaluationPurpose,
        representation_id: uuid.UUID | None,
    ) -> BenchmarkEvaluationLaunch:
        """The launch this request key already produced, or a refusal that it asked for another.

        A retry after a lost response repeats the key, the purpose and the representation, and
        gets its own launch back. Either of the first two differing under a key that has already
        been used is a different command wearing an old name, and answering with the old launch
        would tell a merchant something was queued that was not.
        """
        if existing.purpose is not purpose or existing.representation_id != representation_id:
            raise ConflictError(
                "evaluation_request_key_reused",
                "This request has already launched a different evaluation.",
                resource=RESOURCE,
                identifier=str(existing.id),
            )
        return existing

    async def _merchant(self, merchant_id: uuid.UUID) -> Merchant:
        merchant = (
            await self._session.execute(select(Merchant).where(Merchant.id == merchant_id))
        ).scalar_one_or_none()
        if merchant is None:
            raise NotFoundError("merchant", str(merchant_id))
        return merchant

    async def _purpose(
        self, merchant_id: uuid.UUID, representation: CommerceRepresentation | None
    ) -> EvaluationPurpose:
        """Which command this merchant is making, decided from what they have.

        A merchant publishing an agent-ready representation is asking about that artifact, and
        that is true whether or not they have ever run a benchmark: a first run of a published
        representation was always admissible and still is, with nothing to compare it against
        and every surface saying so.

        Everyone else is deciding between two things. With no completed run there is no evidence
        about this merchant at all, and the honest measurement is of the merchant as they are.
        With a completed run there already is, so the merchant is not bootstrapping and is told
        to publish rather than handed a second way to measure a storefront: repeating an initial
        evaluation once evidence exists would be a raw arm collected whenever somebody felt like
        it, which is a controlled experiment's job and is a different thing with different rules.

        An aborted run is not a completed one and does not count. A merchant whose first
        evaluation stopped has no result, and stranding them because something already failed
        would be the opposite of what this exists for.
        """
        if representation is not None:
            return EvaluationPurpose.REEVALUATION
        completed = await self._session.scalar(
            select(BenchmarkRun.id)
            .where(
                BenchmarkRun.merchant_id == merchant_id,
                BenchmarkRun.status == BenchmarkRunStatus.COMPLETED,
            )
            .limit(1)
        )
        return (
            EvaluationPurpose.REEVALUATION if completed is not None else EvaluationPurpose.INITIAL
        )

    async def _current_representation(
        self, merchant_id: uuid.UUID
    ) -> CommerceRepresentation | None:
        """The compiler-produced representation this merchant is publishing now.

        Newest by `write_order`, the value PostgreSQL assigns at INSERT. The same rule the
        overview's representation state uses, so the console never shows one artifact and
        launches another, and the same rule the merchant's source history is read with.

        This decides what a re-evaluation measures, which is why it is not ordered by the
        primary key. Those are version 7 UUIDs generated in Python, monotonic only within one
        process, so two processes publishing in the same millisecond would order themselves by a
        random draw and the launch could measure the representation the merchant replaced.
        """
        return (
            await self._session.execute(
                select(CommerceRepresentation)
                .where(
                    CommerceRepresentation.merchant_id == merchant_id,
                    CommerceRepresentation.producer == RepresentationProducer.COMPILER,
                )
                .order_by(CommerceRepresentation.write_order.desc())
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

    async def _pending(self, merchant_id: uuid.UUID) -> BenchmarkEvaluationLaunch | None:
        return (
            await self._session.execute(
                select(BenchmarkEvaluationLaunch).where(
                    BenchmarkEvaluationLaunch.merchant_id == merchant_id,
                    BenchmarkEvaluationLaunch.status.in_(
                        [EvaluationLaunchStatus.QUEUED, EvaluationLaunchStatus.EXECUTING]
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
    ) -> BenchmarkEvaluationLaunch | None:
        return (
            await self._session.execute(
                select(BenchmarkEvaluationLaunch).where(
                    BenchmarkEvaluationLaunch.merchant_id == merchant_id,
                    BenchmarkEvaluationLaunch.request_key == request_key,
                )
            )
        ).scalar_one_or_none()


class EvaluationLaunchWorkerService:
    """The execution half: claim a queued launch, bind its run, and settle it.

    Nothing here reads a browser session or accepts a merchant identity from a caller who is not
    the database. By the time this runs the request that admitted the launch is long gone, and
    the row is the only trusted statement of what was asked for.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def claim_next(
        self, merchant_id: uuid.UUID, *, environment_id: uuid.UUID
    ) -> BenchmarkEvaluationLaunch | None:
        """The oldest queued launch for one merchant's world, locked for this transaction.

        Scoped to the world the claiming worker holds, and that scope is load bearing rather
        than an optimisation. A settled launch is terminal and cannot be deleted, so a worker
        that claimed a launch it could only refuse would destroy a merchant's request that a
        differently configured worker could have served. A launch this worker cannot serve is
        never taken from one that can.

        `SKIP LOCKED` so a second worker takes the next one instead of waiting on a row it is
        never going to get. The launch is not transitioned here: it stays QUEUED until a run
        actually exists, so a worker that dies between claiming and starting leaves a launch
        that is still honestly queued.
        """
        return (
            await self._session.execute(
                select(BenchmarkEvaluationLaunch)
                .where(
                    BenchmarkEvaluationLaunch.merchant_id == merchant_id,
                    BenchmarkEvaluationLaunch.environment_id == environment_id,
                    BenchmarkEvaluationLaunch.status == EvaluationLaunchStatus.QUEUED,
                )
                .order_by(BenchmarkEvaluationLaunch.requested_at, BenchmarkEvaluationLaunch.id)
                .limit(1)
                .with_for_update(skip_locked=True)
            )
        ).scalar_one_or_none()

    async def bind_run(self, launch_id: uuid.UUID, run_id: uuid.UUID) -> None:
        """Record that this launch produced exactly one benchmark run."""
        launch = await self._locked(launch_id)
        if launch.status is not EvaluationLaunchStatus.QUEUED:
            raise ConflictError(
                "launch_not_queued",
                f"benchmark evaluation launch {launch.id} is {launch.status.value}",
                resource=RESOURCE,
                identifier=str(launch.id),
            )
        launch.run_id = run_id
        launch.started_at = await self._clock()
        launch.status = EvaluationLaunchStatus.EXECUTING
        await self._session.commit()

    async def settle_completed(self, launch_id: uuid.UUID) -> None:
        """Settle a launch whose bound run reached COMPLETED.

        The database checks the run agrees before this is allowed to land, so this status can
        never say a run finished that did not.
        """
        launch = await self._locked(launch_id)
        if launch.status is not EvaluationLaunchStatus.EXECUTING:
            return
        launch.status = EvaluationLaunchStatus.COMPLETED
        launch.settled_at = await self._clock()
        await self._session.commit()

    async def settle_failed(self, launch_id: uuid.UUID, *, failure_code: str) -> None:
        """Settle a launch that cannot produce a finished run, in our own vocabulary.

        The code is one of this repository's, never a provider's words and never an exception's
        text. A merchant reading it is reading a fact about the launch rather than a stack.
        """
        launch = await self._locked(launch_id)
        if launch.status not in {EvaluationLaunchStatus.QUEUED, EvaluationLaunchStatus.EXECUTING}:
            return
        launch.status = EvaluationLaunchStatus.FAILED
        launch.failure_code = failure_code
        launch.settled_at = await self._clock()
        await self._session.commit()

    async def settle_for_terminal_run(self, run_id: uuid.UUID) -> BenchmarkEvaluationLaunch | None:
        """Close the launch a finished run belonged to, if it belonged to one.

        This is the operator's recovery as well as the ordinary abort path. A worker settles its
        own launch when it finishes, and a worker that dies between closing the run and settling
        the launch leaves one executing against a run that is already terminal. Nothing else can
        reach that launch: the dispatcher only claims queued ones, and the merchant's one pending
        slot is held against it until somebody does. So this reads the bound run and settles the
        launch to agree with it, which is the only settlement the database will accept anyway.

        None when the run belongs to no launch, and unchanged when the launch is already settled,
        so running it twice is running it once.
        """
        launch = (
            await self._session.execute(
                select(BenchmarkEvaluationLaunch)
                .where(BenchmarkEvaluationLaunch.run_id == run_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if launch is None or launch.status is not EvaluationLaunchStatus.EXECUTING:
            return launch
        run = (
            await self._session.execute(
                select(BenchmarkRun).where(
                    BenchmarkRun.id == run_id, BenchmarkRun.merchant_id == launch.merchant_id
                )
            )
        ).scalar_one_or_none()
        if run is None or not run.is_terminal:
            return launch
        settled = await self._clock()
        if run.status is BenchmarkRunStatus.COMPLETED:
            launch.status = EvaluationLaunchStatus.COMPLETED
        else:
            launch.status = EvaluationLaunchStatus.FAILED
            launch.failure_code = "run_aborted"
        launch.settled_at = settled
        await self._session.commit()
        return launch

    async def _clock(self) -> datetime:
        """The database's own clock, which is the one `requested_at` was written from.

        A launch's timestamps are compared with each other by check constraints, and this
        process's clock and the database's are two clocks. Skew between them would turn an
        ordinary bind into a raw integrity error after a run had already been created.
        """
        now = await self._session.scalar(select(func.now()))
        assert now is not None  # `now()` always answers
        return now

    async def _locked(self, launch_id: uuid.UUID) -> BenchmarkEvaluationLaunch:
        launch = (
            await self._session.execute(
                select(BenchmarkEvaluationLaunch)
                .where(BenchmarkEvaluationLaunch.id == launch_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if launch is None:
            raise NotFoundError(RESOURCE, str(launch_id))
        return launch
