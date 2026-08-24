"""What this API does with a request body it cannot read.

Two claims, and both are about the layer underneath every schema in this repository.

A body that is wrong is a refusal and never a failure. The framework's own answer to a validation
error encodes the offending value, which recurses once per nesting level of whatever arrived,
inside an exception handler that no `try` in the request path wraps. A body of a few hundred
nested brackets therefore left this application answering 500 to a request that was merely wrong,
on every route with a body schema and before authentication ran. The handler in
`agentrank_api.main` answers without encoding the value, and these are the tests that hold it
there.

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
from agentrank_api.limits import BodyLimit, RequestBodyLimit, _path, _within_depth
from agentrank_api.main import MAX_INVALID_FIELDS, create_app
from agentrank_api.payments.fake import FakePaymentProvider

pytestmark = pytest.mark.anyio

RUNS = "/api/v1/compiler/runs"
SOURCES = "/api/v1/sources"

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
        assert body["error"] == "invalid_request", name
        assert isinstance(body["detail"], str), name
        assert body["fields"], name
        # The value is never encoded, so a body of a thousand brackets does not become a
        # response of a thousand brackets.
        assert "[[[" not in answer.text, name
        assert len(answer.text) < 4096, name


async def test_a_deeply_nested_body_is_refused_before_authentication(
    settings: Settings,
    session: AsyncSession,
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Body validation runs before dependencies, so an anonymous caller reached the same crash."""
    http = client(settings, factory)

    answer = http.post(
        RUNS,
        headers={"Content-Type": "application/json"},
        content=nested("source_snapshot_id", CRASHING_DEPTH),
    )

    # 401 rather than 422, because refusing to say anything about a request from a caller who
    # has not said who they are is the older rule and it still wins. What matters is that it is
    # not a 500, and that the answer is byte for byte the one any unauthenticated request gets.
    assert answer.status_code == 401
    assert answer.json() == {
        "error": "unauthenticated",
        "detail": "a valid merchant API credential is required",
        "resource": None,
        "identifier": None,
    }


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
