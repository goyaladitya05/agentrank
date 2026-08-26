"""The operator's backup and restore procedure, executed rather than described.

PostgreSQL is the only place any of this product's evidence exists. Source snapshots, compiler
runs, reviews, published representations, benchmark runs, mission traces and launch history are
all immutable and none of them is reconstructible from anywhere else. There is no backup service
and there is deliberately not going to be one: what there is, is `pg_dump` and `pg_restore` run
by an operator, and the only thing that makes that a procedure rather than a hope is that it has
been run end to end against a populated database.

So this runs it. A merchant is built with real evidence through the real services, the repository
scripts are executed as an operator would execute them, the backup is restored into a database
that has never seen any of it, and the evidence is read back out and compared.

What this claims is exactly what it does: a dump taken with these scripts restores, on this
PostgreSQL version, into an empty database, with the evidence and the schema revision intact.
It claims nothing about replication, point in time recovery or a disk that failed mid-write.
"""

import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest
from conftest import administer
from sqlalchemy import create_engine, text
from sqlalchemy.ext.asyncio import AsyncSession
from workspace_support import catalogued

from agentrank_api.commerce.repository import MerchantRepository
from agentrank_api.compiler.service import MerchantCompilerService
from agentrank_api.config import Settings
from agentrank_api.representation.service import MerchantRepresentationService
from agentrank_api.schema import EXPECTED_REVISION
from agentrank_api.workspace.service import MerchantEvaluationWorkspaceService

pytestmark = pytest.mark.anyio

REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
BACKUP = REPOSITORY_ROOT / "scripts" / "backup.sh"
RESTORE = REPOSITORY_ROOT / "scripts" / "restore.sh"

RESTORE_DATABASE = "agentrank_restore_test"


# Where the operator's tools live, which is not always the machine the tests run on. A deployment
# runs `pg_dump` from a host that has the PostgreSQL client installed; a development machine runs
# PostgreSQL in Docker and frequently has no client at all. Both are supported here, and it is the
# same script executed either way: `bash -s` reads it from this repository over standard input, so
# nothing about the procedure changes to accommodate the container.
HOST_CLIENT = shutil.which("pg_dump") is not None

# Resolved once rather than looked up per call, and as a full path so that what runs is decided
# by this process' own PATH at import time rather than by whatever a subprocess inherits.
DOCKER = shutil.which("docker") or "docker"

# Inside the container the server is on loopback whatever the host reaches it by, and the dump
# lives in the container's own temporary directory because that is where the tool writing it is.
CONTAINER_DUMP = "/tmp/agentrank-backup-test.dump"  # noqa: S108  a path inside a throwaway container


def environment(settings: Settings) -> dict[str, str]:
    """The connection variables the scripts read, which are the deployment's own."""
    return {
        "PATH": "/usr/bin:/bin:/usr/local/bin",
        "POSTGRES_HOST": settings.postgres_host,
        "POSTGRES_PORT": str(settings.postgres_port),
        "POSTGRES_DB": settings.postgres_db,
        "POSTGRES_USER": settings.postgres_user,
        "POSTGRES_PASSWORD": settings.postgres_password.get_secret_value(),
    }


def run(script: Path, *arguments: str, settings: Settings) -> subprocess.CompletedProcess[str]:
    """One repository script, run where the PostgreSQL client actually is."""
    if HOST_CLIENT:
        return subprocess.run(  # noqa: S603  this repository's own script
            [str(script), *arguments],
            capture_output=True,
            text=True,
            env=environment(settings),
            check=False,
            timeout=180,
        )
    inside = environment(settings) | {"POSTGRES_HOST": "localhost", "POSTGRES_PORT": "5432"}
    exported = [item for name, value in inside.items() for item in ("--env", f"{name}={value}")]
    return subprocess.run(  # noqa: S603  the compose service this suite already runs against
        [DOCKER, "compose", "exec", "-T", *exported, "postgres", "bash", "-s", "--", *arguments],
        input=script.read_text(encoding="utf-8"),
        capture_output=True,
        text=True,
        cwd=REPOSITORY_ROOT,
        check=False,
        timeout=180,
    )


def bytes_written(path: str) -> int:
    """The size of a file the backup wrote, wherever the backup ran."""
    if HOST_CLIENT:
        return Path(path).stat().st_size
    measured = subprocess.run(  # noqa: S603  the compose service this suite already runs against
        [DOCKER, "compose", "exec", "-T", "postgres", "stat", "-c", "%s", path],
        capture_output=True,
        text=True,
        cwd=REPOSITORY_ROOT,
        check=False,
        timeout=60,
    )
    return int(measured.stdout.strip()) if measured.returncode == 0 else 0


def discard(path: str) -> None:
    if HOST_CLIENT:
        Path(path).unlink(missing_ok=True)
        return
    subprocess.run(  # noqa: S603  the compose service this suite already runs against
        [DOCKER, "compose", "exec", "-T", "postgres", "rm", "-f", path],
        capture_output=True,
        cwd=REPOSITORY_ROOT,
        check=False,
        timeout=60,
    )


@pytest.fixture
def dump_path(tmp_path: Path) -> Iterator[str]:
    """A path the backup may write to, on whichever filesystem the script will run against."""
    path = str(tmp_path / "evidence.dump") if HOST_CLIENT else CONTAINER_DUMP
    discard(path)
    try:
        yield path
    finally:
        discard(path)


@pytest.fixture
def restored(settings: Settings) -> Iterator[Settings]:
    """An empty database to restore into, dropped afterwards whatever happens."""
    administer(settings, f'DROP DATABASE IF EXISTS "{RESTORE_DATABASE}" WITH (FORCE)')
    administer(settings, f'CREATE DATABASE "{RESTORE_DATABASE}"')
    try:
        yield settings.model_copy(update={"postgres_db": RESTORE_DATABASE})
    finally:
        administer(settings, f'DROP DATABASE IF EXISTS "{RESTORE_DATABASE}" WITH (FORCE)')


async def test_a_backup_restores_a_merchants_evidence_into_an_empty_database(
    catalog_settings: Settings,
    session: AsyncSession,
    restored: Settings,
    dump_path: str,
) -> None:
    """Real evidence out, the same evidence back, through the scripts an operator runs."""
    merchant = await MerchantRepository(session).create(slug="backup-shop", name="Backup Shop")
    await session.commit()
    snapshot = await MerchantRepresentationService(session).publish_source(
        catalogued("backup-shop")
    )
    compiler = MerchantCompilerService(session)
    compiler_run = await compiler.run(merchant.id, snapshot.id)
    representation = await compiler.publish(merchant.id, compiler_run.id)
    workspace = (
        await MerchantEvaluationWorkspaceService(session).bootstrap(
            merchant.id, source_snapshot_id=snapshot.id
        )
    ).workspace
    expected = {
        "merchant": merchant.id,
        "snapshot": snapshot.id,
        "hash": snapshot.content_hash,
        "representation": representation.id,
        "workspace": workspace.id,
        "suite": workspace.suite_id,
    }

    taken = run(BACKUP, dump_path, settings=catalog_settings)
    assert taken.returncode == 0, taken.stderr
    assert bytes_written(dump_path) > 0

    put_back = run(RESTORE, dump_path, RESTORE_DATABASE, settings=catalog_settings)
    assert put_back.returncode == 0, put_back.stderr

    engine = create_engine(restored.database_url)
    try:
        with engine.connect() as connection:
            found = {
                "merchant": connection.execute(
                    text("select id from merchant where slug = 'backup-shop'")
                ).scalar_one(),
                "snapshot": connection.execute(
                    text("select id from merchant_source_snapshot where merchant_id = :m"),
                    {"m": expected["merchant"]},
                ).scalar_one(),
                "hash": connection.execute(
                    text("select content_hash from merchant_source_snapshot where id = :s"),
                    {"s": expected["snapshot"]},
                ).scalar_one(),
                "representation": connection.execute(
                    text("select id from commerce_representation where merchant_id = :m"),
                    {"m": expected["merchant"]},
                ).scalar_one(),
                "workspace": connection.execute(
                    text("select id from merchant_evaluation_workspace where merchant_id = :m"),
                    {"m": expected["merchant"]},
                ).scalar_one(),
                "suite": connection.execute(
                    text("select suite_id from merchant_evaluation_workspace where id = :w"),
                    {"w": expected["workspace"]},
                ).scalar_one(),
            }
            revision = connection.execute(
                text("select version_num from alembic_version")
            ).scalar_one()
            # The world a benchmark would be prepared from came back byte for byte, which is what
            # makes a restored deployment able to reproduce a historical run rather than merely
            # remember that one happened.
            catalog = connection.execute(
                text("select catalog_fixture from merchant_evaluation_workspace where id = :w"),
                {"w": expected["workspace"]},
            ).scalar_one()
    finally:
        engine.dispose()

    assert found == expected
    # The dump carries the revision it was taken at, so `/ready` can tell a restored database
    # that needs migrating from one that does not.
    assert revision == EXPECTED_REVISION
    assert catalog == workspace.catalog_fixture


async def test_restoring_over_a_populated_database_is_refused(
    catalog_settings: Settings, dump_path: str
) -> None:
    """A restore over live data is not a recovery, it is two histories merged.

    `pg_restore` would fail on the first duplicate key and leave whatever it had already written
    behind, which is worse than either outcome an operator was choosing between.
    """
    assert run(BACKUP, dump_path, settings=catalog_settings).returncode == 0

    refused = run(RESTORE, dump_path, catalog_settings.postgres_db, settings=catalog_settings)

    assert refused.returncode == 1
    assert "Restore into an empty database" in refused.stderr


def test_a_backup_refuses_to_overwrite_an_existing_file(
    catalog_settings: Settings, dump_path: str
) -> None:
    """Two backups on one path is one backup, and the one that is gone is the older evidence."""
    assert run(BACKUP, dump_path, settings=catalog_settings).returncode == 0
    first = bytes_written(dump_path)

    refused = run(BACKUP, dump_path, settings=catalog_settings)

    assert refused.returncode == 1
    assert "refusing to overwrite" in refused.stderr
    assert bytes_written(dump_path) == first


def test_the_scripts_refuse_a_call_they_do_not_understand(catalog_settings: Settings) -> None:
    assert run(BACKUP, "one", "two", settings=catalog_settings).returncode == 64
    assert run(RESTORE, settings=catalog_settings).returncode == 64


def test_no_backup_identifier_is_ever_a_caller_string() -> None:
    """The database name a restore targets comes from an argument, so it is worth pinning.

    Both scripts pass every identifier to `psql` and `pg_restore` as a flag value rather than
    interpolating one into SQL. The one SQL statement either of them runs is a fixed count over
    `information_schema`, and this asserts that it stays fixed.
    """
    body = RESTORE.read_text(encoding="utf-8")
    assert "select count(*) from information_schema.tables where table_schema = 'public'" in body
    assert "$database" not in body.split("-tAc")[1].split("\n")[0]
