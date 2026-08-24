"""Starting a compiler run from the console, and what the run that comes back is bound to.

The command is deliberately thin, because the compiler already had the properties a launch
command needs: a run is unique on its source snapshot and its configuration digest, so one
document read one way is one run however many times it is asked for. What is asserted here is
that the thinness is real. A repeat resolves to the run that exists, two concurrent requests
produce one run against real PostgreSQL, a foreign snapshot is an unknown one, and the candidates
that come out cite the snapshot that was actually compiled rather than the merchant's newest.
"""

import asyncio
import uuid

import pytest
from conftest import CredentialIssuer, bearer
from fastapi.testclient import TestClient
from reevaluation_support import build_launch_world
from source_support import (
    FIRST_KEY,
    contradicted_document,
    merchant_with_source,
    submission,
    voltedge_document,
)
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agentrank_api.auth.service import MerchantCredentialService
from agentrank_api.auth.tokens import TokenMarker
from agentrank_api.benchmark.execution import BenchmarkRunCapability
from agentrank_api.benchmark.runner import BenchmarkRunService
from agentrank_api.compiler.definitions import CompilerConfiguration
from agentrank_api.compiler.models import CompilerRun
from agentrank_api.compiler.service import MerchantCompilerService
from agentrank_api.config import Settings
from agentrank_api.main import create_app
from agentrank_api.payments.fake import FakePaymentProvider
from agentrank_api.representation.fields import MAX_EXCERPT_LENGTH

pytestmark = pytest.mark.anyio

RUNS = "/api/v1/compiler/runs"
SOURCES = "/api/v1/sources"


def client(settings: Settings, sessions: async_sessionmaker[AsyncSession]) -> TestClient:
    app = create_app(settings, payment_provider=FakePaymentProvider())
    app.state.session_factory = sessions
    return TestClient(app)


async def test_a_merchant_compiles_newer_evidence_and_reaches_the_review_workflow(
    settings: Settings,
    session: AsyncSession,
    factory: async_sessionmaker[AsyncSession],
    issue_credential: CredentialIssuer,
) -> None:
    merchant, first = await merchant_with_source(session, "run-command-shop")
    token = await issue_credential(merchant.id)
    http = client(settings, factory)
    supplied = http.post(
        SOURCES, headers=bearer(token), json=submission(contradicted_document(), FIRST_KEY)
    ).json()["snapshot"]

    started = http.post(
        RUNS, headers=bearer(token), json={"source_snapshot_id": supplied["source_snapshot_id"]}
    )

    assert started.status_code == 201
    run = started.json()
    assert run["status"] == "COMPLETED"
    assert run["source_snapshot_id"] == supplied["source_snapshot_id"]
    assert run["source_label"] == supplied["source_label"]
    assert run["configuration_digest"] == CompilerConfiguration().configuration_digest
    # The contradicted wattage is exactly the fact the merchant now has to answer for.
    waiting = [
        candidate for candidate in run["candidates"] if candidate["state"] == "REVIEW_REQUIRED"
    ]
    assert {candidate["attribute"] for candidate in waiting} == {"wattage"}
    assert run["readiness"]["publishable"] is False
    assert run["readiness"]["published_representation_id"] is None

    # The same run is reachable at the review address the console already had.
    reread = http.get(f"{RUNS}/{run['run_id']}", headers=bearer(token))
    assert reread.status_code == 200
    assert reread.json()["run_id"] == run["run_id"]

    # And the snapshot now knows what has been compiled from it.
    detail = http.get(f"{SOURCES}/{supplied['source_snapshot_id']}", headers=bearer(token)).json()
    assert detail["compilable"] is False
    assert detail["existing_run_id"] == run["run_id"]
    assert [entry["run_id"] for entry in detail["compiler_runs"]] == [run["run_id"]]
    assert detail["summary"]["compiler_run_count"] == 1

    # Nothing was compiled from the snapshot the operator published.
    original = http.get(f"{SOURCES}/{first.id}", headers=bearer(token)).json()
    assert original["compiler_runs"] == []
    assert original["compilable"] is True


async def test_candidate_evidence_cites_the_snapshot_that_was_compiled(
    settings: Settings,
    session: AsyncSession,
    factory: async_sessionmaker[AsyncSession],
    issue_credential: CredentialIssuer,
) -> None:
    """Provenance follows the run, not the merchant's newest evidence.

    The merchant compiles one snapshot and then supplies a third. The first run's candidates
    still cite the second snapshot's text, and every address they cite is one that snapshot
    actually has.
    """
    merchant, _ = await merchant_with_source(session, "provenance-shop")
    token = await issue_credential(merchant.id)
    http = client(settings, factory)
    compiled = http.post(
        SOURCES, headers=bearer(token), json=submission(contradicted_document(), FIRST_KEY)
    ).json()["snapshot"]
    run = http.post(
        RUNS, headers=bearer(token), json={"source_snapshot_id": compiled["source_snapshot_id"]}
    ).json()

    newer = contradicted_document()
    newer["products"][0]["description"] = "Explicitly supports 45W, unlike its 100W title."
    superseding = http.post(
        SOURCES, headers=bearer(token), json=submission(newer, "third-submission-key")
    ).json()["snapshot"]
    assert superseding["source_snapshot_id"] != compiled["source_snapshot_id"]

    evidence_by_field = {
        entry["field"]: entry["excerpt"]
        for entry in http.get(
            f"{SOURCES}/{compiled['source_snapshot_id']}", headers=bearer(token)
        ).json()["fields"]
    }
    addresses = set(evidence_by_field)
    cited = [evidence for candidate in run["candidates"] for evidence in candidate["evidence"]]
    assert cited
    assert {evidence["field"] for evidence in cited} <= addresses
    assert all(
        evidence["excerpt"] is None or len(evidence["excerpt"]) <= MAX_EXCERPT_LENGTH
        for evidence in cited
    )
    # The fact the merchant has to answer for cites the charger's own text, and the excerpt it
    # quotes is really in the snapshot that was compiled rather than in the newer one.
    wattage = next(
        candidate
        for candidate in run["candidates"]
        if candidate["target"] == "variant.VE-CHG-100-BLK.attribute.wattage"
    )
    quoted = wattage["evidence"][0]
    assert quoted["field"].startswith("products[VE-CHG-100].")
    assert quoted["excerpt"] in evidence_by_field[quoted["field"]]
    assert "45W" not in "".join(evidence_by_field.values())

    # The run still names the snapshot it read, and the newer one has no run at all.
    assert (
        http.get(f"{RUNS}/{run['run_id']}", headers=bearer(token)).json()["source_snapshot_id"]
        == compiled["source_snapshot_id"]
    )
    assert (
        http.get(f"{SOURCES}/{superseding['source_snapshot_id']}", headers=bearer(token)).json()[
            "compiler_runs"
        ]
        == []
    )


async def test_a_repeated_launch_resolves_to_the_run_that_already_exists(
    settings: Settings,
    session: AsyncSession,
    factory: async_sessionmaker[AsyncSession],
    issue_credential: CredentialIssuer,
) -> None:
    merchant, snapshot = await merchant_with_source(session, "repeat-launch-shop")
    token = await issue_credential(merchant.id)
    http = client(settings, factory)
    body = {"source_snapshot_id": str(snapshot.id)}

    first = http.post(RUNS, headers=bearer(token), json=body)
    repeat = http.post(RUNS, headers=bearer(token), json=body)

    assert (first.status_code, repeat.status_code) == (201, 201)
    assert repeat.json()["run_id"] == first.json()["run_id"]
    assert await _run_count(session, snapshot.id) == 1


async def test_two_concurrent_launches_produce_one_run(
    session: AsyncSession,
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Independent sessions, so the two genuinely race on the unique key rather than take turns."""
    merchant, snapshot = await merchant_with_source(session, "racing-launch-shop")
    merchant_id, snapshot_id = merchant.id, snapshot.id

    async def start() -> str:
        async with factory() as racing:
            run = await MerchantCompilerService(racing).run(merchant_id, snapshot_id)
            return str(run.id)

    first, second = await asyncio.gather(start(), start())

    assert first == second
    async with factory() as verify:
        assert await _run_count(verify, snapshot_id) == 1


async def test_a_launch_naming_another_merchants_snapshot_is_an_unknown_one(
    settings: Settings,
    session: AsyncSession,
    factory: async_sessionmaker[AsyncSession],
    issue_credential: CredentialIssuer,
) -> None:
    merchant, _ = await merchant_with_source(session, "launch-scope-shop")
    _, foreign = await merchant_with_source(session, "launch-foreign-shop", name="Foreign")
    token = await issue_credential(merchant.id)
    http = client(settings, factory)

    refused = http.post(RUNS, headers=bearer(token), json={"source_snapshot_id": str(foreign.id)})
    unknown = http.post(RUNS, headers=bearer(token), json={"source_snapshot_id": str(uuid.uuid7())})

    assert refused.status_code == 404
    assert unknown.status_code == 404
    assert refused.json()["error"] == unknown.json()["error"] == "not_found"
    assert await _run_count(session, foreign.id) == 0


async def test_a_launch_body_cannot_carry_a_merchant_or_a_compiler_configuration(
    settings: Settings,
    session: AsyncSession,
    factory: async_sessionmaker[AsyncSession],
    issue_credential: CredentialIssuer,
) -> None:
    merchant, snapshot = await merchant_with_source(session, "launch-smuggle-shop")
    other, _ = await merchant_with_source(session, "launch-victim-shop", name="Victim")
    token = await issue_credential(merchant.id)
    http = client(settings, factory)

    for field, value in (
        ("merchant_id", str(other.id)),
        ("configuration", {"compiler_kind": "anything"}),
        ("configuration_digest", "sha256:" + "0" * 64),
        ("semantic_extractor", "gpt"),
        ("status", "COMPLETED"),
    ):
        answer = http.post(
            RUNS,
            headers=bearer(token),
            json={"source_snapshot_id": str(snapshot.id), field: value},
        )
        assert answer.status_code == 422, field

    assert await _run_count(session, snapshot.id) == 0


async def test_the_compiler_namespace_refuses_a_benchmark_buyers_credential(
    settings: Settings,
    session: AsyncSession,
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """A buyer executing a run must not be able to change what it is being measured against.

    Source evidence is the compiler's only input and a compiler representation is what an AI
    buyer is given as its discovery surface, so a credential minted for a run has no business
    supplying evidence, compiling it, reviewing it or publishing it.
    """
    world = await build_launch_world(session, "buyer-compiler-shop")
    started = await BenchmarkRunService(session).start_run(
        suite_key=world.suite.suite_key,
        suite_version=world.suite.version,
        merchant_slug=world.merchant_slug,
    )
    issued = await MerchantCredentialService(session).issue_for_benchmark(
        capability=BenchmarkRunCapability(merchant_id=world.merchant_id, run_id=started.id),
        label="benchmark executor",
        marker=TokenMarker.DEVELOPMENT,
    )
    http = client(settings, factory)
    headers = bearer(issued.token)

    answers = [
        http.get("/api/v1/compiler/overview", headers=headers),
        http.get(f"{RUNS}/{uuid.uuid7()}", headers=headers),
        http.post(
            RUNS, headers=headers, json={"source_snapshot_id": str(world.source_snapshot_id)}
        ),
        http.post(f"{RUNS}/{uuid.uuid7()}/publish", headers=headers),
        http.get(SOURCES, headers=headers),
        http.get(f"{SOURCES}/{world.source_snapshot_id}", headers=headers),
        http.post(SOURCES, headers=headers, json=submission(voltedge_document(), FIRST_KEY)),
    ]

    assert [answer.status_code for answer in answers] == [401] * 7
    # Byte for byte the refusal an unknown credential gets, so which of the two it is says
    # nothing about the credential.
    assert answers[0].json() == {
        "error": "unauthenticated",
        "detail": "a valid merchant API credential is required",
        "resource": None,
        "identifier": None,
    }


async def test_starting_a_run_is_authenticated(
    settings: Settings,
    session: AsyncSession,
    factory: async_sessionmaker[AsyncSession],
) -> None:
    _, snapshot = await merchant_with_source(session, "launch-anonymous-shop")
    http = client(settings, factory)

    answer = http.post(RUNS, json={"source_snapshot_id": str(snapshot.id)})

    assert answer.status_code == 401
    assert await _run_count(session, snapshot.id) == 0


async def _run_count(session: AsyncSession, source_snapshot_id: uuid.UUID) -> int:
    return int(
        (
            await session.execute(
                select(func.count()).where(CompilerRun.source_snapshot_id == source_snapshot_id)
            )
        ).scalar_one()
    )
