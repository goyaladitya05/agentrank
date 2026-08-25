"""Product-facing read and write models for a merchant's evaluation setup.

Written out field by field from the service's frozen dataclasses, per this repository's rule
that adding a field to a domain type must never silently change an API response.

Three things are deliberately absent.

There is no generated catalog and no generated mission. What a merchant reads here is counts,
identities and composition; the world is a whole product catalog and rendering it on an overview
would be loading one to draw a table of numbers, and a mission carries an expected outcome that
no merchant-facing surface has any business publishing.

There is no cost estimate, for the same reason the evaluation preflight has none. Building a
workspace spends nothing at all, and the evaluation it makes possible has its own preflight where
what will be executed is stated and no currency figure is invented.

And there is nothing a browser can use to choose what gets built. The request carries the source
snapshot the merchant was shown, and the server checks it against their current one rather than
selecting from it; every other identity comes from the credential that authenticated the request.
"""

import uuid
from datetime import datetime
from typing import Self

from pydantic import BaseModel, ConfigDict

from agentrank_api.workspace.definitions import MissionFamily
from agentrank_api.workspace.projection import CatalogSummary
from agentrank_api.workspace.service import (
    PlannedWorkspace,
    WorkspacePreflight,
    WorkspaceSummary,
)


class WorkspaceBlockerView(BaseModel):
    """One reason an evaluation setup cannot be built right now, with a merchant sentence."""

    code: str
    message: str


class EvaluationCatalogView(BaseModel):
    """The shape of the isolated catalog a workspace evaluates, as counts.

    `purchasable_variants` is what a buyer could actually take away today, which is not the same
    as how many variants the merchant listed. A merchant whose whole catalog is out of stock
    reads the difference here rather than discovering it as a benchmark full of abstentions.
    """

    products: int
    variants: int
    purchasable_variants: int
    currencies: list[str]
    categories: list[str]

    @classmethod
    def from_domain(cls, summary: CatalogSummary) -> Self:
        return cls(
            products=summary.products,
            variants=summary.variants,
            purchasable_variants=summary.purchasable_variants,
            currencies=list(summary.currencies),
            categories=list(summary.categories),
        )


class MissionFamilyView(BaseModel):
    """How many missions of one shape a suite holds, and what each of them expects."""

    family: MissionFamily
    missions: int
    purchase_available: int
    no_acceptable_purchase: int


class UnsupportedFamilyView(BaseModel):
    """One mission shape this merchant's evidence did not support, and why not."""

    family: MissionFamily
    reason: str


class EvaluationWorkspaceView(BaseModel):
    """One built evaluation setup: what it was built from, and what it holds."""

    model_config = ConfigDict(frozen=True)

    workspace_id: uuid.UUID
    created_at: datetime
    source_snapshot_id: uuid.UUID
    source_snapshot_label: str
    environment_id: uuid.UUID
    environment_label: str
    suite_id: uuid.UUID
    suite_label: str
    mission_count: int
    catalog: EvaluationCatalogView
    composition: list[MissionFamilyView]
    unsupported: list[UnsupportedFamilyView]
    generator_version: str
    configuration_digest: str
    catalog_hash: str
    suite_hash: str

    @classmethod
    def from_domain(cls, summary: WorkspaceSummary) -> Self:
        return cls(
            workspace_id=summary.workspace_id,
            created_at=summary.created_at,
            source_snapshot_id=summary.source_snapshot_id,
            source_snapshot_label=summary.source_snapshot_label,
            environment_id=summary.environment_id,
            environment_label=summary.environment_label,
            suite_id=summary.suite_id,
            suite_label=summary.suite_label,
            mission_count=summary.mission_count,
            catalog=EvaluationCatalogView.from_domain(summary.catalog),
            composition=[_family(entry) for entry in summary.composition],
            unsupported=[_unsupported(entry) for entry in summary.unsupported],
            generator_version=summary.generator_version,
            configuration_digest=summary.configuration_digest,
            catalog_hash=summary.catalog_hash,
            suite_hash=summary.suite_hash,
        )


class PlannedWorkspaceView(BaseModel):
    """What building a setup right now would produce.

    Computed by running the generator rather than by describing it, so what a merchant reads
    before pressing the button is what they get. `omitted_fields` names the source fields the
    evaluation catalog does not carry, so a merchant whose metadata holds something this
    projection cannot compare can see that rather than wonder where it went.
    """

    mission_count: int
    catalog: EvaluationCatalogView
    composition: list[MissionFamilyView]
    unsupported: list[UnsupportedFamilyView]
    omitted_fields: list[str]
    mission_budget: int

    @classmethod
    def from_domain(cls, planned: PlannedWorkspace) -> Self:
        return cls(
            mission_count=planned.mission_count,
            catalog=EvaluationCatalogView.from_domain(planned.catalog),
            composition=[_family(entry) for entry in planned.composition],
            unsupported=[_unsupported(entry) for entry in planned.unsupported],
            omitted_fields=list(planned.omitted_fields),
            mission_budget=planned.configuration.mission_budget,
        )


class EvaluationSetupView(BaseModel):
    """Where this merchant's evaluation setup has got to, and what would happen next.

    `source_is_newer_than_the_workspace` is a fact rather than an instruction. A newer source
    snapshot never rebuilds a world and never invalidates a run measured against the old one;
    what it means is that the merchant may now build a second setup, and this is how they are
    told that rather than being surprised by it.
    """

    buildable: bool
    current_source_snapshot_id: uuid.UUID | None
    current_source_snapshot_label: str | None
    source_is_newer_than_the_workspace: bool
    workspace: EvaluationWorkspaceView | None
    planned: PlannedWorkspaceView | None
    blockers: list[WorkspaceBlockerView]

    @classmethod
    def from_domain(cls, preflight: WorkspacePreflight) -> Self:
        return cls(
            buildable=preflight.buildable,
            current_source_snapshot_id=preflight.current_source_snapshot_id,
            current_source_snapshot_label=preflight.current_source_snapshot_label,
            source_is_newer_than_the_workspace=preflight.source_is_newer_than_the_workspace,
            workspace=None
            if preflight.current is None
            else EvaluationWorkspaceView.from_domain(preflight.current),
            planned=None
            if preflight.planned is None
            else PlannedWorkspaceView.from_domain(preflight.planned),
            blockers=[
                WorkspaceBlockerView(code=blocker.code, message=blocker.message)
                for blocker in preflight.blockers
            ],
        )


class WorkspaceBuildRequest(BaseModel):
    """The merchant's command to build their evaluation setup.

    One field, and it selects nothing. `source_snapshot_id` is the evidence the merchant was
    shown, and the server compares it with their own current snapshot rather than reading it: a
    browser that could name a snapshot could build a world from evidence that has been
    superseded, and a page rendered before a source refresh would do exactly that.

    There is no request key beside it, and that is not an omission. A workspace is identified by
    the merchant, the snapshot and the generation configuration, so a repeat of this command is
    already the same command; a key would be a second idempotency mechanism for a rule the
    schema already holds.

    `extra="forbid"` so a body carrying a merchant, a mission budget or a suite is refused rather
    than ignored. A field this schema does not have is a field a caller believed would take
    effect, and silently dropping one is how a request comes to mean something the caller did
    not intend.
    """

    model_config = ConfigDict(extra="forbid")

    source_snapshot_id: uuid.UUID


class WorkspaceBuildView(BaseModel):
    """What one build command did.

    `created` is false when the command resolved to a setup that already existed, which is what a
    retry after a lost response produces. Reported rather than hidden, so a caller can tell "this
    built your setup" from "this was already your setup".
    """

    created: bool
    workspace: EvaluationWorkspaceView


def _family(entry: object) -> MissionFamilyView:
    return MissionFamilyView.model_validate(entry, from_attributes=True)


def _unsupported(entry: object) -> UnsupportedFamilyView:
    return UnsupportedFamilyView.model_validate(entry, from_attributes=True)
