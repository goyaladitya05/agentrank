"""Opening and closing a merchant console browser session.

Three operations and no more. The console opens a session by presenting the merchant API key
once, reads back when the session it holds expires, and closes it on sign-out. Everything else a
console screen needs is an ordinary merchant endpoint, authenticated by the session rather than
by the key.

The key reaches this API exactly once per sign-in and is not stored by anything. What persists is
a verifier for a credential the console derives and holds, and the console's cookie is not that
verifier. See `agentrank_api.auth.console`.

Opening a session requires a merchant API key and refuses a session. That is the rule that keeps
a session bounded: a stolen session cannot mint a second one with a fresh expiry, so signing out
and waiting out the clock are both real remedies rather than things an attacker can step around.
A benchmark-bound credential is refused as well, through the same dependency that keeps a buyer
off every other merchant command surface.
"""

from datetime import datetime
from typing import Any

from fastapi import APIRouter, status
from pydantic import BaseModel, ConfigDict, Field

from agentrank_api.auth.console import (
    CONSOLE_SESSION_SCHEME,
    VERIFIER_HEX_LENGTH,
    ConsoleSessionService,
)
from agentrank_api.auth.models import MerchantConsoleSession
from agentrank_api.dependencies import OperatorKeyDep, PresentedCredentialDep, SessionDep
from agentrank_api.errors import AuthenticationError, ErrorResponse

router = APIRouter(prefix="/api/v1/console", tags=["console"])

UNAUTHENTICATED: dict[int | str, dict[str, Any]] = {
    status.HTTP_401_UNAUTHORIZED: {
        "model": ErrorResponse,
        "description": "No usable merchant credential was presented",
    }
}


class OpenSessionRequest(BaseModel):
    """The credential the console will present from now on, registered before it is used.

    Supplied by the caller rather than generated here, which is what lets the console keep the
    browser's cookie and this verifier as two different values. The bound is the whole of the
    validation: a caller who chooses a weak one weakens their own session and nobody else's,
    because the session is bound to the merchant their key authenticated as.

    One field, and nothing else is accepted. `extra="forbid"` is what makes a body carrying a
    merchant identifier a refusal rather than a value quietly ignored: a caller who tries to
    choose their own tenant is told no, instead of being told yes and left to work out from the
    response that the field they sent did nothing.
    """

    model_config = ConfigDict(extra="forbid")

    verifier: str = Field(
        pattern=rf"^{CONSOLE_SESSION_SCHEME}_[0-9a-f]{{{VERIFIER_HEX_LENGTH}}}$",
        description="A console session verifier: the scheme marker and 256 bits of hex.",
    )


class SessionView(BaseModel):
    """What the console learns about the session it just opened, or the one it holds.

    No secret and no identifier that would be useful to anybody else.

    Both a timestamp and a duration, and the duration is the one the console uses. A console that
    subtracted the timestamp from its own clock would be putting its clock back into an answer
    the database clock had already decided: one running fast hands every merchant a cookie that
    expires before their session does, and one running far enough fast computes a lifetime of
    zero and makes signing in impossible. `expires_at` stays because it is what a human reads.
    """

    merchant_id: str
    expires_at: datetime
    expires_in_seconds: int


class SignOutView(BaseModel):
    """Whether this call is what closed the session. False for one that was already closed."""

    revoked: bool


@router.post(
    "/sessions",
    response_model=SessionView,
    status_code=status.HTTP_201_CREATED,
    responses=UNAUTHENTICATED,
)
async def open_session(
    session: SessionDep, merchant: OperatorKeyDep, body: OpenSessionRequest
) -> SessionView:
    """Open a browser session for the merchant this API key belongs to.

    Which merchant is decided by the credential and never by the request body, which carries no
    merchant field at all. A caller holding a key for one merchant cannot open a session for
    another whatever they send.
    """
    service = ConsoleSessionService(session)
    record = await service.open(
        merchant_id=merchant.merchant_id,
        credential_id=merchant.credential_id,
        verifier=body.verifier,
    )
    return await _view(service, record)


@router.get("/sessions/current", response_model=SessionView, responses=UNAUTHENTICATED)
async def current_session(session: SessionDep, presented: PresentedCredentialDep) -> SessionView:
    """Confirm the session this request presented is still open, and say until when.

    Refuses a merchant API key rather than inventing an expiry for one. A key is not a session
    and has no session lifetime, and answering with a plausible date would be this endpoint
    making one up. The console only ever calls it holding a session.
    """
    service = ConsoleSessionService(session)
    record = await service.current(presented)
    if record is None:
        raise AuthenticationError()
    return await _view(service, record)


async def _view(service: ConsoleSessionService, record: MerchantConsoleSession) -> SessionView:
    """One session as the console reads it, with the remaining lifetime measured by PostgreSQL.

    The clock is read here rather than taken from the process, so both halves of the subtraction
    come from the database and no console's idea of now enters the answer.
    """
    remaining = record.expires_at - await service.clock()
    return SessionView(
        merchant_id=str(record.merchant_id),
        expires_at=record.expires_at,
        expires_in_seconds=max(0, int(remaining.total_seconds())),
    )


@router.delete("/sessions/current", response_model=SignOutView, responses=UNAUTHENTICATED)
async def close_session(session: SessionDep, presented: PresentedCredentialDep) -> SignOutView:
    """Close the session that authenticated this request.

    The verifier rather than the principal, because a principal says which merchant is calling
    and not which of their sessions did. Signing out of one browser must not sign out of the
    others, and a merchant who wants every session closed revokes the credential behind them.
    """
    revoked = await ConsoleSessionService(session).revoke(presented)
    return SignOutView(revoked=revoked)
