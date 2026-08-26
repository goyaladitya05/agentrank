"""The whole merchant product, from an empty database to a comparison, in one deployment.

This is the release candidate's own test. Every other smoke here proves one boundary; this one
proves that the boundaries join up, by taking a merchant who does not exist through every step
the product actually offers and reading the evidence back afterwards.

```text
empty database -> migrations -> operator provisions a merchant and issues their key
sign in -> import their public pages -> review what was read -> create a source snapshot
build an evaluation setup -> first evaluation preflight -> dispatch it -> read its diagnostics
refresh the source -> compile it -> review the facts -> publish -> re-evaluate -> compare
sign out -> restart the API -> the evidence is still there
```

Nothing is inserted behind the workflow. Every step is the HTTP command the console makes or the
operator command a private-beta operator runs, in that order, against processes that are really
running. The one thing an operator does that a merchant cannot is the first line: there is no
public signup, so a merchant exists because somebody with a shell created one.

The storefront is a separate process serving five invented pages on loopback. Reaching loopback
at all requires `AGENTRANK_IMPORT_ALLOWED_NETWORKS`, which `Settings` refuses to load outside
development, CI and test, so this deployment runs as `ci` rather than `production`. That is the
one difference from `tests/test_deployment_smoke.py` and it is the boundary being demonstrated
from the side that is allowed to see it: every other variable is stated explicitly, including
both provider keys as empty, and the test asserts the process came up with no provider
capability at all.

No model provider is contacted and none could be. The buyer is the deterministic reference
executor, which is what makes this runnable with no credential and no quota, and its result is
evidence that the product path works rather than evidence about an autonomous agent.
"""

import json
import os
import socket
import subprocess
import sys
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx2
import pytest
from conftest import administer
from deployment_support import (
    COMMAND_TIMEOUT_SECONDS,
    REPOSITORY_ROOT,
    Deployment,
    operator,
    wait_for,
)

from agentrank_api.auth.console import CONSOLE_SESSION_SCHEME
from agentrank_api.config import Settings

# Not an anyio module, for the reason the deployment smoke states: everything here is a
# subprocess or a synchronous HTTP call, which is what an operator and a browser both are.
RELEASE_DATABASE = f"agentrank_release_{os.getpid()}"

MERCHANT = "release-candidate-shop"
VERIFIER = f"{CONSOLE_SESSION_SCHEME}_{'7c' * 32}"
# A second sign in needs a second credential. A session that has been signed out cannot be
# reopened, which is the rule Phase 5A made structural, so a browser signing in again presents a
# new cookie and therefore a new verifier.
SECOND_VERIFIER = f"{CONSOLE_SESSION_SCHEME}_{'9d' * 32}"

FIXTURE = REPOSITORY_ROOT / "scripts" / "serve_import_fixture.py"

# The merchant's own pages, in the order they would be pasted into the console.
PRODUCT_PATHS = ("/p/charger", "/p/sleeve", "/p/dock")
POLICY_PATH = "/returns"


def free_ports(count: int) -> list[int]:
    """Distinct ports with nothing listening on them, held open until all are chosen."""
    holders = [socket.socket(socket.AF_INET, socket.SOCK_STREAM) for _ in range(count)]
    try:
        for holder in holders:
            holder.bind(("127.0.0.1", 0))
        return [int(holder.getsockname()[1]) for holder in holders]
    finally:
        for holder in holders:
            holder.close()


def release_environment(settings: Settings, database: str) -> dict[str, str]:
    """This deployment's environment, stated rather than inherited.

    `ci` rather than `production` for one reason and it is written down above: the importer's
    address policy refuses loopback, and the variable that widens it is one `Settings` will not
    read anywhere else. Every other variable is set here explicitly, so a developer's provider
    key, Razorpay pair or database cannot reach the processes under test.
    """
    return {
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", ""),
        "VIRTUAL_ENV": os.environ.get("VIRTUAL_ENV", ""),
        "AGENTRANK_ENV": "ci",
        "AGENTRANK_LOG_LEVEL": "info",
        "POSTGRES_HOST": settings.postgres_host,
        "POSTGRES_PORT": str(settings.postgres_port),
        "POSTGRES_DB": database,
        "POSTGRES_USER": settings.postgres_user,
        "POSTGRES_PASSWORD": settings.postgres_password.get_secret_value(),
        "OPENAI_API_KEY": "",
        "GEMINI_API_KEY": "",
        "AGENTRANK_IMPORT_ALLOWED_NETWORKS": "127.0.0.0/8",
    }


class Storefront:
    """The merchant's public website, as a process that is not this application."""

    def __init__(self, port: int, log: Path) -> None:
        self.port = port
        self.origin = f"http://127.0.0.1:{port}"
        self._log = log
        self._process: subprocess.Popen[bytes] | None = None

    def url(self, path: str) -> str:
        return f"{self.origin}{path}"

    def start(self) -> None:
        self._stream = self._log.open("ab")
        self._process = subprocess.Popen(  # noqa: S603  this repository's own script
            [sys.executable, str(FIXTURE), "--port", str(self.port)],
            cwd=REPOSITORY_ROOT,
            stdout=self._stream,
            stderr=subprocess.STDOUT,
        )
        wait_for(self.url(PRODUCT_PATHS[0]))

    def stop(self) -> None:
        if self._process is None:
            return
        self._process.terminate()
        try:
            self._process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            self._process.kill()
            self._process.wait(timeout=30)
        self._process = None
        self._stream.close()


@pytest.fixture
def release(settings: Settings, tmp_path: Path) -> Iterator[tuple[Deployment, Storefront]]:
    """A clean database, one API process over it, and the merchant's storefront beside it."""
    api_port, storefront_port = free_ports(2)
    administer(settings, f'DROP DATABASE IF EXISTS "{RELEASE_DATABASE}" WITH (FORCE)')
    administer(settings, f'CREATE DATABASE "{RELEASE_DATABASE}"')
    environment = release_environment(settings, RELEASE_DATABASE)
    api = Deployment(environment, api_port, tmp_path / "api.log")
    storefront = Storefront(storefront_port, tmp_path / "storefront.log")
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
        storefront.start()
        api.start()
        yield api, storefront
    finally:
        api.stop()
        storefront.stop()
        administer(settings, f'DROP DATABASE IF EXISTS "{RELEASE_DATABASE}" WITH (FORCE)')


def refreshed_document(document: dict[str, Any]) -> dict[str, Any]:
    """The merchant's own source, with prose a compiler can read a fact out of.

    Prose only. The evaluation setup was generated from the earlier snapshot and a re-evaluation
    runs in that world, so a refresh that moved a price or withdrew a line would be a
    representation describing a different shop and the launch would refuse it by name. Adding a
    stated wattage to a description is the ordinary loop this product is built around: the
    diagnostics say a fact is not published, the merchant publishes it, and the compiler carries
    it into an agent-ready surface.
    """
    products: list[dict[str, Any]] = [dict(product) for product in document["products"]]
    first = products[0]
    stated = first.get("description") or ""
    first["description"] = f"{stated} Ships in recyclable packaging.".strip()
    return {"products": products, "policy_text": document["policy_text"]}


def test_the_whole_merchant_product_runs_end_to_end_with_no_provider(
    release: tuple[Deployment, Storefront], settings: Settings
) -> None:
    """One merchant, one deployment, every step of the product, and nothing written by hand."""
    api, storefront = release
    environment = release_environment(settings, RELEASE_DATABASE)

    startup = api.output()
    assert "environment=ci" in startup
    # The one capability this deployment holds, named exactly. It says this process may fetch
    # merchant pages from somewhere other than the public internet, which is what lets it reach
    # a storefront on loopback and is the reason it is `ci` rather than `production`. No provider
    # is in that list, so no provider can be reached from here.
    assert "capabilities=import_networks_widened" in startup, startup
    assert settings.postgres_password.get_secret_value() not in startup

    ready = wait_for(f"{api.base_url}/ready")
    components = {entry["name"]: entry for entry in ready.json()["components"]}
    assert components["schema"]["status"] == "compatible"

    # The operator's whole job: a merchant, and a key for them. No catalog, no world, no row
    # written by hand, and nothing decided on the merchant's behalf.
    created = operator(
        environment,
        "merchants",
        "create",
        "--merchant-slug",
        MERCHANT,
        "--name",
        "Release",
        "--json",
    )
    assert created["created"] is True
    issued = operator(
        environment,
        "credentials",
        "create",
        "--merchant-slug",
        MERCHANT,
        "--label",
        "console",
        "--json",
    )

    with httpx2.Client(base_url=api.base_url, timeout=60.0) as client:
        opened = client.post(
            "/api/v1/console/sessions",
            json={"verifier": VERIFIER},
            headers={"Authorization": f"Bearer {issued['token']}"},
        )
        assert opened.status_code == 201, opened.text
        headers = {"Authorization": f"Bearer {VERIFIER}"}

        # Import the merchant's own public pages, over a real socket, from a real web server.
        imported = client.post(
            "/api/v1/sources/imports",
            json={
                "request_key": f"release-import-{uuid.uuid7().hex}",
                "pages": [
                    *(
                        {"url": storefront.url(path), "kind": "PRODUCT", "name": None}
                        for path in PRODUCT_PATHS
                    ),
                    {"url": storefront.url(POLICY_PATH), "kind": "POLICY", "name": "returns"},
                ],
            },
            headers=headers,
        )
        assert imported.status_code == 201, imported.text
        draft = imported.json()
        assert draft["summary"]["product_count"] == 3
        assert draft["summary"]["policy_count"] == 1
        assert draft["confirmable"] is True
        # Every page published a state and none published a count, which is the ordinary
        # storefront this whole representation exists for.
        stock = {
            (variant["availability"], variant["inventory_quantity"])
            for product in draft["products"]
            for variant in product["variants"]
        }
        assert stock == {("IN_STOCK", None), ("OUT_OF_STOCK", None)}
        assert draft["unstated_availability"] == []

        # Confirming states nothing. It is a decision about evidence already read.
        confirmed = client.post(
            f"/api/v1/sources/imports/{draft['summary']['import_id']}/confirm",
            json={},
            headers=headers,
        )
        assert confirmed.status_code == 201, confirmed.text
        assert confirmed.json()["created_snapshot"] is True
        first_snapshot = confirmed.json()["source_snapshot_id"]

        # The evaluation setup, generated from that snapshot and from nothing else.
        setup = client.post(
            "/api/v1/benchmark/workspace",
            json={"source_snapshot_id": first_snapshot},
            headers=headers,
        )
        assert setup.status_code == 201, setup.text
        built = setup.json()["workspace"]
        assert built["catalog"]["simulated_stock_variants"] > 0, built["catalog"]
        assert built["mission_count"] > 0

        # The first evaluation, which measures the snapshot the world was built from.
        preflight = client.get("/api/v1/benchmark/evaluations/preflight", headers=headers).json()
        assert preflight["purpose"] == "INITIAL"
        assert preflight["launchable"] is True, preflight["blockers"]
        assert preflight["buyer_profile"] == "REFERENCE_BUYER"
        assert preflight["source_snapshot_id"] == first_snapshot
        assert preflight["source_is_newer_than_the_setup"] is False

        launched = client.post(
            "/api/v1/benchmark/evaluations",
            json={
                "purpose": "INITIAL",
                "representation_id": None,
                "request_key": f"release-initial-{uuid.uuid7().hex}",
                "plan_digest": preflight["plan_digest"],
            },
            headers=headers,
        )
        assert launched.status_code == 201, launched.text
        first_launch = launched.json()["launch_id"]

    # A separate process executes it, against the world this merchant's own setup generated.
    queued = operator(environment, "benchmark", "queue", "--json")
    assert queued["queued"] == 1
    assert queued["unserviceable"] == 0
    dispatched = operator(
        environment, "benchmark", "dispatch", "--merchant-slug", MERCHANT, "--json"
    )
    assert dispatched["status"] == "COMPLETED", dispatched
    first_run = str(dispatched["run_id"])

    with httpx2.Client(base_url=api.base_url, timeout=60.0) as client:
        headers = {"Authorization": f"Bearer {VERIFIER}"}
        settled = client.get(f"/api/v1/benchmark/evaluations/{first_launch}", headers=headers)
        assert settled.status_code == 200
        assert settled.json()["status"] == "COMPLETED"

        # Diagnostics, which is what a merchant reads a run for.
        diagnosed = client.get(f"/api/v1/insights/runs/{first_run}", headers=headers)
        assert diagnosed.status_code == 200
        assert diagnosed.json()["status"] == "COMPLETED"

        # Refresh the source, naming the snapshot the editor was showing.
        snapshot = client.get(f"/api/v1/sources/{first_snapshot}", headers=headers).json()
        refreshed = client.post(
            "/api/v1/sources",
            json={
                **refreshed_document(snapshot["document"]),
                "request_key": f"release-refresh-{uuid.uuid7().hex}",
                "base_source_snapshot_id": first_snapshot,
            },
            headers=headers,
        )
        assert refreshed.status_code == 201, refreshed.text
        assert refreshed.json()["created_snapshot"] is True
        second_snapshot = refreshed.json()["snapshot"]["source_snapshot_id"]

        # Compile it, review whatever needs a decision, and publish.
        compiled = client.post(
            "/api/v1/compiler/runs",
            json={"source_snapshot_id": second_snapshot},
            headers=headers,
        )
        assert compiled.status_code == 201, compiled.text
        compiler_run = compiled.json()["run_id"]
        assert compiled.json()["status"] == "COMPLETED"

        candidates = client.get(f"/api/v1/compiler/runs/{compiler_run}", headers=headers).json()[
            "candidates"
        ]
        for candidate in candidates:
            if candidate["state"] != "REVIEW_REQUIRED" or candidate["review"] is not None:
                continue
            decided = client.post(
                f"/api/v1/compiler/candidates/{candidate['candidate_id']}/reject",
                headers=headers,
            )
            assert decided.status_code == 201, decided.text

        published = client.post(
            f"/api/v1/compiler/runs/{compiler_run}/publish", headers=headers
        )
        assert published.status_code == 200, published.text
        representation_id = published.json()["readiness"]["published_representation_id"]
        assert representation_id is not None

        # The re-evaluation, which measures the published representation in the same world.
        second_preflight = client.get(
            "/api/v1/benchmark/evaluations/preflight", headers=headers
        ).json()
        assert second_preflight["purpose"] == "REEVALUATION"
        assert second_preflight["launchable"] is True, second_preflight["blockers"]
        assert second_preflight["representation_id"] == representation_id
        # The setup was built from the earlier snapshot and the merchant has since refreshed it.
        # Said out loud rather than left to be inferred.
        assert second_preflight["source_is_newer_than_the_setup"] is True
        # The earlier run is what this one will be read against, and the two are comparable:
        # the reference buyer reads structured commerce fields and receives neither the
        # storefront discovery view nor an agent-ready one, so both runs measured the same thing.
        # With a model buyer they would not be, and the preflight says so before the merchant
        # spends rather than after.
        assert second_preflight["baseline_run_id"] == first_run
        assert second_preflight["baseline_surface_matches"] is True

        relaunched = client.post(
            "/api/v1/benchmark/evaluations",
            json={
                "purpose": "REEVALUATION",
                "representation_id": representation_id,
                "request_key": f"release-reeval-{uuid.uuid7().hex}",
                "plan_digest": second_preflight["plan_digest"],
            },
            headers=headers,
        )
        assert relaunched.status_code == 201, relaunched.text
        second_launch = relaunched.json()["launch_id"]

    second_dispatch = operator(
        environment, "benchmark", "dispatch", "--merchant-slug", MERCHANT, "--json"
    )
    assert second_dispatch["status"] == "COMPLETED", second_dispatch
    second_run = str(second_dispatch["run_id"])

    with httpx2.Client(base_url=api.base_url, timeout=60.0) as client:
        headers = {"Authorization": f"Bearer {VERIFIER}"}
        detail = client.get(f"/api/v1/benchmark/evaluations/{second_launch}", headers=headers)
        assert detail.status_code == 200
        assert detail.json()["status"] == "COMPLETED"

        # The comparison, read against the first evaluation. What it says is decided after the
        # fact from the two runs rather than promised in advance, and either answer is a real
        # one: a reading, or a refusal naming which of the seven pins differ.
        comparison = detail.json()["comparison"]
        assert comparison is not None
        assert isinstance(comparison["comparable"], bool)
        assert comparison["comparable"] or comparison["reasons"], comparison

        # Both runs are still exactly what they were.
        for run_id in (first_run, second_run):
            assert (
                client.get(f"/api/v1/insights/runs/{run_id}", headers=headers).json()["status"]
                == "COMPLETED"
            )

        closed = client.delete("/api/v1/console/sessions/current", headers=headers)
        assert closed.status_code == 200
        assert client.get("/api/v1/insights/runs", headers=headers).status_code == 401

    # The process boundary. Everything above was done by a process that no longer exists.
    api.restart()

    with httpx2.Client(base_url=api.base_url, timeout=60.0) as client:
        # A signed out session cannot be reopened, so this is a new sign in with a new
        # credential, exactly as a browser that had been signed out would make.
        stale = client.post(
            "/api/v1/console/sessions",
            json={"verifier": VERIFIER},
            headers={"Authorization": f"Bearer {issued['token']}"},
        )
        assert stale.status_code == 409, stale.text
        reopened = client.post(
            "/api/v1/console/sessions",
            json={"verifier": SECOND_VERIFIER},
            headers={"Authorization": f"Bearer {issued['token']}"},
        )
        assert reopened.status_code == 201, reopened.text
        headers = {"Authorization": f"Bearer {SECOND_VERIFIER}"}

        # Every artifact this merchant produced is still readable and still says what it said.
        sources = client.get("/api/v1/sources", headers=headers).json()
        assert sources["current_source_snapshot_id"] == second_snapshot
        history = client.get("/api/v1/benchmark/evaluations", headers=headers).json()
        assert {entry["launch_id"] for entry in history} == {first_launch, second_launch}
        survived = client.get(f"/api/v1/insights/runs/{first_run}", headers=headers)
        assert survived.status_code == 200
        assert survived.json()["run_id"] == first_run

    # And what the operator can read about it afterwards, which is the question a merchant asks
    # when something is not happening.
    launches = operator(environment, "benchmark", "launches", "--merchant-slug", MERCHANT, "--json")
    settled = list(launches["launches"])  # type: ignore[call-overload]
    assert {entry["status"] for entry in settled} == {"COMPLETED"}
    assert len(settled) == 2


def test_no_model_provider_is_reachable_from_this_deployment(
    release: tuple[Deployment, Storefront], settings: Settings
) -> None:
    """The zero-provider claim, made about the running process rather than about a constant."""
    api, _ = release
    reported = subprocess.run(
        [
            sys.executable,
            "-c",
            "from agentrank_api.config import build_settings; s = build_settings();"
            " import json; print(json.dumps({'openai': s.openai is not None,"
            " 'gemini': s.gemini is not None, 'environment': s.environment}))",
        ],
        cwd=REPOSITORY_ROOT,
        env=release_environment(settings, RELEASE_DATABASE),
        capture_output=True,
        text=True,
        timeout=COMMAND_TIMEOUT_SECONDS,
        check=False,
    )
    assert reported.returncode == 0, reported.stderr
    assert json.loads(reported.stdout) == {
        "openai": False,
        "gemini": False,
        "environment": "ci",
    }
    # The startup line names the one capability this deployment holds and no provider is in it.
    assert "capabilities=import_networks_widened" in api.output()
