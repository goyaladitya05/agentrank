"""The supported private-beta topology, started from nothing and made to do something real.

Every other test in this suite runs inside the pytest process against a database somebody else
migrated. This one is the opposite of that on purpose: it creates an empty database, migrates it
with the real command, provisions a merchant with the real operator commands, starts a real API
process, drives it over real HTTP, and runs a real dispatcher process against it.

What it is trying to catch is the class of failure that only exists between processes. A
configuration rule that works when a test constructs `Settings` and not when a process reads its
own environment. A migration that applies from a developer's database and not from an empty one.
A session that resolves in the process that opened it. A dispatcher that needs something the API
left in memory.

The environment is a deployment's, not a developer's:

```text
AGENTRANK_ENV=production   so no .env is read, by the rule config.py enforces
POSTGRES_*                 stated explicitly, because a deployment must
OPENAI_API_KEY=            empty, so nothing can reach a model provider
GEMINI_API_KEY=            empty, for the same reason
RAZORPAY_*                 absent, so the integration is simply not configured
```

Emptied rather than inherited. A developer machine may hold a real provider key and a browser or
smoke test must never spend one; with none configured the launch is admitted for the deterministic
reference buyer, which is what makes this test the same test every time it runs.

Nothing here writes a database row by hand. Everything the deployment needs comes from the
operator commands a private-beta operator would actually run, which is the point: a bootstrap
path that only works because a test reached past it is not a bootstrap path.

The console is deliberately not started here. Two real console processes, a real browser and the
session boundary between them are covered by `apps/web/e2e/session-durability.spec.ts`, which has
the built console the browser job produces; building it a second time to assert the same thing
would be minutes of CI for no additional evidence.
"""

import os
import subprocess
import sys
import uuid
from collections.abc import Iterator
from pathlib import Path

import httpx2
import pytest
from benchmark_support import VOLTEDGE, VOLTEDGE_DIRECTORY
from conftest import administer
from deployment_support import (
    COMMAND_TIMEOUT_SECONDS,
    REPOSITORY_ROOT,
    Deployment,
    operator,
    wait_for,
)

from agentrank_api.auth.console import CONSOLE_SESSION_SCHEME
from agentrank_api.config import ENV_FILE, Settings

# Not an anyio module. Everything here is a subprocess or a synchronous HTTP call, which is what
# an operator and an orchestrator both are, and an async wrapper around blocking calls would be a
# shape this test does not have.
# The database this deployment is built in and torn down with. Named for the process so two
# runners cannot destroy each other's.
SMOKE_DATABASE = f"agentrank_smoke_{os.getpid()}"

# The verifier this deployment's one console would present. A fixed synthetic value: the real
# console derives one by HMAC from a cookie, and what this test needs is a well formed one.
VERIFIER = f"{CONSOLE_SESSION_SCHEME}_{'5e' * 32}"

# The smallest program that does what every process here does first: read its own configuration.
BUILD_SETTINGS = "from agentrank_api.config import build_settings; build_settings()"

# The same, plus what the process resolved, so a test can tell where a value came from.
REPORT_CONFIGURATION = (
    "import logging; logging.basicConfig(level='WARNING');"
    " from agentrank_api.config import build_settings; s = build_settings();"
    " print(f'database={s.postgres_db}');"
    " print(f'openai={s.openai is not None}')"
)


def deployment_environment(settings: Settings) -> dict[str, str]:
    """A deployment's environment, built rather than inherited.

    `PATH`, `HOME` and the interpreter's own variables are carried through because a subprocess
    needs them to be a subprocess at all. Every variable this application reads is set here
    explicitly, so a developer's provider key, Razorpay pair or database cannot reach the
    processes under test.

    Not a sandbox. `HOME` is still the developer's, so libpq's own file-based configuration is
    still reachable; it changes nothing here because the password is stated, and it is worth
    knowing that "nothing is inherited" is a claim about this application's variables rather
    than about the driver's.
    """
    return {
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", ""),
        "VIRTUAL_ENV": os.environ.get("VIRTUAL_ENV", ""),
        "AGENTRANK_ENV": "production",
        # The documented default rather than something quieter. The line naming this process'
        # capabilities is INFO, so a smoke test that raised the level to keep its output tidy
        # would be a smoke test that could not see the thing the startup work added.
        "AGENTRANK_LOG_LEVEL": "info",
        "POSTGRES_HOST": settings.postgres_host,
        "POSTGRES_PORT": str(settings.postgres_port),
        "POSTGRES_DB": SMOKE_DATABASE,
        "POSTGRES_USER": settings.postgres_user,
        "POSTGRES_PASSWORD": settings.postgres_password.get_secret_value(),
        "OPENAI_API_KEY": "",
        "GEMINI_API_KEY": "",
    }


@pytest.fixture
def deployment(settings: Settings, unused_port: int, tmp_path: Path) -> Iterator[Deployment]:
    """A clean database, migrated by the real command, and one API process over it.

    A `.env` is planted in the repository root's place by pointing the processes at a working
    directory that has one. It names a different database and a provider key, so a deployment
    that read it would be visibly configured by it, and the assertions below would fail.
    """
    administer(settings, f'DROP DATABASE IF EXISTS "{SMOKE_DATABASE}" WITH (FORCE)')
    administer(settings, f'CREATE DATABASE "{SMOKE_DATABASE}"')
    environment = deployment_environment(settings)
    running = Deployment(environment, unused_port, tmp_path / "deployment.log")
    try:
        migrated = subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            cwd=REPOSITORY_ROOT,
            env=environment,
            capture_output=True,
            text=True,
            timeout=COMMAND_TIMEOUT_SECONDS,
            check=False,
        )
        assert migrated.returncode == 0, f"migration failed: {migrated.stderr.strip()}"
        running.start()
        yield running
    finally:
        running.stop()
        administer(settings, f'DROP DATABASE IF EXISTS "{SMOKE_DATABASE}" WITH (FORCE)')


def test_the_supported_topology_starts_clean_and_completes_a_merchant_evaluation(
    deployment: Deployment, settings: Settings
) -> None:
    """Bootstrap, sign in, measure, read the result, restart, read it again.

    One test rather than several, and deliberately. Every step depends on the one before it, and
    a suite that started this deployment six times to assert six things would spend six times as
    long proving the same sequence.
    """
    environment = deployment_environment(settings)

    # What the process said about itself on the way up. The capability line is the one the
    # configuration work added, and what makes it worth asserting is the second half: no
    # configured value appears beside it.
    startup = deployment.output()
    assert "agentrank api starting" in startup
    assert "environment=production" in startup
    assert "capabilities=none" in startup, "no provider is configured, and it should say so"
    # This runs from the repository root, which on a developer machine has a `.env`. A deployment
    # ignores it and says so, and that sentence appearing here is the rule working in a real
    # process rather than in a test that constructed `Settings` itself.
    if (REPOSITORY_ROOT / ENV_FILE).is_file():
        assert f"ignoring {ENV_FILE}" in startup
    # The database user is deliberately not in this list. It is `agentrank`, which is also this
    # project's name and appears in every logger path, so asserting its absence would be
    # asserting that the log does not mention the application.
    for secret in (settings.postgres_password.get_secret_value(), SMOKE_DATABASE):
        assert secret not in startup, "a startup log names variables and never values"

    # Readiness before anything else. A migrated database at the revision this build expects is
    # what the deploy procedure promises, and a process that answered ready without it would make
    # the procedure unverifiable.
    ready = wait_for(f"{deployment.base_url}/ready")
    components = {entry["name"]: entry for entry in ready.json()["components"]}
    assert components["database"]["status"] == "connected"
    assert components["schema"]["status"] == "compatible"

    # Bootstrap, through the operator commands and nothing else. No row is written by hand.
    seeded = operator(
        environment, "benchmark", "seed", "--world", str(VOLTEDGE_DIRECTORY), "--json"
    )
    assert seeded["merchant_slug"] == VOLTEDGE.merchant_slug
    operator(environment, "representation", "publish-source", "--json")
    issued = operator(
        environment,
        "credentials",
        "create",
        "--merchant-slug",
        VOLTEDGE.merchant_slug,
        "--label",
        "smoke console",
        "--json",
    )
    merchant_key = str(issued["token"])

    with httpx2.Client(base_url=deployment.base_url, timeout=30.0) as client:
        # Sign in the way the console does: the key once, a session from then on.
        opened = client.post(
            "/api/v1/console/sessions",
            json={"verifier": VERIFIER},
            headers={"Authorization": f"Bearer {merchant_key}"},
        )
        assert opened.status_code == 201, opened.text
        session_headers = {"Authorization": f"Bearer {VERIFIER}"}

        # A meaningful product operation, requested exactly as the console requests it.
        preflight = client.get("/api/v1/benchmark/evaluations/preflight", headers=session_headers)
        assert preflight.status_code == 200, preflight.text
        plan = preflight.json()
        assert plan["launchable"] is True, plan
        # No provider credential is configured, so this deployment measures with the
        # deterministic reference buyer and says so rather than spending on a model.
        assert plan["buyer_profile"] == "REFERENCE_BUYER"

        requested = client.post(
            "/api/v1/benchmark/evaluations",
            json={
                "purpose": plan["purpose"],
                "representation_id": plan["representation_id"],
                "request_key": f"smoke-{uuid.uuid7().hex}",
                "plan_digest": plan["plan_digest"],
            },
            headers=session_headers,
        )
        assert requested.status_code == 201, requested.text
        launch_id = requested.json()["launch_id"]

        # A separate process executes it, which is the topology's whole point: browser request
        # handling and benchmark dispatch are different processes.
        queued = operator(environment, "benchmark", "queue", "--json")
        assert queued["queued"] == 1
        assert queued["unserviceable"] == 0

        dispatched = operator(
            environment, "benchmark", "dispatch", "--world", str(VOLTEDGE_DIRECTORY), "--json"
        )
        assert dispatched["status"] == "COMPLETED", dispatched
        run_id = str(dispatched["run_id"])

        settled = client.get(f"/api/v1/benchmark/evaluations/{launch_id}", headers=session_headers)
        assert settled.status_code == 200
        assert settled.json()["status"] == "COMPLETED"

        run = client.get(f"/api/v1/insights/runs/{run_id}", headers=session_headers)
        assert run.status_code == 200
        assert run.json()["status"] == "COMPLETED"
        assert run.json()["run_id"] == run_id

    # The process boundary. Everything above was done by a process that no longer exists.
    deployment.restart()

    with httpx2.Client(base_url=deployment.base_url, timeout=30.0) as client:
        session_headers = {"Authorization": f"Bearer {VERIFIER}"}
        assert client.get("/ready").status_code == 200

        # The session was opened by the dead process and is resolved by this one.
        current = client.get("/api/v1/console/sessions/current", headers=session_headers)
        assert current.status_code == 200

        # And the evidence the dead process wrote is still there and still readable.
        survived = client.get(f"/api/v1/insights/runs/{run_id}", headers=session_headers)
        assert survived.status_code == 200
        assert survived.json()["status"] == "COMPLETED"
        assert survived.json()["run_id"] == run_id

        # Signing out ends it, on this process and on any other.
        closed = client.delete("/api/v1/console/sessions/current", headers=session_headers)
        assert closed.status_code == 200
        assert closed.json() == {"revoked": True}
        assert client.get("/api/v1/insights/runs", headers=session_headers).status_code == 401


def test_a_deployment_process_ignores_an_env_file_in_its_working_directory(
    settings: Settings, tmp_path: Path
) -> None:
    """The headline claim, exercised where it can actually fail: in a real process.

    CI checks out no `.env`, so on the machine this runs on in anger there is nothing to ignore
    and the claim would be vacuous. One is planted here naming a database that does not exist and
    a provider credential, so a process that read it would either fail to connect or report a
    capability it does not have. It does neither.
    """
    environment = deployment_environment(settings)
    (tmp_path / ENV_FILE).write_text(
        "POSTGRES_DB=a-database-that-does-not-exist\nOPENAI_API_KEY=not-a-real-key\n"
    )

    finished = subprocess.run(  # noqa: S603  this repository's own module
        [sys.executable, "-c", REPORT_CONFIGURATION],
        cwd=tmp_path,
        env={**environment, "PYTHONPATH": str(REPOSITORY_ROOT / "apps" / "api" / "src")},
        capture_output=True,
        text=True,
        timeout=COMMAND_TIMEOUT_SECONDS,
        check=False,
    )

    assert finished.returncode == 0, finished.stderr
    assert f"database={SMOKE_DATABASE}" in finished.stdout
    assert "openai=False" in finished.stdout
    assert "a-database-that-does-not-exist" not in finished.stdout
    # And it says it found one rather than ignoring it silently.
    assert ENV_FILE in finished.stderr


def test_a_deployment_process_refuses_to_start_without_its_database_configuration(
    settings: Settings,
) -> None:
    """The other half of the promise: a deployment that states nothing does not come up.

    Run as a real process reading its own environment, because that is the code path a
    deployment takes and the one a constructed `Settings` in a test would not.
    """
    environment = deployment_environment(settings)
    del environment["POSTGRES_HOST"]

    finished = subprocess.run(
        [sys.executable, "-c", "from agentrank_api.config import build_settings; build_settings()"],
        cwd=REPOSITORY_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=COMMAND_TIMEOUT_SECONDS,
        check=False,
    )

    assert finished.returncode != 0
    assert "POSTGRES_HOST" in finished.stderr
    # Named, and never with a value beside it.
    assert settings.postgres_password.get_secret_value() not in finished.stderr
