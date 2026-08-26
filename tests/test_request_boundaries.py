"""What this API does with a request body it cannot read.

Two claims, and both are about the layer underneath every schema in this repository.

A body that is wrong is a refusal and never a failure. The framework's own answer to a validation
error encodes the offending value, which recurses once per nesting level of whatever arrived,
inside an exception handler that no `try` in the request path wraps. A body of a few hundred
nested brackets therefore left this application answering 500 to a request that was merely wrong,
on every route with a body schema.

An authenticated caller, to be exact: body field errors are collected inside dependency solving
and after the dependencies themselves run, so an anonymous caller is refused before a validation
error exists to be rendered. Every route here takes a merchant credential and a merchant is not a
trusted caller, so the narrower blast radius is not a smaller bug. The handler in
`agentrank_api.main` answers without encoding the value, and these are the tests that hold it
there, including that the answer never grows with the body.

And the merchant source path, which is the one body a caller composes freely, is bounded in size
and in nesting before anything parses it. Those bounds are exercised in `test_source_api.py`
against the real route; what is here is the middleware's own reasoning, unit tested, because a
bound that fails open is indistinguishable from a bound that passes.
"""

import uuid
from collections.abc import MutableMapping
from typing import Any

import pytest
from conftest import CredentialIssuer, bearer
from fastapi.testclient import TestClient
from source_support import merchant_with_source
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.types import Receive, Scope, Send

from agentrank_api.config import Settings
from agentrank_api.errors import MAX_FIELD_LOCATION_PART_LENGTH, MAX_INVALID_FIELDS
from agentrank_api.limits import BodyLimit, RequestBodyLimit, _path, _within_depth
from agentrank_api.main import create_app
from agentrank_api.payments.fake import FakePaymentProvider

pytestmark = pytest.mark.anyio

RUNS = "/api/v1/compiler/runs"
SOURCES = "/api/v1/sources"
IMPORTS = "/api/v1/sources/imports"

# Deep enough that the framework's own handler answered 500, and far past the depth any schema in
# this repository describes. The body is under three kilobytes.
CRASHING_DEPTH = 1_000


def client(settings: Settings, sessions: async_sessionmaker[AsyncSession]) -> TestClient:
    app = create_app(settings, payment_provider=FakePaymentProvider())
    app.state.session_factory = sessions
    return TestClient(app)


def nested(field: str, depth: int) -> str:
    return '{"' + field + '": ' + "[" * depth + "]" * depth + "}"


async def test_a_deeply_nested_body_is_a_refusal_on_every_route_with_a_schema(
    settings: Settings,
    session: AsyncSession,
    factory: async_sessionmaker[AsyncSession],
    issue_credential: CredentialIssuer,
) -> None:
    merchant, _ = await merchant_with_source(session, "boundary-shop")
    token = await issue_credential(merchant.id)
    http = client(settings, factory)
    headers = {**bearer(token), "Content-Type": "application/json"}
    candidate = uuid.uuid7()

    answers = {
        "compiler run": http.post(
            RUNS, headers=headers, content=nested("source_snapshot_id", CRASHING_DEPTH)
        ),
        "correction": http.post(
            f"/api/v1/compiler/candidates/{candidate}/correct",
            headers=headers,
            content=nested("value", CRASHING_DEPTH),
        ),
    }

    assert {name: answer.status_code for name, answer in answers.items()} == dict.fromkeys(
        answers, 422
    )
    for name, answer in answers.items():
        body = answer.json()
        # One 422 shape whichever layer refused. A body past the declared nesting bound is
        # refused by the middleware before anything parses it; one inside the bound and wrong
        # in some other way is refused by the validation handler. Both carry `error`, `detail`
        # and `fields`, because the OpenAPI document declares one 422 model for every operation.
        assert body["error"] in {"invalid_request", "request_too_deeply_nested"}, name
        assert isinstance(body["detail"], str), name
        assert isinstance(body["fields"], list), name
        # The value is never encoded, so a body of a thousand brackets does not become a
        # response of a thousand brackets.
        assert "[[[" not in answer.text, name
        assert len(answer.text) < 4096, name


async def test_a_deeply_nested_body_is_refused_before_authentication(
    settings: Settings,
    session: AsyncSession,
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """An anonymous caller never reached the crash, and still must not reach a 500.

    The bound is in front of routing, so it is also in front of authentication, and that is the
    same rule the merchant source path has always answered under. What an anonymous caller learns
    is a fact about the request they sent rather than anything about this merchant, this
    deployment or what exists at the address: the same refusal, byte for byte, whether the path
    they aimed it at exists or not.
    """
    http = client(settings, factory)

    real = http.post(
        RUNS,
        headers={"Content-Type": "application/json"},
        content=nested("source_snapshot_id", CRASHING_DEPTH),
    )
    invented = http.post(
        "/api/v1/no-such-route",
        headers={"Content-Type": "application/json"},
        content=nested("source_snapshot_id", CRASHING_DEPTH),
    )

    assert real.status_code == 422
    assert real.json()["error"] == "request_too_deeply_nested"
    assert real.json()["fields"] == []
    assert invented.text == real.text


async def test_a_validation_refusal_names_the_field_and_echoes_no_value(
    settings: Settings,
    session: AsyncSession,
    factory: async_sessionmaker[AsyncSession],
    issue_credential: CredentialIssuer,
) -> None:
    merchant, _ = await merchant_with_source(session, "boundary-fields-shop")
    token = await issue_credential(merchant.id)
    http = client(settings, factory)

    answer = http.post(
        RUNS, headers=bearer(token), json={"source_snapshot_id": "not-a-uuid-at-all"}
    )

    assert answer.status_code == 422
    body = answer.json()
    assert body["error"] == "invalid_request"
    assert body["fields"] == [
        {
            "location": ["body", "source_snapshot_id"],
            "message": body["fields"][0]["message"],
        }
    ]
    assert "source_snapshot_id" in body["detail"]
    # The caller's own value would leak nothing, and it is still not repeated: a refusal that
    # quoted the body would grow with the body.
    assert "not-a-uuid-at-all" not in answer.text


async def test_a_refusal_does_not_grow_with_the_body_that_caused_it(
    settings: Settings,
    session: AsyncSession,
    factory: async_sessionmaker[AsyncSession],
    issue_credential: CredentialIssuer,
) -> None:
    """An unexpected field puts the caller's own key name into the location it is refused at.

    Bounding the number of location parts is not enough, because the part itself is whatever the
    caller called it. Unbounded, a megabyte of key name comes back twice: once in the location and
    once in the sentence built from it.
    """
    merchant, _ = await merchant_with_source(session, "boundary-amplify-shop")
    token = await issue_credential(merchant.id)
    http = client(settings, factory)
    invented = "K" * 10_000
    body = f'{{"source_snapshot_id": "{uuid.uuid7()}", "{invented}": 1}}'

    answer = http.post(
        RUNS, headers={**bearer(token), "Content-Type": "application/json"}, content=body
    )

    assert answer.status_code == 422
    assert len(answer.text) < 2048
    assert invented not in answer.text


async def test_a_validation_refusal_is_bounded_however_many_fields_are_wrong(
    settings: Settings,
    session: AsyncSession,
    factory: async_sessionmaker[AsyncSession],
    issue_credential: CredentialIssuer,
) -> None:
    merchant, _ = await merchant_with_source(session, "boundary-many-shop")
    token = await issue_credential(merchant.id)
    http = client(settings, factory)
    body = {
        "request_key": "many-wrong-fields",
        "policy_text": {},
        "products": [
            {
                "external_id": f"P{index}",
                "title": "",
                "description": None,
                "category": None,
                "variants": [],
                "merchant_metadata": {},
            }
            for index in range(60)
        ],
    }

    answer = http.post(SOURCES, headers=bearer(token), json=body)

    assert answer.status_code == 422
    assert len(answer.json()["fields"]) <= MAX_INVALID_FIELDS


def test_the_depth_scan_reads_brackets_and_not_text() -> None:
    assert _within_depth(b'{"a": "[[[[[[[[[[[[[[[[[[[["}', 3)
    assert _within_depth(rb'{"a": "\""}', 3)
    assert not _within_depth(b"[[[[]]]]", 3)
    assert _within_depth(b"[[[]]]", 3)


def test_the_depth_scan_cannot_be_talked_down_by_closing_brackets_first() -> None:
    """A floored counter, so a leading run of closers buys no headroom."""
    assert not _within_depth(b"]" * 50 + b"[" * 20 + b"]" * 20, 3)


def test_the_bounded_path_is_the_path_routing_will_use() -> None:
    """A bound keyed on a route's own path has to be compared with the same string."""
    assert _path({"path": "/api/v1/sources"}) == "/api/v1/sources"
    assert _path({"path": "/api/v1/sources/"}) == "/api/v1/sources"
    assert _path({"path": "/gw/api/v1/sources", "root_path": "/gw"}) == "/api/v1/sources"
    assert _path({"path": "/gw/api/v1/sources/", "root_path": "/gw"}) == "/api/v1/sources"
    assert _path({"path": "/other"}) == "/other"


async def test_the_body_limit_leaves_every_other_path_alone() -> None:
    """Only a path that names a bound is checked, and a checked path is checked completely."""
    seen: list[bytes] = []
    sent: list[MutableMapping[str, Any]] = []

    async def application(scope: Scope, receive: Receive, send: Send) -> None:
        del scope, send
        message = await receive()
        seen.append(bytes(message["body"]))

    async def send(message: MutableMapping[str, Any]) -> None:
        sent.append(message)

    async def receive() -> MutableMapping[str, Any]:
        return {"type": "http.request", "body": b'{"a": 1}', "more_body": False}

    middleware = RequestBodyLimit(
        application, limits={("POST", "/bounded"): BodyLimit(max_bytes=4, max_depth=2)}
    )
    await middleware(
        {"type": "http", "method": "POST", "path": "/unbounded", "headers": []}, receive, send
    )
    assert seen == [b'{"a": 1}']
    assert sent == []

    await middleware(
        {
            "type": "http",
            "method": "POST",
            "path": "/bounded",
            "headers": [(b"content-length", b"8")],
        },
        receive,
        send,
    )
    assert sent[0]["status"] == 413


async def test_a_body_naming_a_field_a_schema_lacks_is_refused_rather_than_ignored(
    settings: Settings,
    session: AsyncSession,
    factory: async_sessionmaker[AsyncSession],
    issue_credential: CredentialIssuer,
) -> None:
    """Every merchant-facing command refuses an unknown field, including the ones about money.

    A schema that ignores unknown fields answers a caller yes and then does something else. Two
    of these were live hazards rather than tidiness: a body spelling `maxQuantity` created a
    mandate with no quantity ceiling at all and was told 201, and a body spelling
    `idempotencyKey` created a payment with no idempotency key, so the caller's retry after a
    lost response became a second operation rather than the same one.
    """
    merchant, _ = await merchant_with_source(session, "unknown-field-shop")
    token = await issue_credential(merchant.id)
    http = client(settings, factory)
    headers = {**bearer(token), "Content-Type": "application/json"}

    answers = {
        "mandate": http.post(
            "/api/v1/commerce/mandates",
            headers=headers,
            json={
                "maxQuantity": 3,
                "max_total_amount_minor": 5000,
                "currency": "INR",
                "valid_until": "2030-01-01T00:00:00Z",
            },
        ),
        "checkout": http.post(
            "/api/v1/commerce/checkouts",
            headers=headers,
            json={"merchant_id": str(merchant.id), "items": []},
        ),
        "search": http.post(
            "/api/v1/commerce/products/search", headers=headers, json={"merchantSlug": "x"}
        ),
    }

    assert {name: answer.status_code for name, answer in answers.items()} == dict.fromkeys(
        answers, 422
    )
    for name, answer in answers.items():
        assert answer.json()["error"] == "invalid_request", name


async def test_a_route_refusal_carries_the_error_contract_every_other_one_does(
    settings: Settings,
    session: AsyncSession,
    factory: async_sessionmaker[AsyncSession],
    issue_credential: CredentialIssuer,
) -> None:
    """A 422 a route decides is the same shape as a 422 the framework decides.

    These used to be raised as a bare `HTTPException`, whose body carries only `detail`: no
    `error` code and no `fields`, and nothing a client generated from this application's own
    OpenAPI document could decode, because that document declares one 422 model for every
    operation.
    """
    merchant, _ = await merchant_with_source(session, "refusal-shape-shop")
    token = await issue_credential(merchant.id)
    http = client(settings, factory)
    duplicated = {
        "request_key": "duplicate-sku",
        "products": [
            {
                "external_id": "P1",
                "title": "One",
                "variants": [
                    {
                        "sku": "SAME",
                        "price_amount_minor": 100,
                        "currency": "INR",
                        "availability": "IN_STOCK",
                    }
                ],
            },
            {
                "external_id": "P2",
                "title": "Two",
                "variants": [
                    {
                        "sku": "SAME",
                        "price_amount_minor": 100,
                        "currency": "INR",
                        "availability": "IN_STOCK",
                    }
                ],
            },
        ],
    }

    answer = http.post(SOURCES, headers=bearer(token), json=duplicated)

    assert answer.status_code == 422
    body = answer.json()
    assert body["error"] == "invalid_request"
    assert "unique" in body["detail"]
    assert body["fields"] == []
    # A domain refusal is this repository's own prose, and it stays bounded even so.
    assert len(body["detail"]) <= 400


async def test_a_refusal_a_route_built_is_bounded_the_same_way(
    settings: Settings,
    session: AsyncSession,
    factory: async_sessionmaker[AsyncSession],
    issue_credential: CredentialIssuer,
) -> None:
    """The one place a caller's own string reaches a location part on purpose.

    The merchant import route puts the refused URL into the field location, so a merchant who
    pasted twelve addresses learns which one AgentRank will not fetch. That is exactly the shape
    the location bounds exist for, and they used to apply only to the framework's own validation
    errors: a hand-built field went out whatever length the route gave it.
    """
    merchant, _ = await merchant_with_source(session, "refusal-bound-shop")
    token = await issue_credential(merchant.id)
    http = client(settings, factory)
    long = "http://169.254.169.254/" + "a" * 300

    answer = http.post(
        IMPORTS,
        headers=bearer(token),
        json={
            "request_key": "bounded-refusal",
            "pages": [{"url": long, "kind": "PRODUCT", "name": None}],
        },
    )

    assert answer.status_code == 422
    body = answer.json()
    assert body["error"] == "invalid_request"
    # The refusal names a URL and does not carry the whole of one.
    assert "169.254.169.254" in answer.text
    assert "a" * 100 not in answer.text
    assert all(
        len(part) <= MAX_FIELD_LOCATION_PART_LENGTH for part in body["fields"][0]["location"]
    )
