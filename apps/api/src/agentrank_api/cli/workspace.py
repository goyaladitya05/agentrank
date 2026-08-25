"""The operator commands that turn one merchant's source evidence into an evaluation setup.

Three commands and none of them spends anything:

```text
show        what this merchant's setup is, what a bootstrap would build, what stops it
bootstrap   build the evaluation world and workload from their current source snapshot
history     every workspace this merchant has had, newest first
```

`bootstrap` writes three rows and calls no model. It registers the merchant as a benchmark
world, publishes the generated workload, and records that both came from one source snapshot
under one configuration. It writes no product row, no price and no stock level, so unlike
`benchmark seed` it cannot overwrite a catalog: the world is materialized later, by the
preparation a benchmark run already performs.

Running it twice is one workspace. The command is safe to repeat, safe to retry after a lost
connection and safe to run from two shells at once, because the service resolves all three to the
same row rather than building a second world.

Nothing here is a second product surface. A merchant reaches the same service from the console,
and this exists because a private beta operator sometimes needs to build a setup on a merchant's
behalf and to read exactly what was built. What it can do is what the merchant can do, apart from
the mission budget, which is an operator choice, is stated in the output, and is part of the
workspace's identity rather than a display option.
"""

import argparse
from typing import Any, TextIO

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agentrank_api.cli.exits import ExitCode
from agentrank_api.cli.output import write_json
from agentrank_api.commerce.models import Merchant
from agentrank_api.commerce.repository import MerchantRepository
from agentrank_api.config import Settings
from agentrank_api.errors import ConflictError, NotFoundError
from agentrank_api.payments.provider import PaymentProvider
from agentrank_api.workspace.definitions import (
    DEFAULT_MISSION_BUDGET,
    MAX_MISSION_BUDGET,
    MIN_MISSION_BUDGET,
    BootstrapConfiguration,
)
from agentrank_api.workspace.service import (
    MerchantEvaluationWorkspaceService,
    WorkspacePreflight,
    WorkspaceSummary,
)

MISSING = "none"


def add_commands(parser: argparse.ArgumentParser) -> None:
    """Declare the evaluation workspace command surface."""
    commands = parser.add_subparsers(dest="command_name", required=True)

    reading = commands.add_parser(
        "show",
        help="this merchant's evaluation setup, and what a bootstrap would build",
        description=(
            "Read what AgentRank would evaluate this merchant against. Nothing is written and no"
            " model is called. The planned section is produced by running the generator, so the"
            " mission count and composition shown are the ones a bootstrap would create."
        ),
    )
    _add_merchant(reading)
    _add_budget(reading)
    _add_json(reading)
    reading.set_defaults(command=show)

    building = commands.add_parser(
        "bootstrap",
        help="build the evaluation world and workload from this merchant's current source",
        description=(
            "Turn this merchant's current source snapshot into the isolated evaluation catalog"
            " and benchmark suite a first evaluation needs. Deterministic, and no model is"
            " called. Running it twice produces one workspace rather than two, and it writes no"
            " product, price or stock row: the world is put in place by the preparation a"
            " benchmark run already performs."
        ),
    )
    _add_merchant(building)
    _add_budget(building)
    _add_json(building)
    building.set_defaults(command=bootstrap)

    listing = commands.add_parser(
        "history",
        help="every evaluation workspace this merchant has had, newest first",
        description=(
            "Read this merchant's workspaces. Each one is immutable and stays pointed at the"
            " source snapshot it was built from, so a run measured against an older one remains"
            " interpretable after a newer one exists."
        ),
    )
    _add_merchant(listing)
    listing.add_argument("--limit", type=int, default=10)
    _add_json(listing)
    listing.set_defaults(command=history)


def _add_merchant(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--merchant-slug", required=True)


def _add_budget(parser: argparse.ArgumentParser) -> None:
    """How many missions a generated suite may hold.

    Part of the workspace identity rather than a display option, so a different value is a
    different workspace and never a rewrite of an existing one. Bounded here as well as in the
    domain, so that a wrong number is a usage error rather than a refusal after a database round
    trip.
    """
    parser.add_argument(
        "--missions",
        type=int,
        default=DEFAULT_MISSION_BUDGET,
        choices=range(MIN_MISSION_BUDGET, MAX_MISSION_BUDGET + 1),
        metavar=f"{{{MIN_MISSION_BUDGET}..{MAX_MISSION_BUDGET}}}",
        help=f"how many missions the generated suite may hold (default {DEFAULT_MISSION_BUDGET})",
    )


def _add_json(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--json",
        dest="as_json",
        action="store_true",
        help="print one JSON document instead of a table",
    )


async def show(
    session: AsyncSession,
    sessions: async_sessionmaker[AsyncSession],
    provider: PaymentProvider,
    arguments: argparse.Namespace,
    out: TextIO,
    settings: Settings,
) -> int:
    """Read the setup and the plan.

    Exits REFUSED when nothing could be built right now, so an operator loop can tell "this
    merchant is ready" from "this merchant is blocked" without parsing prose. A merchant who is
    already set up and has no newer evidence is ready by that measure and exits OK.
    """
    del sessions, provider, settings
    merchant = await _merchant(session, arguments.merchant_slug)
    preflight = await MerchantEvaluationWorkspaceService(session).preflight(
        merchant.id, configuration=BootstrapConfiguration(mission_budget=arguments.missions)
    )
    if arguments.as_json:
        write_json(out, _preflight_payload(preflight))
    else:
        _render_preflight(out, preflight)
    settled = preflight.buildable or preflight.current is not None
    return ExitCode.OK if settled else ExitCode.REFUSED


async def bootstrap(
    session: AsyncSession,
    sessions: async_sessionmaker[AsyncSession],
    provider: PaymentProvider,
    arguments: argparse.Namespace,
    out: TextIO,
    settings: Settings,
) -> int:
    """Build the workspace, or report the one this command already produced.

    The snapshot is resolved here and checked again inside the service under its lock. This read
    is what lets an operator run the command without naming an identifier; the check is what
    stops the command building a world from evidence that was superseded between the two.
    """
    del sessions, provider, settings
    merchant = await _merchant(session, arguments.merchant_slug)
    service = MerchantEvaluationWorkspaceService(session)
    snapshot = await service.current_source_snapshot_id(merchant.id)
    if snapshot is None:
        raise ConflictError(
            "merchant_source_unavailable",
            "this merchant has published no source snapshot to build an evaluation setup from",
            resource="merchant_evaluation_workspace",
            identifier=arguments.merchant_slug,
        )
    outcome = await service.bootstrap(
        merchant.id,
        source_snapshot_id=snapshot,
        configuration=BootstrapConfiguration(mission_budget=arguments.missions),
    )
    summary = await service.summary_of(merchant.id, outcome.workspace.id)
    if arguments.as_json:
        write_json(out, {"created": outcome.created, **_summary_payload(summary)})
        return ExitCode.OK

    built = "yes" if outcome.created else "no, this setup already existed"
    print(f"created     {built}", file=out)
    _render_summary(out, summary)
    return ExitCode.OK


async def history(
    session: AsyncSession,
    sessions: async_sessionmaker[AsyncSession],
    provider: PaymentProvider,
    arguments: argparse.Namespace,
    out: TextIO,
    settings: Settings,
) -> int:
    """Every workspace this merchant has had, newest first."""
    del sessions, provider, settings
    merchant = await _merchant(session, arguments.merchant_slug)
    summaries = await MerchantEvaluationWorkspaceService(session).history(
        merchant.id, limit=max(1, arguments.limit)
    )
    if arguments.as_json:
        write_json(out, {"workspaces": [_summary_payload(entry) for entry in summaries]})
        return ExitCode.OK

    if not summaries:
        print("workspaces  none built for this merchant", file=out)
        return ExitCode.OK
    for entry in summaries:
        print(
            f"{entry.workspace_id}  {entry.source_snapshot_label:<24}"
            f"  {entry.suite_label:<34}  {entry.mission_count:>3} missions",
            file=out,
        )
    return ExitCode.OK


def _render_preflight(out: TextIO, preflight: WorkspacePreflight) -> None:
    print(f"source      {preflight.current_source_snapshot_label or MISSING}", file=out)
    if preflight.current is None:
        print("workspace   none built for this merchant", file=out)
    else:
        _render_summary(out, preflight.current)
        if preflight.source_is_newer_than_the_workspace:
            print(
                "newer       this merchant has published source evidence their current"
                " evaluation setup was not built from",
                file=out,
            )
    if preflight.planned is not None:
        planned = preflight.planned
        print(
            f"planned     {planned.mission_count} missions from {planned.catalog.products}"
            f" products, {planned.catalog.purchasable_variants} of {planned.catalog.variants}"
            " variants in stock",
            file=out,
        )
        for family in planned.composition:
            print(
                f"  family    {family.family.value:<30} {family.missions} missions"
                f" ({family.purchase_available} purchasable,"
                f" {family.no_acceptable_purchase} correct abstention)",
                file=out,
            )
        for missing in planned.unsupported:
            print(f"  omitted   {missing.family.value:<30} {missing.reason}", file=out)
        for field in planned.omitted_fields:
            print(f"  dropped   {field}", file=out)
    for blocker in preflight.blockers:
        print(f"blocked     {blocker.code}: {blocker.message}", file=out)


def _render_summary(out: TextIO, summary: WorkspaceSummary) -> None:
    print(f"workspace   {summary.workspace_id}  from {summary.source_snapshot_label}", file=out)
    print(f"world       {summary.environment_label}  {summary.catalog_hash}", file=out)
    print(f"suite       {summary.suite_label}  {summary.suite_hash}", file=out)
    print(
        f"catalog     {summary.catalog.products} products,"
        f" {summary.catalog.purchasable_variants} of {summary.catalog.variants} variants in"
        f" stock, {', '.join(summary.catalog.currencies) or MISSING}",
        file=out,
    )
    print(f"missions    {summary.mission_count}", file=out)
    print(f"generator   {summary.generator_version}  {summary.configuration_digest}", file=out)


def _preflight_payload(preflight: WorkspacePreflight) -> dict[str, Any]:
    planned = preflight.planned
    return {
        "current_source_snapshot_id": _text(preflight.current_source_snapshot_id),
        "current_source_snapshot_label": preflight.current_source_snapshot_label,
        "workspace": None if preflight.current is None else _summary_payload(preflight.current),
        "source_is_newer_than_the_workspace": preflight.source_is_newer_than_the_workspace,
        "buildable": preflight.buildable,
        "planned": None
        if planned is None
        else {
            "mission_count": planned.mission_count,
            "catalog": planned.catalog.to_payload(),
            "composition": [entry.to_payload() for entry in planned.composition],
            "unsupported": [entry.to_payload() for entry in planned.unsupported],
            "omitted_fields": list(planned.omitted_fields),
            "configuration": planned.configuration.to_payload(),
        },
        "blockers": [
            {"code": blocker.code, "message": blocker.message} for blocker in preflight.blockers
        ],
    }


def _summary_payload(summary: WorkspaceSummary) -> dict[str, Any]:
    return {
        "workspace_id": str(summary.workspace_id),
        "created_at": summary.created_at.isoformat(),
        "source_snapshot_id": str(summary.source_snapshot_id),
        "source_snapshot_label": summary.source_snapshot_label,
        "environment_id": str(summary.environment_id),
        "environment_label": summary.environment_label,
        "suite_id": str(summary.suite_id),
        "suite_label": summary.suite_label,
        "mission_count": summary.mission_count,
        "catalog": summary.catalog.to_payload(),
        "composition": [entry.to_payload() for entry in summary.composition],
        "unsupported": [entry.to_payload() for entry in summary.unsupported],
        "generator_version": summary.generator_version,
        "configuration_digest": summary.configuration_digest,
        "catalog_hash": summary.catalog_hash,
        "suite_hash": summary.suite_hash,
    }


def _text(value: object) -> str | None:
    return None if value is None else str(value)


async def _merchant(session: AsyncSession, slug: str) -> Merchant:
    merchant = await MerchantRepository(session).get_by_slug(slug)
    if merchant is None:
        raise NotFoundError("merchant", slug)
    return merchant
