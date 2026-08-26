"""The lineage row that says which world and which workload one merchant's evidence produced.

Every other artifact this phase creates already had a table. A registered benchmark world is a
`benchmark_environment` and a published workload is a `benchmark_suite`, and neither of them is
being duplicated here: this row points at both and adds only what neither can hold.

```text
benchmark_environment    which merchant is a benchmark world, under which fixture identity
benchmark_suite          one immutable workload, as a global template with no merchant
merchant_source_snapshot what the merchant said about themselves
this row                 that those three are one workspace, built by one generator under one
                         configuration, in that order
```

Without it the lineage is guesswork. A benchmark environment records a fixture key and a digest
and has no idea which source snapshot the fixture was projected from. A benchmark suite is a
global template with no merchant column at all. Resolving a merchant's world by "the most
recently registered one" and their workload by "the newest suite whose authored slug matches"
is what the evaluation preflight had to do before this existed, and it is a convention rather
than a fact the database can prove.

Four properties are the database's rather than the application's.

A workspace is immutable. It is written once and refused for UPDATE and DELETE by a trigger, for
the same reason its environment and its suite are: a benchmark run points at the world and the
workload this row names, and a report read a year later has to mean what it meant on the day.

One workspace per merchant, source snapshot and configuration digest, through a unique
constraint. That is the whole of idempotency: a repeated bootstrap, a retry after a lost
response and two browsers submitting at once all resolve to one row rather than to three
identical worlds. A different configuration is a different digest and therefore a different
workspace, which is the other half of the same rule.

An environment and a suite belong to at most one workspace, through two more unique constraints.
A world generated for one merchant's snapshot cannot be claimed by a second workspace, so
"which evidence produced this world" has exactly one answer.

And `write_order` decides which workspace is current, exactly as it decides which source
snapshot is. It is `GENERATED ALWAYS AS IDENTITY`, so nothing in this application can supply,
override or backdate a workspace's place in history, and two processes inserting in the same
millisecond are ordered by PostgreSQL rather than by a random draw inside a version 7 UUID.

`catalog_fixture` is the generated world itself, and it is here because there is nowhere else
for it. An authored world lives in `benchmarks/<world>/catalog.json` and is read by an operator
command with a database credential; a generated one has no file, and a benchmark run has to be
able to put the merchant's catalog back to exactly what this workspace describes. Its digest is
the `fixture_hash` on the environment row, so a payload edited around this application is
refused by the registration check rather than silently prepared.

What is deliberately not here is anything a mission oracle could be read out of. The workload
lives in `benchmark_mission` exactly as an authored one does, and the isolated buyer process has
no database credential and no route to either.
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    String,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from agentrank_api.benchmark.definitions import KEY_PATTERN, MAX_KEY_LENGTH
from agentrank_api.benchmark.identity import HASH_LENGTH, HASH_PATTERN
from agentrank_api.models import Base
from agentrank_api.representation.models import write_order_column


class MerchantEvaluationWorkspace(Base):
    """One merchant's evaluation setup, as the artifacts it is made of and nothing more."""

    __tablename__ = "merchant_evaluation_workspace"
    __table_args__ = (
        # The idempotency key. One merchant, one snapshot and one configuration are one
        # workspace, however many times somebody asks for it.
        UniqueConstraint(
            "merchant_id",
            "source_snapshot_id",
            "configuration_digest",
            name="uq_merchant_evaluation_workspace_identity",
        ),
        # A generated world and a generated workload belong to one workspace. Without these,
        # two workspaces could name one environment and "which evidence produced this world"
        # would have two answers.
        UniqueConstraint("environment_id", name="uq_merchant_evaluation_workspace_environment"),
        UniqueConstraint("suite_id", name="uq_merchant_evaluation_workspace_suite"),
        ForeignKeyConstraint(["merchant_id"], ["merchant.id"], ondelete="RESTRICT"),
        # Each artifact is tied to this merchant as well as named, so a workspace cannot point
        # at another merchant's snapshot or another merchant's world.
        ForeignKeyConstraint(
            ["source_snapshot_id", "merchant_id"],
            ["merchant_source_snapshot.id", "merchant_source_snapshot.merchant_id"],
            name="fk_merchant_evaluation_workspace_source",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["environment_id", "merchant_id"],
            ["benchmark_environment.id", "benchmark_environment.merchant_id"],
            name="fk_merchant_evaluation_workspace_environment",
            ondelete="RESTRICT",
        ),
        CheckConstraint(f"generator_version ~ '{KEY_PATTERN}'", name="generator_format"),
        CheckConstraint(
            f"configuration_digest ~ '{HASH_PATTERN}'", name="configuration_digest_format"
        ),
        CheckConstraint(f"catalog_hash ~ '{HASH_PATTERN}'", name="catalog_hash_format"),
        CheckConstraint(f"suite_hash ~ '{HASH_PATTERN}'", name="suite_hash_format"),
        CheckConstraint("jsonb_typeof(catalog_fixture) = 'object'", name="catalog_fixture_object"),
        CheckConstraint("jsonb_typeof(composition) = 'object'", name="composition_object"),
        CheckConstraint(
            "configuration IS NULL OR jsonb_typeof(configuration) = 'object'",
            name="configuration_object",
        ),
        CheckConstraint(
            "stock_assumption IS NULL OR jsonb_typeof(stock_assumption) = 'object'",
            name="stock_assumption_object",
        ),
        Index(None, "merchant_id"),
        Index(None, "source_snapshot_id", "merchant_id"),
        Index(None, "suite_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid7)
    write_order: Mapped[int] = write_order_column()
    merchant_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    source_snapshot_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    environment_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    # RESTRICT and no composite target, because a benchmark suite is a global template with no
    # merchant of its own. Which merchant it was generated for is this row's `merchant_id`, and
    # the suite's own `merchant_slug` records who it was authored against, exactly as an
    # authored suite's does.
    suite_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("benchmark_suite.id", ondelete="RESTRICT"), nullable=False
    )
    generator_version: Mapped[str] = mapped_column(String(MAX_KEY_LENGTH), nullable=False)
    configuration_digest: Mapped[str] = mapped_column(String(HASH_LENGTH), nullable=False)
    # The two content identities this workspace is reproducible against, copied here so that a
    # reader can compare them without loading the environment row and every mission.
    catalog_hash: Mapped[str] = mapped_column(String(HASH_LENGTH), nullable=False)
    suite_hash: Mapped[str] = mapped_column(String(HASH_LENGTH), nullable=False)
    catalog_fixture: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    composition: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    # The frozen bootstrap choices as a document rather than as the digest beside them, and which
    # of this world's lines hold a depth AgentRank supplied rather than one the merchant stated.
    # Nullable because a workspace built before this build recorded them says nothing about them,
    # and a row saying nothing is honest where a row backfilled with a guess would not be.
    configuration: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    stock_assumption: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
