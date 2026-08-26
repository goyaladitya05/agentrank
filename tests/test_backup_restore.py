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

import os
import re
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

REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
BACKUP = REPOSITORY_ROOT / "scripts" / "backup.sh"
RESTORE = REPOSITORY_ROOT / "scripts" / "restore.sh"

RESTORE_DATABASE = "agentrank_restore_test"


# Where the operator's tools live, which is not always the machine the tests run on.
#
# A deployment runs `pg_dump` from a host that has the PostgreSQL client installed. A development
# machine runs PostgreSQL in Docker and frequently has no client at all, and a CI runner has a
# client whose major version may be older than the server it is pointed at, which `pg_dump`
# refuses outright rather than dumping badly.
#
# So the container is preferred wherever there is one: its client is the server's own version by
# construction. A host client is used when it is new enough. Neither is a reason to pretend the
# procedure was verified, so the remaining case is a skip that says which of the two was missing.
#
# It is the same script either way. `bash -s` reads it from this repository over standard input,
# so nothing about the procedure changes to accommodate where it runs.
DOCKER = shutil.which("docker") or "docker"


def _compose_postgres() -> bool:
    """Whether this repository's own PostgreSQL container is up and reachable."""
    if shutil.which("docker") is None:
        return False
    probe = subprocess.run(  # noqa: S603  the compose service this suite already runs against
        [DOCKER, "compose", "exec", "-T", "postgres", "which", "pg_dump"],
        capture_output=True,
        cwd=REPOSITORY_ROOT,
        check=False,
        timeout=60,
    )
    return probe.returncode == 0


def _host_client_is_new_enough() -> bool:
    """Whether the host's own `pg_dump` can dump the server this suite runs against.

    `pg_dump` refuses a server newer than itself, which is the case on a runner shipping an older
    client. Comparing majors here turns that into a skip with a reason rather than a failure in
    the middle of a dump.
    """
    client = shutil.which("pg_dump")
    if client is None:
        return False
    reported = subprocess.run(  # noqa: S603  the client this machine's own PATH resolved
        [client, "--version"], capture_output=True, text=True, check=False, timeout=60
    )
    if reported.returncode != 0:
        return False
    digits = "".join(
        character
        for character in reported.stdout.split()[-1]
        if character.isdigit() or character == "."
    )
    major = digits.split(".")[0]
    return major.isdigit() and int(major) >= SERVER_MAJOR


# The PostgreSQL this repository runs against, stated once. `scripts/check-postgres.sh` holds the
# same number for the same reason.
SERVER_MAJOR = 18

IN_CONTAINER = _compose_postgres()
HOST_CLIENT = not IN_CONTAINER and _host_client_is_new_enough()
RUNNABLE = IN_CONTAINER or HOST_CLIENT
UNRUNNABLE = (
    "no PostgreSQL client this suite can dump a version"
    f" {SERVER_MAJOR} server with, on the host or in a container"
)

# Inside the container the server is on loopback whatever the host reaches it by, and the dump
# lives in the container's own temporary directory because that is where the tool writing it is.
CONTAINER_DUMP = "/tmp/agentrank-backup-test.dump"  # noqa: S108  a path inside a throwaway container


pytestmark = [pytest.mark.anyio, pytest.mark.skipif(not RUNNABLE, reason=UNRUNNABLE)]


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
    # Names on the command line and values in this process' own environment, which `docker compose
    # exec --env NAME` forwards. The value form would put the database password in an argument
    # vector every process on the host can read, which is exactly what `scripts/backup.sh` goes to
    # trouble to avoid and would be a poor thing for the test that proves it to reintroduce.
    forwarded = [item for name in inside for item in ("--env", name)]
    return subprocess.run(  # noqa: S603  the compose service this suite already runs against
        [DOCKER, "compose", "exec", "-T", *forwarded, "postgres", "bash", "-s", "--", *arguments],
        input=script.read_text(encoding="utf-8"),
        capture_output=True,
        text=True,
        cwd=REPOSITORY_ROOT,
        env=os.environ | inside,
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


def file_mode(path: str) -> int:
    """The permission bits on a file the backup wrote, wherever the backup ran."""
    if HOST_CLIENT:
        return Path(path).stat().st_mode & 0o777
    reported = subprocess.run(  # noqa: S603  the compose service this suite already runs against
        [DOCKER, "compose", "exec", "-T", "postgres", "stat", "-c", "%a", path],
        capture_output=True,
        text=True,
        cwd=REPOSITORY_ROOT,
        check=False,
        timeout=60,
    )
    assert reported.returncode == 0, reported.stderr
    return int(reported.stdout.strip(), 8)


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


def test_no_caller_string_is_ever_interpolated_into_sql() -> None:
    """Both scripts pass every identifier to `psql` as a flag value, never into a statement.

    Asserted over every quoted SQL literal in the file rather than over the line after the first
    `-tAc`, which is a line continuation and made this assertion trivially true.
    """
    body = RESTORE.read_text(encoding="utf-8")
    statements = re.findall(r'"(select [^"]+)"', body)
    assert statements, body
    for statement in statements:
        assert "$" not in statement, statement


async def test_a_database_name_that_is_a_connection_string_is_refused(
    catalog_settings: Settings, dump_path: str
) -> None:
    """libpq reads a `dbname` containing `=` or `://` as a whole connection string.

    It would override the host, the port and the user this script resolved, and offer the
    deployment's password to whatever host it named, on the emptiness probe before `pg_restore`
    even runs.
    """
    assert run(BACKUP, dump_path, settings=catalog_settings).returncode == 0

    refused = run(
        RESTORE,
        dump_path,
        "host=collector.example user=x dbname=y",
        settings=catalog_settings,
    )

    assert refused.returncode == 64
    assert "connection string" in refused.stderr


def test_a_backup_is_readable_only_by_the_operator_who_took_it(
    catalog_settings: Settings, dump_path: str
) -> None:
    """The most sensitive file this project produces is not world readable.

    It holds every tenant's evidence plus every credential and session digest. Under the default
    umask `pg_dump` would create it 0644, which on a shared host or a CI runner is every other
    account on the machine.
    """
    assert run(BACKUP, dump_path, settings=catalog_settings).returncode == 0

    assert file_mode(dump_path) & 0o077 == 0


def test_a_backup_that_fails_leaves_no_file_pretending_to_be_one(
    catalog_settings: Settings, dump_path: str
) -> None:
    """A truncated dump is discovered at the moment somebody needs it, which is the worst moment.

    The failure is produced by pointing the script at a database that does not exist, which is
    the shape of every real one: the file is created before `pg_dump` runs and the dump then
    does not finish.
    """
    missing = catalog_settings.model_copy(update={"postgres_db": "agentrank_no_such_database"})

    failed = run(BACKUP, dump_path, settings=missing)

    assert failed.returncode != 0
    assert bytes_written(dump_path) == 0
