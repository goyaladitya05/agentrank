"""Trusted operator commands for publishing and inspecting merchant representations."""

import argparse
from pathlib import Path
from typing import TextIO

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agentrank_api.cli.exits import ExitCode
from agentrank_api.cli.output import write_json
from agentrank_api.config import Settings
from agentrank_api.payments.provider import PaymentProvider
from agentrank_api.representation.fixtures import read_ir, read_source
from agentrank_api.representation.service import MerchantRepresentationService

DEFAULT_SOURCE = Path("benchmarks/voltedge/source.json")
DEFAULT_IR = Path("benchmarks/voltedge/commerce_ir.json")


def add_commands(parser: argparse.ArgumentParser) -> None:
    commands = parser.add_subparsers(dest="command_name", required=True)
    source = commands.add_parser(
        "publish-source", help="publish an immutable merchant source snapshot"
    )
    source.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    source.add_argument("--json", dest="as_json", action="store_true")
    source.set_defaults(command=publish_source)
    ir = commands.add_parser("publish-ir", help="publish a manually-authored Commerce IR fixture")
    ir.add_argument("--ir", type=Path, default=DEFAULT_IR)
    ir.add_argument("--json", dest="as_json", action="store_true")
    ir.set_defaults(command=publish_ir)


async def publish_source(
    session: AsyncSession,
    sessions: async_sessionmaker[AsyncSession],
    provider: PaymentProvider,
    arguments: argparse.Namespace,
    out: TextIO,
    settings: Settings,
) -> int:
    del sessions, provider, settings
    row = await MerchantRepresentationService(session).publish_source(read_source(arguments.source))
    payload = {"source": row.label, "source_id": str(row.id), "content_hash": row.content_hash}
    if arguments.as_json:
        write_json(out, payload)
    else:
        print(f"source      {payload['source']}", file=out)
        print(f"identity    {payload['source_id']}  {payload['content_hash']}", file=out)
    return ExitCode.OK


async def publish_ir(
    session: AsyncSession,
    sessions: async_sessionmaker[AsyncSession],
    provider: PaymentProvider,
    arguments: argparse.Namespace,
    out: TextIO,
    settings: Settings,
) -> int:
    del sessions, provider, settings
    row = await MerchantRepresentationService(session).publish_ir(read_ir(arguments.ir))
    payload = {
        "representation_id": str(row.id),
        "source_id": str(row.source_snapshot_id),
        "producer": row.producer.value,
        "producer_version": row.producer_version,
        "content_hash": row.content_hash,
    }
    if arguments.as_json:
        write_json(out, payload)
    else:
        print(f"representation  {payload['representation_id']}", file=out)
        print(f"source          {payload['source_id']}", file=out)
        print(f"producer        {payload['producer']} {payload['producer_version']}", file=out)
        print(f"content hash    {payload['content_hash']}", file=out)
    return ExitCode.OK
