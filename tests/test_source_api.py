"""The merchant source HTTP boundary: what it accepts, refuses, and never lets a browser choose.

A source document is the one body this API accepts that a caller composes freely, so this file is
mostly about refusals. Each one is asserted with the smallest possible change to a document that
is otherwise valid, so a refusal is provably about the thing it names rather than about the
document being wrong in some other way as well.
"""

import json
from typing import Any

import pytest
from conftest import CredentialIssuer, bearer
from fastapi.testclient import TestClient
from source_support import (
    FIRST_KEY,
    SECOND_KEY,
    bare_merchant,
    contradicted_document,
    merchant_with_source,
    submission,
    voltedge_document,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agentrank_api.config import Settings
from agentrank_api.main import create_app
from agentrank_api.payments.fake import FakePaymentProvider
from agentrank_api.representation.schemas import MAX_SOURCE_REQUEST_BYTES

pytestmark = pytest.mark.anyio

SOURCES = "/api/v1/sources"


def client(settings: Settings, sessions: async_sessionmaker[AsyncSession]) -> TestClient:
    app = create_app(settings, payment_provider=FakePaymentProvider())
    app.state.session_factory = sessions
    return TestClient(app)


def refused(response: Any) -> str:
    return json.dumps(response.json())


async def test_a_merchant_submits_newer_evidence_and_reads_its_own_history(
    settings: Settings,
    session: AsyncSession,
    factory: async_sessionmaker[AsyncSession],
    issue_credential: CredentialIssuer,
) -> None:
    merchant, first = await merchant_with_source(session, "api-source-shop")
    token = await issue_credential(merchant.id)
    http = client(settings, factory)

    created = http.post(
        SOURCES, headers=bearer(token), json=submission(contradicted_document(), FIRST_KEY)
    )
    assert created.status_code == 201
    body = created.json()
    assert body["created_snapshot"] is True
    assert body["request_key"] == FIRST_KEY
    assert body["snapshot"]["source_version"] == 2
    assert body["snapshot"]["origin"] == "MERCHANT_CONSOLE"
    assert body["snapshot"]["is_current"] is True

    overview = http.get(SOURCES, headers=bearer(token))
    assert overview.status_code == 200
    listed = overview.json()
    assert listed["current_source_snapshot_id"] == body["snapshot"]["source_snapshot_id"]
    assert [entry["source_version"] for entry in listed["snapshots"]] == [2, 1]
    # A history row carries identity and size, never the document it summarizes.
    assert all("document" not in entry for entry in listed["snapshots"])

    detail = http.get(f"{SOURCES}/{body['snapshot']['source_snapshot_id']}", headers=bearer(token))
    assert detail.status_code == 200
    read = detail.json()
    assert set(read["document"]) == {"products", "policy_text"}
    assert "merchant_slug" not in read["document"]
    assert read["compilable"] is True
    assert read["existing_run_id"] is None
    assert read["compiler_runs"] == []
    addresses = {entry["field"] for entry in read["fields"]}
    assert "products[VE-CHG-100].description" in addresses
    assert "policy_text.warranty" in addresses

    # The snapshot the operator published is still exactly what it was.
    original = http.get(f"{SOURCES}/{first.id}", headers=bearer(token))
    assert original.status_code == 200
    assert original.json()["summary"]["origin"] == "OPERATOR_FIXTURE"
    assert original.json()["summary"]["is_current"] is False


async def test_a_repeat_of_one_submission_is_the_same_submission(
    settings: Settings,
    session: AsyncSession,
    factory: async_sessionmaker[AsyncSession],
    issue_credential: CredentialIssuer,
) -> None:
    merchant, _ = await merchant_with_source(session, "api-retry-shop")
    token = await issue_credential(merchant.id)
    http = client(settings, factory)
    body = submission(contradicted_document(), FIRST_KEY)

    first = http.post(SOURCES, headers=bearer(token), json=body)
    repeat = http.post(SOURCES, headers=bearer(token), json=body)

    assert (first.status_code, repeat.status_code) == (201, 201)
    assert repeat.json() == first.json()
    assert len(http.get(SOURCES, headers=bearer(token)).json()["snapshots"]) == 2


async def test_resubmitting_unchanged_evidence_says_so_rather_than_writing_a_copy(
    settings: Settings,
    session: AsyncSession,
    factory: async_sessionmaker[AsyncSession],
    issue_credential: CredentialIssuer,
) -> None:
    merchant, first = await merchant_with_source(session, "api-unchanged-shop")
    token = await issue_credential(merchant.id)
    http = client(settings, factory)

    answer = http.post(
        SOURCES, headers=bearer(token), json=submission(voltedge_document(), SECOND_KEY)
    )

    assert answer.status_code == 201
    assert answer.json()["created_snapshot"] is False
    assert answer.json()["snapshot"]["source_snapshot_id"] == str(first.id)
    assert len(http.get(SOURCES, headers=bearer(token)).json()["snapshots"]) == 1


async def test_a_browser_cannot_choose_the_merchant_the_source_line_or_the_version(
    settings: Settings,
    session: AsyncSession,
    factory: async_sessionmaker[AsyncSession],
    issue_credential: CredentialIssuer,
) -> None:
    merchant, _ = await merchant_with_source(session, "api-identity-shop")
    other = await bare_merchant(session, "api-victim-shop")
    token = await issue_credential(merchant.id)
    http = client(settings, factory)

    for field, value in (
        ("merchant_slug", other.slug),
        ("merchant_id", str(other.id)),
        ("key", "somebody-elses-source"),
        ("version", 99),
        ("content_hash", "sha256:" + "0" * 64),
        ("origin", "OPERATOR_FIXTURE"),
    ):
        smuggled = submission(contradicted_document(), FIRST_KEY)
        smuggled[field] = value
        answer = http.post(SOURCES, headers=bearer(token), json=smuggled)
        assert answer.status_code == 422, field
        assert "extra" in refused(answer).lower(), field

    # Nothing was written for either merchant while every one of those was refused.
    assert len(http.get(SOURCES, headers=bearer(token)).json()["snapshots"]) == 1


async def test_a_malformed_document_is_refused_field_by_field(
    settings: Settings,
    session: AsyncSession,
    factory: async_sessionmaker[AsyncSession],
    issue_credential: CredentialIssuer,
) -> None:
    merchant, _ = await merchant_with_source(session, "api-malformed-shop")
    token = await issue_credential(merchant.id)
    http = client(settings, factory)

    def refuse(mutate: Any) -> Any:
        body = submission(contradicted_document(), FIRST_KEY)
        mutate(body)
        return http.post(SOURCES, headers=bearer(token), json=body)

    def set_variant(body: Any, field: str, value: Any) -> None:
        body["products"][0]["variants"][0][field] = value

    refusals = {
        "no products": refuse(lambda body: body.update(products=[])),
        "no variants": refuse(lambda body: body["products"][0].update(variants=[])),
        "blank title": refuse(lambda body: body["products"][0].update(title="")),
        "long title": refuse(lambda body: body["products"][0].update(title="x" * 201)),
        "string price": refuse(lambda body: set_variant(body, "price_amount_minor", "499900")),
        "negative price": refuse(lambda body: set_variant(body, "price_amount_minor", -1)),
        "float price": refuse(lambda body: set_variant(body, "price_amount_minor", 1.5)),
        "bad currency": refuse(lambda body: set_variant(body, "currency", "rupees")),
        "negative stock": refuse(lambda body: set_variant(body, "inventory_quantity", -3)),
        "dotted sku": refuse(lambda body: set_variant(body, "sku", "VE.CHG.100")),
        "bracketed id": refuse(lambda body: body["products"][0].update(external_id="VE[1]")),
        "nested metadata": refuse(
            lambda body: set_variant(body, "merchant_metadata", {"finish": {"deep": 1}})
        ),
        "blank policy": refuse(lambda body: body["policy_text"].update(warranty="   ")),
        "bad policy name": refuse(lambda body: body["policy_text"].update(**{"a b": "text"})),
        "short request key": refuse(lambda body: body.update(request_key="short")),
        "missing request key": refuse(lambda body: body.pop("request_key")),
    }

    assert {name: answer.status_code for name, answer in refusals.items()} == dict.fromkeys(
        refusals, 422
    )
    assert len(http.get(SOURCES, headers=bearer(token)).json()["snapshots"]) == 1


async def test_a_document_that_is_well_typed_and_not_a_source_document_is_refused(
    settings: Settings,
    session: AsyncSession,
    factory: async_sessionmaker[AsyncSession],
    issue_credential: CredentialIssuer,
) -> None:
    """Duplicate identifiers pass every field bound and still cannot address anything."""
    merchant, _ = await merchant_with_source(session, "api-duplicate-shop")
    token = await issue_credential(merchant.id)
    http = client(settings, factory)
    body = submission(contradicted_document(), FIRST_KEY)
    body["products"][1]["variants"][0]["sku"] = body["products"][0]["variants"][0]["sku"]

    answer = http.post(SOURCES, headers=bearer(token), json=body)

    assert answer.status_code == 422
    assert "unique" in refused(answer)


async def test_an_oversized_document_is_refused_before_it_is_read(
    settings: Settings,
    session: AsyncSession,
    factory: async_sessionmaker[AsyncSession],
    issue_credential: CredentialIssuer,
) -> None:
    merchant, _ = await merchant_with_source(session, "api-oversized-shop")
    token = await issue_credential(merchant.id)
    http = client(settings, factory)
    body = submission(contradicted_document(), FIRST_KEY)
    body["products"][0]["description"] = "x" * MAX_SOURCE_REQUEST_BYTES

    answer = http.post(SOURCES, headers=bearer(token), json=body)

    assert answer.status_code == 413
    assert answer.json()["error"] == "request_too_large"
    assert len(http.get(SOURCES, headers=bearer(token)).json()["snapshots"]) == 1


async def test_a_document_that_declares_no_length_is_refused(
    settings: Settings,
    session: AsyncSession,
    factory: async_sessionmaker[AsyncSession],
    issue_credential: CredentialIssuer,
) -> None:
    """A body with no declared length cannot be bounded by looking at the declaration."""
    merchant, _ = await merchant_with_source(session, "api-chunked-shop")
    token = await issue_credential(merchant.id)
    http = client(settings, factory)
    encoded = json.dumps(submission(contradicted_document(), FIRST_KEY)).encode()

    answer = http.post(
        SOURCES,
        headers={
            **bearer(token),
            "Content-Type": "application/json",
            "Transfer-Encoding": "chunked",
        },
        content=iter([encoded]),
    )

    assert answer.status_code == 411
    assert answer.json()["error"] == "length_required"


async def test_the_source_namespace_is_authenticated_and_merchant_scoped(
    settings: Settings,
    session: AsyncSession,
    factory: async_sessionmaker[AsyncSession],
    issue_credential: CredentialIssuer,
) -> None:
    merchant, snapshot = await merchant_with_source(session, "api-scope-shop")
    _, foreign_snapshot = await merchant_with_source(session, "api-foreign-shop", name="Foreign")
    token = await issue_credential(merchant.id)
    http = client(settings, factory)

    unauthenticated = [
        http.get(SOURCES),
        http.get(f"{SOURCES}/{snapshot.id}"),
        http.post(SOURCES, json=submission(contradicted_document(), FIRST_KEY)),
    ]
    assert [answer.status_code for answer in unauthenticated] == [401] * 3

    foreign = http.get(f"{SOURCES}/{foreign_snapshot.id}", headers=bearer(token))
    assert foreign.status_code == 404

    answered = [
        http.get(SOURCES, headers=bearer(token)),
        http.get(f"{SOURCES}/{snapshot.id}", headers=bearer(token)),
        http.post(
            SOURCES, headers=bearer(token), json=submission(contradicted_document(), FIRST_KEY)
        ),
    ]
    joined = "\n".join(answer.text for answer in answered)
    assert [answer.status_code for answer in answered] == [200, 200, 201]
    assert token not in joined
    assert token.split("_")[-1] not in joined
    assert "secret_hash" not in joined
