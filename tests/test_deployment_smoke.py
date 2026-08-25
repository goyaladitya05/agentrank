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

import json
import os
import subprocess
import sys
import time
import uuid
from collections.abc import Iterator
from pathlib import Path

import httpx2
import pytest
from benchmark_support import VOLTEDGE, VOLTEDGE_DIRECTORY
from conftest import administer

from agentrank_api.auth.console import CONSOLE_SESSION_SCHEME
from agentrank_api.config import Settings

# Not an anyio module. Everything here is a subprocess or a synchronous HTTP call, which is what
# an operator and an orchestrator both are, and an async wrapper around blocking calls would be a
# shape this test does not have.
REPOSITORY_ROOT = Path(__file__).resolve().parent.parent

# The database this deployment is built in and torn down with. Named for the process so two
# runners cannot destroy each other's.
SMOKE_DATABASE = f"agentrank_smoke_{os.getpid()}"

# How long a started process gets to answer before this gives up on it, and how often it is asked.
BOOT_TIMEOUT_SECONDS = 60.0
BOOT_POLL_SECONDS = 0.2

# One operator command should never take this long. Bounded so a hung subprocess is a named
# failure rather than a job timeout with no test attached to it.
COMMAND_TIMEOUT_SECONDS = 180.0

# The verifier this deployment's one console would present. A fixed synthetic value: the real
# console derives one by HMAC from a cookie, and what this test needs is a well formed one.
VERIFIER = f"{CONSOLE_SESSION_SCHEME}_{'5e' * 32}"

# The smallest program that does what every process here does first: read its own configuration.
BUILD_SETTINGS = "from agentrank_api.config import build_settings; build_settings()"


def deployment_environment(settings: Settings, port: int) -> dict[str, str]:
    """A deployment's environment, built rather than inherited.

    `PATH` and the interpreter's own variables are carried through because a subprocess needs
    them to be a subprocess at all. Everything this application reads is set here explicitly, so
    a developer's provider key, Razorpay pair or database cannot reach the processes under test.
    """
    del port
    return {
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", ""),
        "VIRTUAL_ENV": os.environ.get("VIRTUAL_ENV", ""),
        "AGENTRANK_ENV": "production",
        "AGENTRANK_LOG_LEVEL": "warning",
        "POSTGRES_HOST": settings.postgres_host,
        "POSTGRES_PORT": str(settings.postgres_port),
        "POSTGRES_DB": SMOKE_DATABASE,
        "POSTGRES_USER": settings.postgres_user,
        "POSTGRES_PASSWORD": settings.postgres_password.get_secret_value(),
        "OPENAI_API_KEY": "",
        "GEMINI_API_KEY": "",
    }


def operator(environment: dict[str, str], *arguments: str) -> dict[str, object]:
    """Run one operator command as a shell would, and return the JSON it printed.

    A failure raises with the command's own stderr attached, because the useful thing about a
    bootstrap step that did not work is what it said.
    """
    finished = subprocess.run(  # noqa: S603  the interpreter and the module are this repository's
        [sys.executable, "-m", "agentrank_api.cli", *arguments],
        cwd=REPOSITORY_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=COMMAND_TIMEOUT_SECONDS,
        check=False,
    )
    if finished.returncode != 0:
        raise AssertionError(
            f"operator command {' '.join(arguments)} exited {finished.returncode}:"
            f" {finished.stderr.strip()}"
        )
    parsed: dict[str, object] = json.loads(finished.stdout)
    return parsed


def wait_for(url: str, *, expect: int = 200) -> httpx2.Response:
    """Poll one endpoint until it answers as expected, or fail naming what it did instead."""
    deadline = time.monotonic() + BOOT_TIMEOUT_SECONDS
    last = "never answered"
    while time.monotonic() < deadline:
        try:
            response = httpx2.get(url, timeout=2.0)
        except httpx2.HTTPError as unreachable:
            last = type(unreachable).__name__
        else:
            if response.status_code == expect:
                return response
            last = f"HTTP {response.status_code}"
        time.sleep(BOOT_POLL_SECONDS)
    raise AssertionError(f"{url} did not answer {expect} within the boot timeout: {last}")


class Deployment:
    """One API process, started and stopped as an orchestrator would."""

    def __init__(self, environment: dict[str, str], port: int) -> None:
        self._environment = environment
        self.port = port
        self.base_url = f"http://127.0.0.1:{port}"
        self._process: subprocess.Popen[bytes] | None = None

    def start(self) -> None:
        self._process = subprocess.Popen(  # noqa: S603  this repository's own module
            [
                sys.executable,
                "-m",
                "uvicorn",
                "agentrank_api.main:create_app",
                "--factory",
                "--host",
                "127.0.0.1",
                "--port",
                str(self.port),
            ],
            cwd=REPOSITORY_ROOT,
            env=self._environment,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        wait_for(f"{self.base_url}/health")

    def stop(self) -> None:
        if self._process is None:
            return
        self._process.terminate()
        self._process.wait(timeout=30)
        self._process = None

    def restart(self) -> None:
        """A process boundary an orchestrator would create, made for real.

        Not a new object in one process. The old process is signalled, waited for and gone, and
        what comes up afterwards has an empty heap, a new connection pool and no knowledge of
        anything the first one did that it did not write down.
        """
        self.stop()
        self.start()


@pytest.fixture
def deployment(settings: Settings, unused_port: int) -> Iterator[Deployment]:
    """A clean database, migrated by the real command, and one API process over it."""

    administer(settings, f'DROP DATABASE IF EXISTS "{SMOKE_DATABASE}" WITH (FORCE)')
    administer(settings, f'CREATE DATABASE "{SMOKE_DATABASE}"')
    environment = deployment_environment(settings, unused_port)
    running = Deployment(environment, unused_port)
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
    deployment: Deployment, settings: Settings, unused_port: int
) -> None:
    """Bootstrap, sign in, measure, read the result, restart, read it again.

    One test rather than several, and deliberately. Every step depends on the one before it, and
    a suite that started this deployment six times to assert six things would spend six times as
    long proving the same sequence.
    """
    environment = deployment_environment(settings, unused_port)

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


def test_a_deployment_process_refuses_to_start_without_its_database_configuration(
    settings: Settings, unused_port: int
) -> None:
    """The other half of the promise: a deployment that states nothing does not come up.

    Run as a real process reading its own environment, because that is the code path a
    deployment takes and the one a constructed `Settings` in a test would not.
    """
    environment = deployment_environment(settings, unused_port)
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
