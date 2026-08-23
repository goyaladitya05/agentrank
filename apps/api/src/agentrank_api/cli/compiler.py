"""Trusted operator workflow for deterministic merchant compilation and review."""

import argparse
import json
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import TextIO

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agentrank_api.cli.exits import ExitCode
from agentrank_api.cli.output import write_json
from agentrank_api.commerce.models import Merchant
from agentrank_api.commerce.repository import MerchantRepository
from agentrank_api.compiler.models import ReviewDecision
from agentrank_api.compiler.service import MerchantCompilerService, _proposal
from agentrank_api.config import Settings
from agentrank_api.errors import NotFoundError
from agentrank_api.payments.provider import PaymentProvider


def add_commands(parser: argparse.ArgumentParser) -> None:
    commands = parser.add_subparsers(dest="command_name", required=True)
    run = commands.add_parser("run", help="compile one immutable merchant source snapshot")
    run.add_argument("--merchant-slug", required=True)
    run.add_argument("--source-id", required=True, type=uuid.UUID)
    run.add_argument("--json", dest="as_json", action="store_true")
    run.set_defaults(command=run_compiler)
    show = commands.add_parser("show", help="show compiler candidates and review state")
    show.add_argument("--merchant-slug", required=True)
    show.add_argument("run_id", type=uuid.UUID)
    show.add_argument("--json", dest="as_json", action="store_true")
    show.set_defaults(command=show_compiler)
    review = commands.add_parser("review", help="accept, correct, or reject one candidate")
    review.add_argument("--merchant-slug", required=True)
    review.add_argument("candidate_id", type=uuid.UUID)
    review.add_argument("decision", choices=[item.value.lower() for item in ReviewDecision])
    review.add_argument("--correction", type=Path)
    review.add_argument("--json", dest="as_json", action="store_true")
    review.set_defaults(command=review_candidate)
    publish = commands.add_parser("publish", help="publish reviewed compiler Commerce IR")
    publish.add_argument("--merchant-slug", required=True)
    publish.add_argument("run_id", type=uuid.UUID)
    publish.add_argument("--json", dest="as_json", action="store_true")
    publish.set_defaults(command=publish_compiler)


async def run_compiler(
    session: AsyncSession,
    sessions: async_sessionmaker[AsyncSession],
    provider: PaymentProvider,
    arguments: argparse.Namespace,
    out: TextIO,
    settings: Settings,
) -> int:
    del sessions, provider, settings
    merchant = await _merchant(session, arguments.merchant_slug)
    run = await MerchantCompilerService(session).run(merchant.id, arguments.source_id)
    return _write(
        out,
        arguments.as_json,
        {
            "run_id": str(run.id),
            "status": run.status.value,
            "configuration_digest": run.configuration_digest,
        },
    )


async def show_compiler(
    session: AsyncSession,
    sessions: async_sessionmaker[AsyncSession],
    provider: PaymentProvider,
    arguments: argparse.Namespace,
    out: TextIO,
    settings: Settings,
) -> int:
    del sessions, provider, settings
    merchant = await _merchant(session, arguments.merchant_slug)
    service = MerchantCompilerService(session)
    run = await service.get_run(merchant.id, arguments.run_id)
    candidates = await service.candidates(merchant.id, run.id)
    payload = {
        "run_id": str(run.id),
        "status": run.status.value,
        "candidates": [
            {
                "id": str(candidate.id),
                "target": candidate.target,
                "state": candidate.state.value,
                "proposal": candidate.proposal,
            }
            for candidate in candidates
        ],
    }
    return _write(out, arguments.as_json, payload)


async def review_candidate(
    session: AsyncSession,
    sessions: async_sessionmaker[AsyncSession],
    provider: PaymentProvider,
    arguments: argparse.Namespace,
    out: TextIO,
    settings: Settings,
) -> int:
    del sessions, provider, settings
    merchant = await _merchant(session, arguments.merchant_slug)
    correction = None
    if arguments.correction is not None:
        correction = _proposal(json.loads(arguments.correction.read_text(encoding="utf-8")))
    decision = ReviewDecision(arguments.decision.upper())
    review = await MerchantCompilerService(session).review(
        merchant.id, arguments.candidate_id, decision, correction=correction
    )
    return _write(
        out,
        arguments.as_json,
        {
            "review_id": str(review.id),
            "decision": review.decision.value,
            "candidate_id": str(review.candidate_id),
        },
    )


async def publish_compiler(
    session: AsyncSession,
    sessions: async_sessionmaker[AsyncSession],
    provider: PaymentProvider,
    arguments: argparse.Namespace,
    out: TextIO,
    settings: Settings,
) -> int:
    del sessions, provider, settings
    merchant = await _merchant(session, arguments.merchant_slug)
    representation = await MerchantCompilerService(session).publish(merchant.id, arguments.run_id)
    return _write(
        out,
        arguments.as_json,
        {
            "representation_id": str(representation.id),
            "content_hash": representation.content_hash,
            "compiler_run_id": str(representation.compiler_run_id),
        },
    )


async def _merchant(session: AsyncSession, slug: str) -> Merchant:
    merchant = await MerchantRepository(session).get_by_slug(slug)
    if merchant is None:
        raise NotFoundError("merchant", slug)
    return merchant


def _write(out: TextIO, as_json: bool, payload: Mapping[str, object]) -> int:
    if as_json:
        write_json(out, payload)
    else:
        for key, value in payload.items():
            print(f"{key}  {value}", file=out)
    return ExitCode.OK
