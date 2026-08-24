"""Refusing an oversized request body before anything reads it.

Every other bound in this application is a bound on meaning: a title is at most two hundred
characters, a document describes at most fifty products, a suite holds at most two hundred
missions. Those are checked by the schema, which is the right layer for them and the wrong layer
for this one. By the time a schema sees a body, the body has been read, and a body that is a
gigabyte of whitespace has already cost a gigabyte of memory to discover that it is not a source
document.

So this sits in front of routing, as ASGI middleware, and looks at one header. A request to a
bounded path must declare its length and that length must be within the bound, and both refusals
happen before a single byte of the body is received.

Requiring the declaration is what makes checking it sufficient. A chunked request declares no
length, so the header check would pass it through unbounded; refusing one instead is safe here
because every caller of a bounded path is a browser or a client posting a JSON document, and
both declare a length. `411 Length Required` is exactly what the status exists for.

Deliberately narrow. Only paths that name a bound are checked, so this cannot quietly become the
place other endpoints' limits are decided, and there is no default that would apply to a route
nobody considered. The body shape matches `agentrank_api.errors.ErrorResponse`, because a caller
that parses one refusal should not need a second parser for this one.
"""

import json
from collections.abc import Mapping

from starlette.types import ASGIApp, Receive, Scope, Send

LENGTH_REQUIRED = 411
PAYLOAD_TOO_LARGE = 413


class RequestBodyLimit:
    """Bound the declared length of a request body on the paths that name a bound."""

    def __init__(self, app: ASGIApp, *, limits: Mapping[tuple[str, str], int]) -> None:
        self._app = app
        self._limits = dict(limits)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        limit = self._limits.get((scope.get("method", ""), scope.get("path", "")))
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
        if declared > limit:
            await _refuse(
                send,
                PAYLOAD_TOO_LARGE,
                "request_too_large",
                f"this request may be at most {limit} bytes",
            )
            return
        await self._app(scope, receive, send)


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


async def _refuse(send: Send, status: int, reason: str, detail: str) -> None:
    """Answer without calling the application, in this repository's own error shape."""
    body = json.dumps({"error": reason, "detail": detail, "resource": None, "identifier": None})
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
