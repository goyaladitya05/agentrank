"""Narrow persistent controls for a raw-versus-compiled merchant experiment."""

import uuid
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    Enum,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    UniqueConstraint,
    select,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from agentrank_api.benchmark.grounding import (
    MAX_REPORTED,
    contradictions,
    representation_facts,
    world_facts,
)
from agentrank_api.benchmark.identity import HASH_LENGTH, HASH_PATTERN
from agentrank_api.benchmark.lifecycle import BenchmarkRunStatus
from agentrank_api.benchmark.models import BenchmarkEnvironment, BenchmarkRun, BenchmarkSuite
from agentrank_api.commerce.models import Merchant
from agentrank_api.compiler.models import CompilerRun
from agentrank_api.errors import ConflictError, NotFoundError
from agentrank_api.models import Base
from agentrank_api.representation.definitions import RepresentationProducer
from agentrank_api.representation.models import CommerceRepresentation, MerchantSourceSnapshot
from agentrank_api.representation.projection import compiled_projection, raw_projection
from agentrank_api.workspace.models import MerchantEvaluationWorkspace


class RepresentationKind(StrEnum):
    RAW = "RAW"
    COMPILED = "COMPILED"


REPRESENTATION_KIND = Enum(
    RepresentationKind, native_enum=False, create_constraint=False, length=16
)


class CompilerImpactExperiment(Base):
    __tablename__ = "compiler_impact_experiment"
    __table_args__ = (
        UniqueConstraint("id", "merchant_id", name="uq_compiler_impact_experiment_binding"),
        ForeignKeyConstraint(["merchant_id"], ["merchant.id"], ondelete="RESTRICT"),
        ForeignKeyConstraint(["suite_id"], ["benchmark_suite.id"], ondelete="RESTRICT"),
        ForeignKeyConstraint(
            ["environment_id", "merchant_id"],
            ["benchmark_environment.id", "benchmark_environment.merchant_id"],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["source_snapshot_id", "merchant_id"],
            ["merchant_source_snapshot.id", "merchant_source_snapshot.merchant_id"],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["compiled_representation_id", "merchant_id"],
            ["commerce_representation.id", "commerce_representation.merchant_id"],
            ondelete="RESTRICT",
        ),
        CheckConstraint("sample_count > 0 AND sample_count <= 3", name="sample_count_bounded"),
        CheckConstraint(
            f"buyer_configuration_digest ~ '{HASH_PATTERN}'", name="buyer_digest_format"
        ),
        CheckConstraint(
            "jsonb_typeof(buyer_configuration) = 'object'",
            name="buyer_configuration_object",
        ),
        CheckConstraint("jsonb_typeof(methodology) = 'object'", name="methodology_object"),
        Index(None, "merchant_id"),
        Index(
            "ix_compiler_impact_experiment_environment_id_merchant_id",
            "environment_id",
            "merchant_id",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid7)
    merchant_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    suite_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    environment_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    source_snapshot_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    compiled_representation_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    buyer_configuration_digest: Mapped[str] = mapped_column(String(HASH_LENGTH), nullable=False)
    buyer_configuration: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    methodology: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    sample_count: Mapped[int] = mapped_column(Integer, nullable=False)


class CompilerImpactSample(Base):
    __tablename__ = "compiler_impact_sample"
    __table_args__ = (
        UniqueConstraint(
            "experiment_id",
            "pair_ordinal",
            "representation_kind",
            name="uq_compiler_impact_pair_arm",
        ),
        UniqueConstraint("run_id", name="uq_compiler_impact_sample_run"),
        UniqueConstraint(
            "experiment_id", "execution_ordinal", name="uq_compiler_impact_execution_ordinal"
        ),
        ForeignKeyConstraint(
            ["experiment_id", "merchant_id"],
            ["compiler_impact_experiment.id", "compiler_impact_experiment.merchant_id"],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["source_snapshot_id", "merchant_id"],
            ["merchant_source_snapshot.id", "merchant_source_snapshot.merchant_id"],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["representation_id", "merchant_id"],
            ["commerce_representation.id", "commerce_representation.merchant_id"],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["run_id", "merchant_id"],
            ["benchmark_run.id", "benchmark_run.merchant_id"],
            ondelete="RESTRICT",
        ),
        CheckConstraint("pair_ordinal > 0", name="pair_ordinal_positive"),
        CheckConstraint("execution_ordinal > 0", name="execution_ordinal_positive"),
        CheckConstraint(
            "representation_kind IN ('RAW', 'COMPILED')",
            name="representation_kind_known",
        ),
        CheckConstraint(
            "(representation_kind = 'RAW' AND source_snapshot_id IS NOT NULL"
            " AND representation_id IS NULL) OR (representation_kind = 'COMPILED'"
            " AND source_snapshot_id IS NULL AND representation_id IS NOT NULL)",
            name="representation_identity_shape",
        ),
        Index(None, "experiment_id", "execution_ordinal"),
        Index(None, "merchant_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid7)
    experiment_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    merchant_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    pair_ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    execution_ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    representation_kind: Mapped[RepresentationKind] = mapped_column(
        REPRESENTATION_KIND, nullable=False
    )
    source_snapshot_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    representation_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    run_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)


@dataclass(frozen=True, slots=True)
class ExperimentTreatment:
    experiment: CompilerImpactExperiment
    sample: CompilerImpactSample
    projection: dict[str, Any]
    representation: CommerceRepresentation | None


class CompilerImpactExperimentService:
    """Create immutable paired studies and resolve their next strictly controlled sample."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def _generated_catalog(self, environment_id: uuid.UUID) -> dict[str, Any] | None:
        """The stored world of the workspace that generated this environment, if one did."""
        return await self._session.scalar(
            select(MerchantEvaluationWorkspace.catalog_fixture).where(
                MerchantEvaluationWorkspace.environment_id == environment_id
            )
        )

    async def create(
        self,
        *,
        merchant_id: uuid.UUID,
        suite_id: uuid.UUID,
        environment: BenchmarkEnvironment,
        source_snapshot_id: uuid.UUID,
        compiled_representation_id: uuid.UUID,
        buyer_configuration: dict[str, Any],
        buyer_configuration_digest: str,
        sample_count: int,
        development_benchmark: bool = True,
    ) -> CompilerImpactExperiment:
        source = await self._source(merchant_id, source_snapshot_id)
        merchant = await self._merchant(merchant_id)
        suite = await self._suite(suite_id)
        if suite.merchant_slug != merchant.slug:
            raise ValueError("compiler impact benchmark suite belongs to another merchant")
        if environment.merchant_id != merchant_id:
            raise ValueError("compiler impact benchmark environment belongs to another merchant")
        representation = await self._representation(merchant_id, compiled_representation_id)
        if representation.producer is not RepresentationProducer.COMPILER:
            raise ValueError("a compiler impact treatment must be compiler-produced Commerce IR")
        if representation.compiler_run_id is None or representation.source_snapshot_id != source.id:
            raise ValueError("compiled treatment must derive from the experiment source snapshot")
        compiler_run = await self._compiler_run(merchant_id, representation.compiler_run_id)
        if compiler_run.source_snapshot_id != source.id:
            raise ValueError(
                "compiled treatment compiler run must derive from the experiment source"
            )
        # Both arms are handed merchant information and both transact in one world, and until now
        # nothing checked that the two described the same shop. Every lineage rule above binds the
        # representation to the source snapshot and both to the merchant; none of them binds
        # either to the environment, so an environment prepared from one snapshot could be paired
        # with a source and a representation from another. Both arms would then be told a price
        # the shelf does not hold, both would break their own budgets, and the experiment would
        # report a compiler comparison of the drift.
        #
        # The same three facts the evaluation launch compares, for the same reason: a world is
        # authoritative for price, availability and which SKUs exist, and nothing else.
        # Only where a workspace generated the world. An operator-authored world's catalog is a
        # file rather than a row, and the environment records its identity rather than its
        # content, so there is nothing here to compare against; that is the same boundary the
        # evaluation launch draws for the same reason.
        generated = await self._generated_catalog(environment.id)
        drift = (
            ()
            if generated is None
            else contradictions(
                world_facts(generated), representation_facts(representation.payload)
            )
        )
        if drift:
            raise ValueError(
                "compiled treatment describes products this benchmark environment does not hold:"
                f" {'; '.join(drift[:MAX_REPORTED])}"
            )
        experiment = CompilerImpactExperiment(
            merchant_id=merchant_id,
            suite_id=suite_id,
            environment_id=environment.id,
            source_snapshot_id=source.id,
            compiled_representation_id=representation.id,
            buyer_configuration_digest=buyer_configuration_digest,
            buyer_configuration=buyer_configuration,
            methodology={
                "benchmark_designation": "DEVELOPMENT" if development_benchmark else "EVALUATION",
                "comparison": "paired alternating raw and compiled samples",
                # Frozen before any result exists. Odd pairs run raw first and even pairs run
                # compiled first, so a rate limited provider cannot systematically hand the
                # second arm of every pair its accumulated quota pressure. Historical
                # experiments declared no pair order and remain raw first for every pair.
                "pair_order": "counterbalanced",
                "primary_metrics": "existing deterministic benchmark metrics",
                "simulated_demand": True,
            },
            sample_count=sample_count,
        )
        self._session.add(experiment)
        await self._session.flush()
        for pair in range(1, sample_count + 1):
            arms: tuple[RepresentationKind, RepresentationKind] = (
                (RepresentationKind.RAW, RepresentationKind.COMPILED)
                if pair % 2 == 1
                else (RepresentationKind.COMPILED, RepresentationKind.RAW)
            )
            for offset, kind in enumerate(arms):
                if kind is RepresentationKind.RAW:
                    identity: dict[str, Any] = {"source_snapshot_id": source.id}
                else:
                    identity = {"representation_id": representation.id}
                self._session.add(
                    CompilerImpactSample(
                        experiment_id=experiment.id,
                        merchant_id=merchant_id,
                        pair_ordinal=pair,
                        execution_ordinal=(pair - 1) * 2 + offset + 1,
                        representation_kind=kind,
                        **identity,
                    )
                )
        await self._session.commit()
        return experiment

    async def get(
        self, merchant_id: uuid.UUID, experiment_id: uuid.UUID
    ) -> CompilerImpactExperiment:
        row = (
            await self._session.execute(
                select(CompilerImpactExperiment).where(
                    CompilerImpactExperiment.id == experiment_id,
                    CompilerImpactExperiment.merchant_id == merchant_id,
                )
            )
        ).scalar_one_or_none()
        if row is None:
            raise NotFoundError("compiler_impact_experiment", str(experiment_id))
        return row

    async def samples(
        self, merchant_id: uuid.UUID, experiment_id: uuid.UUID
    ) -> list[CompilerImpactSample]:
        await self.get(merchant_id, experiment_id)
        rows = await self._session.execute(
            select(CompilerImpactSample)
            .where(
                CompilerImpactSample.experiment_id == experiment_id,
                CompilerImpactSample.merchant_id == merchant_id,
            )
            .order_by(CompilerImpactSample.execution_ordinal)
        )
        return list(rows.scalars())

    async def next_treatment(
        self, merchant_id: uuid.UUID, experiment_id: uuid.UUID
    ) -> ExperimentTreatment:
        experiment = await self.get(merchant_id, experiment_id)
        sample = (
            await self._session.execute(
                select(CompilerImpactSample)
                .where(
                    CompilerImpactSample.experiment_id == experiment_id,
                    CompilerImpactSample.merchant_id == merchant_id,
                    CompilerImpactSample.run_id.is_(None),
                )
                .order_by(CompilerImpactSample.execution_ordinal)
                .limit(1)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if sample is None:
            raise ConflictError("compiler_impact_experiment_complete", str(experiment_id))
        self._validate_sample_identity(experiment, sample)
        if sample.representation_kind is RepresentationKind.RAW:
            source = await self._source(merchant_id, sample.source_snapshot_id)
            return ExperimentTreatment(experiment, sample, raw_projection(source), None)
        representation = await self._representation(merchant_id, sample.representation_id)
        return ExperimentTreatment(
            experiment, sample, compiled_projection(representation), representation
        )

    async def bind_run(self, treatment: ExperimentTreatment, run_id: uuid.UUID) -> None:
        sample = (
            await self._session.execute(
                select(CompilerImpactSample)
                .where(
                    CompilerImpactSample.id == treatment.sample.id,
                    CompilerImpactSample.experiment_id == treatment.experiment.id,
                    CompilerImpactSample.run_id.is_(None),
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if sample is None:
            raise ConflictError("compiler_impact_sample_already_started", str(treatment.sample.id))
        self._validate_sample_identity(treatment.experiment, sample)
        run = await self._run(treatment.experiment.merchant_id, run_id)
        self._validate_run_binding(treatment, run)
        await self._validate_existing_run_pins(treatment.experiment, run)
        sample.run_id = run_id
        await self._session.commit()

    async def _source(
        self, merchant_id: uuid.UUID, source_id: uuid.UUID | None
    ) -> MerchantSourceSnapshot:
        if source_id is None:
            raise ValueError("raw treatment is missing a source identity")
        row = (
            await self._session.execute(
                select(MerchantSourceSnapshot).where(
                    MerchantSourceSnapshot.id == source_id,
                    MerchantSourceSnapshot.merchant_id == merchant_id,
                )
            )
        ).scalar_one_or_none()
        if row is None:
            raise NotFoundError("merchant_source_snapshot", str(source_id))
        return row

    async def _representation(
        self, merchant_id: uuid.UUID, representation_id: uuid.UUID | None
    ) -> CommerceRepresentation:
        if representation_id is None:
            raise ValueError("compiled treatment is missing a Commerce IR identity")
        row = (
            await self._session.execute(
                select(CommerceRepresentation).where(
                    CommerceRepresentation.id == representation_id,
                    CommerceRepresentation.merchant_id == merchant_id,
                )
            )
        ).scalar_one_or_none()
        if row is None:
            raise NotFoundError("commerce_representation", str(representation_id))
        return row

    async def _compiler_run(self, merchant_id: uuid.UUID, run_id: uuid.UUID) -> CompilerRun:
        row = (
            await self._session.execute(
                select(CompilerRun).where(
                    CompilerRun.id == run_id, CompilerRun.merchant_id == merchant_id
                )
            )
        ).scalar_one_or_none()
        if row is None:
            raise ValueError("compiler treatment is missing its compiler run")
        return row

    async def _merchant(self, merchant_id: uuid.UUID) -> Merchant:
        row = (
            await self._session.execute(select(Merchant).where(Merchant.id == merchant_id))
        ).scalar_one_or_none()
        if row is None:
            raise NotFoundError("merchant", str(merchant_id))
        return row

    async def _suite(self, suite_id: uuid.UUID) -> BenchmarkSuite:
        row = (
            await self._session.execute(select(BenchmarkSuite).where(BenchmarkSuite.id == suite_id))
        ).scalar_one_or_none()
        if row is None:
            raise NotFoundError("benchmark_suite", str(suite_id))
        return row

    async def _run(self, merchant_id: uuid.UUID, run_id: uuid.UUID) -> BenchmarkRun:
        row = (
            await self._session.execute(
                select(BenchmarkRun).where(
                    BenchmarkRun.id == run_id, BenchmarkRun.merchant_id == merchant_id
                )
            )
        ).scalar_one_or_none()
        if row is None:
            raise NotFoundError("benchmark_run", str(run_id))
        return row

    @staticmethod
    def _validate_sample_identity(
        experiment: CompilerImpactExperiment, sample: CompilerImpactSample
    ) -> None:
        # The plan is read from the experiment's own frozen methodology, never inferred from
        # what happens to be in the table. Experiments created before pair ordering existed
        # declare nothing and are validated under the original raw first scheme, so history
        # keeps validating exactly as it was written.
        pair_order = experiment.methodology.get("pair_order", "raw_then_compiled")
        if pair_order not in {"raw_then_compiled", "counterbalanced"}:
            raise ValueError("compiler impact experiment declares an unknown pair order")
        if not 1 <= sample.pair_ordinal <= experiment.sample_count:
            raise ValueError("compiler impact sample pair is outside experiment plan")
        # Each slot of a pair has its own expected arm: the odd execution slot opens the pair.
        # Counterbalancing swaps which arm that is on even pairs; legacy experiments keep raw
        # opening every pair.
        first_slot = sample.execution_ordinal == sample.pair_ordinal * 2 - 1
        opens_compiled = pair_order == "counterbalanced" and sample.pair_ordinal % 2 == 0
        if first_slot is opens_compiled:
            expected_kind = RepresentationKind.COMPILED
        else:
            expected_kind = RepresentationKind.RAW
        if sample.representation_kind is not expected_kind or sample.execution_ordinal not in (
            sample.pair_ordinal * 2 - 1,
            sample.pair_ordinal * 2,
        ):
            raise ValueError("compiler impact sample order does not match experiment plan")
        if (
            sample.representation_kind is RepresentationKind.RAW
            and sample.source_snapshot_id != experiment.source_snapshot_id
        ):
            raise ValueError("compiler impact raw sample source does not match experiment")
        if (
            sample.representation_kind is RepresentationKind.COMPILED
            and sample.representation_id != experiment.compiled_representation_id
        ):
            raise ValueError("compiler impact compiled sample does not match experiment")

    @staticmethod
    def _validate_run_binding(treatment: ExperimentTreatment, run: BenchmarkRun) -> None:
        experiment, sample = treatment.experiment, treatment.sample
        if run.status is not BenchmarkRunStatus.RUNNING:
            raise ValueError("compiler impact sample requires a running benchmark run")
        if run.suite_id != experiment.suite_id:
            raise ValueError("compiler impact sample benchmark suite does not match experiment")
        if run.environment_id != experiment.environment_id:
            raise ValueError("compiler impact sample benchmark world does not match experiment")
        if run.agent_configuration != experiment.buyer_configuration:
            raise ValueError("compiler impact sample buyer configuration does not match experiment")
        if run.executor_revision != experiment.buyer_configuration_digest:
            raise ValueError("compiler impact sample executor does not match experiment")
        if (
            sample.representation_kind is RepresentationKind.RAW
            and run.representation_id is not None
        ):
            raise ValueError("raw sample cannot use a Commerce IR benchmark representation")
        if (
            sample.representation_kind is RepresentationKind.COMPILED
            and run.representation_id != sample.representation_id
        ):
            raise ValueError("compiled sample benchmark representation does not match experiment")

    async def _validate_existing_run_pins(
        self, experiment: CompilerImpactExperiment, run: BenchmarkRun
    ) -> None:
        if run.catalog_hash is None or run.evaluator_version is None:
            raise ValueError("compiler impact sample is missing benchmark identity pins")
        rows = await self._session.execute(
            select(BenchmarkRun.catalog_hash, BenchmarkRun.evaluator_version)
            .join(CompilerImpactSample, CompilerImpactSample.run_id == BenchmarkRun.id)
            .where(
                CompilerImpactSample.experiment_id == experiment.id,
                CompilerImpactSample.run_id.is_not(None),
            )
        )
        for catalog_hash, evaluator_version in rows:
            if (catalog_hash, evaluator_version) != (run.catalog_hash, run.evaluator_version):
                raise ValueError("compiler impact sample benchmark pins do not match experiment")
