"""Refusing a request body this application will not read, before anything reads it.

Every other bound in this application is a bound on meaning: a title is at most two hundred
characters, a document describes at most fifty products, a suite holds at most two hundred
missions. Those are checked by the schema, which is the right layer for them and the wrong layer
for the two here. By the time a schema sees a body, the body has been read and parsed, and both
of these are about what reading and parsing one costs.

```text
size     a gigabyte of whitespace costs a gigabyte to discover it is not a source document
depth    a document nested tens of thousands deep is work nobody asked for, at every layer
         that walks it: the parser, the validator, and whatever renders the refusal
```

The depth bound is not the fix for any one crash. The crash a deep body used to cause was in the
framework's own validation error handler, which serialized the offending value and recursed once
per level of it; that is fixed centrally in `agentrank_api.main` for every route with a body, and
this bound would have been the wrong place for it because only one path has a size bound to make
a depth bound reachable from. What this is instead is a statement about the one body a caller
composes freely: a source document is seven levels deep by construction, twelve is generous, and
anything past that is not a source document and does not need to be parsed to find out.

So this sits in front of routing, as ASGI middleware. It refuses an oversized declared length
before receiving a byte, and it drains and inspects the body it does accept before handing it on,
because a declared length is a claim and the bytes are the fact.

Requiring the declaration is what makes checking it sufficient rather than merely helpful. A
chunked request declares no length, and refusing one with `411 Length Required` is safe here
because every caller of a bounded path is a browser or a client posting a JSON document and both
declare a length.

Two bounds and they are different kinds of statement. A path that names one is a body this
repository has thought about, and the number is what that body is: a source document is 128 KiB
because that is what a source document is. Everything else that carries a body takes `default`,
which is not a considered figure for any particular route and is not meant to be. It exists
because the alternative to a generous default is no bound at all, and "a route nobody considered"
is exactly the route that needs one: an authenticated merchant posting half a gigabyte to
`/api/v1/commerce/checkouts` had every byte received and parsed before any schema could refuse it.

The body shape matches `agentrank_api.errors.ErrorResponse`, because a caller that parses one
refusal should not need a second parser for this one.
"""

import json
from collections.abc import Mapping
from dataclasses import dataclass

from starlette.types import ASGIApp, Message, Receive, Scope, Send

LENGTH_REQUIRED = 411
PAYLOAD_TOO_LARGE = 413
UNPROCESSABLE_CONTENT = 422

_OPEN = frozenset(b"[{")
_CLOSE = frozenset(b"]}")
_QUOTE = ord('"')
_BACKSLASH = ord("\\")


@dataclass(frozen=True, slots=True)
class BodyLimit:
    """What one bounded path will accept, in bytes and in nesting levels."""

    max_bytes: int
    max_depth: int


# The methods this bounds. A body on any other method is not something this application reads, and
# checking one would be refusing a request over bytes nothing would have looked at.
BODIED_METHODS = frozenset({"POST", "PUT", "PATCH"})


class RequestBodyLimit:
    """Bound the size and the nesting of a request body, by path where one is named."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        limits: Mapping[tuple[str, str], BodyLimit],
        default: BodyLimit | None = None,
    ) -> None:
        self._app = app
        self._limits = dict(limits)
        self._default = default

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        method = str(scope.get("method", ""))
        limit = self._limits.get((method, _path(scope)))
        if limit is None and method in BODIED_METHODS:
            limit = self._default
        if limit is None:
            await self._app(scope, receive, send)
            return

        declared = _declared_length(scope)
        if declared is None:
            await _refuse(
                send,
                LENGTH_REQUIRED,
                "length_required",
                "this request must declare its content length",
            )
            return
        if declared > limit.max_bytes:
            await _refuse(
                send,
                PAYLOAD_TOO_LARGE,
                "request_too_large",
                f"this request may be at most {limit.max_bytes} bytes",
            )
            return

        body = await _drain(receive, limit.max_bytes)
        if body is None:
            await _refuse(
                send,
                PAYLOAD_TOO_LARGE,
                "request_too_large",
                f"this request may be at most {limit.max_bytes} bytes",
            )
            return
        if not _within_depth(body, limit.max_depth):
            await _refuse(
                send,
                UNPROCESSABLE_CONTENT,
                "request_too_deeply_nested",
                f"this request may nest at most {limit.max_depth} levels",
            )
            return
        await self._app(scope, _replay(body), send)


def _path(scope: Scope) -> str:
    """The path Starlette will route on, with one trailing slash removed.

    `root_path` is stripped because that is what routing does, and a bound keyed on the path a
    route is registered under has to be compared with the same string. Getting this wrong fails
    open: an unrecognised key means no bound rather than no route, so nothing would break loudly.
    """
    path = str(scope.get("path", ""))
    root = str(scope.get("root_path", ""))
    if root and path.startswith(root):
        path = path[len(root) :] or "/"
    return path[:-1] if len(path) > 1 and path.endswith("/") else path


def _declared_length(scope: Scope) -> int | None:
    """The declared body length, or None when there is not exactly one readable one."""
    values = [
        value for name, value in scope.get("headers", []) if name.lower() == b"content-length"
    ]
    if len(values) != 1:
        return None
    try:
        length = int(values[0])
    except ValueError:
        return None
    return length if length >= 0 else None


async def _drain(receive: Receive, max_bytes: int) -> bytes | None:
    """Read the body, or None if it turns out to be larger than it declared.

    A client that disconnects mid-body ends the read and what it sent so far is handed on, so the
    application sees a short body and refuses it as one rather than raising a disconnect. Nothing
    is at stake: a truncation can only make a body smaller and shallower, and the refusal goes to
    a client that is no longer there.
    """
    chunks: list[bytes] = []
    total = 0
    while True:
        message = await receive()
        if message["type"] != "http.request":
            break
        chunk = bytes(message.get("body", b""))
        total += len(chunk)
        if total > max_bytes:
            return None
        chunks.append(chunk)
        if not message.get("more_body", False):
            break
    return b"".join(chunks)


def _within_depth(body: bytes, max_depth: int) -> bool:
    """Whether the brackets in this body nest no deeper than the bound.

    A byte scan rather than a parse, because parsing is the thing this exists to make safe.
    Brackets inside a JSON string are text, so the scan tracks quoting and escaping; anything
    else about the body is somebody else's problem and this deliberately does not judge it.
    """
    depth = 0
    in_string = False
    escaped = False
    for byte in body:
        if in_string:
            if escaped:
                escaped = False
            elif byte == _BACKSLASH:
                escaped = True
            elif byte == _QUOTE:
                in_string = False
            continue
        if byte == _QUOTE:
            in_string = True
        elif byte in _OPEN:
            depth += 1
            if depth > max_depth:
                return False
        elif byte in _CLOSE:
            # Floored, so a body that closes more brackets than it opens cannot buy itself
            # headroom. The parser refuses such a body anyway; a bound that can be talked down
            # is not a bound, and this is one line.
            depth = max(depth - 1, 0)
    return True


def _replay(body: bytes) -> Receive:
    """Hand the drained body to the application exactly once, then report disconnection."""
    sent = False

    async def receive() -> Message:
        nonlocal sent
        if sent:
            return {"type": "http.disconnect"}
        sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    return receive


async def _refuse(send: Send, status: int, reason: str, detail: str) -> None:
    """Answer without calling the application, in this repository's own error shape.

    A 422 carries an empty `fields`, because this application's OpenAPI document declares
    `InvalidRequestResponse` as the 422 model for every operation and a client generated from it
    would fail to decode a body missing the field. There is nothing to put in it: this refusal is
    about the body as a whole rather than about anything inside it, which is exactly why it can
    be made without parsing one.
    """
    payload: dict[str, object] = {
        "error": reason,
        "detail": detail,
        "resource": None,
        "identifier": None,
    }
    if status == UNPROCESSABLE_CONTENT:
        payload["fields"] = []
    body = json.dumps(payload)
    encoded = body.encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(encoded)).encode("ascii")),
            ],
        }
    )
    await send({"type": "http.response.body", "body": encoded})
