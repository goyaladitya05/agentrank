"""Durable browser sessions for the merchant console.

The console is a Next.js server that renders merchant pages by calling this API. It has to hold
a merchant identity between requests, and until Phase 5A it held one in the process' memory: a
random cookie token, a map from that token to the merchant API key, and a deployment story that
ended at localhost. A cookie identified a session only to the process that minted it, so a second
console instance signed the merchant out and so did every restart.

This is the durable replacement, and the shape of it is a deliberate trust decision.

**The merchant API key is not stored anywhere.** It is presented once to open a session, verified
by the existing credential path, and forgotten. Nothing in this table can produce one, which is
the property `merchant_api_credential` already has and the one thing an encrypted-at-rest key
vault in the console would have thrown away: a copy of that table would have been a set of
working merchant keys.

**The session is a credential of its own, bound to the credential that opened it.** It
authenticates as the merchant, expires on its own schedule, can be revoked by signing out, and
dies when the credential behind it is revoked. That last one is why `credential_id` is a column
rather than a convenience: revoking a leaked console key has to close the sessions it minted, and
an operator who could not do that would have no way to end access short of deleting a merchant.

**The verifier is supplied by the console rather than generated here.** That is the unusual half
and it is what buys the property below, so it is worth stating exactly what it does and does not
allow. Opening a session requires a valid merchant API key, and the session is bound to whichever
merchant that key belongs to, so no verifier a caller chooses can reach another merchant's data.
A caller who chose a guessable one would be weakening their own session and nobody else's, and
the unique index means a value already registered cannot be registered again. What a caller
cannot do is pick a verifier that belongs to somebody else, because the row it would collide with
already exists.

What that buys: the value in the browser's cookie is not this verifier. The console derives the
verifier from the cookie with an HMAC under a secret only the console deployment holds, so a
cookie recovered from a retained browser trace, a proxy log or a support screenshot is inert
without that secret. The cookie and the deployment secret are both required and neither is
sufficient. See docs/security.md.

`ars_` rather than `ar_`: the two grammars are disjoint, so a console session verifier can never
parse as a merchant API key and a merchant API key can never parse as one of these. Presenting
either is `Authorization: Bearer`, and which one arrived is decided by the value rather than by
anything the caller says about it.
"""

import re
import uuid
from datetime import datetime, timedelta

from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from agentrank_api.auth.models import MerchantApiCredential, MerchantConsoleSession
from agentrank_api.auth.principal import AuthenticatedMerchant
from agentrank_api.auth.tokens import hash_secret
from agentrank_api.errors import ConflictError

# The scheme marker, and the reason a scanner can act on one of these without decoding anything.
# Distinct from the merchant key scheme so that neither value can be mistaken for the other, by a
# parser or by a person reading a log.
CONSOLE_SESSION_SCHEME = "ars"

# 256 bits of HMAC output, hex encoded. Fixed here so the pattern below, the console that derives
# one and anything that scans for one cannot drift apart.
VERIFIER_HEX_LENGTH = 64

VERIFIER_PATTERN = re.compile(rf"^{CONSOLE_SESSION_SCHEME}_[0-9a-f]{{{VERIFIER_HEX_LENGTH}}}$")

# How long a session is good for, from the moment it is opened. Absolute rather than sliding, and
# that is a performance decision as much as a security one: a sliding window means an UPDATE on
# every authenticated page render, which is a write on the read path of every console screen. A
# working day is the same span the in-process store used, so no merchant's habits change.
SESSION_LIFETIME = timedelta(hours=12)

CONSOLE_SESSION_RESOURCE = "merchant_console_session"


def is_console_session_verifier(presented: str) -> bool:
    """Whether a presented credential is shaped like a console session verifier.

    Total, and the reason authentication can dispatch on the value. A string either matches this
    grammar or it does not, and nothing that matches this one can also match a merchant API key.
    """
    return VERIFIER_PATTERN.match(presented) is not None


class ConsoleSessionService:
    """Opening, resolving and closing the console's durable browser sessions."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def open(
        self,
        *,
        merchant_id: uuid.UUID,
        credential_id: uuid.UUID,
        verifier: str,
        lifetime: timedelta = SESSION_LIFETIME,
    ) -> MerchantConsoleSession:
        """Record a session for an already authenticated merchant, and commit it.

        The verifier is hashed the way a merchant key's secret is hashed, so this table holds a
        digest and never a working credential, and the raw value is not returned: the caller
        supplied it and already has it.

        Expiry is computed by PostgreSQL from its own clock rather than by this process. A
        console whose clock runs fast must not be able to mint a session that outlives what this
        deployment promises, and two processes disagreeing about now is exactly the kind of
        thing a multi-process topology introduces.
        """
        record = MerchantConsoleSession(
            merchant_id=merchant_id,
            credential_id=credential_id,
            verifier_hash=hash_secret(verifier),
            expires_at=func.now() + lifetime,
        )
        self._session.add(record)
        try:
            await self._session.commit()
        except IntegrityError:
            # The unique index refusing a verifier that is already registered. Answered by name
            # rather than left to surface as a database error, for two reasons. It is not a
            # database failure and reporting one would send an operator to look at PostgreSQL.
            # And the driver's exception carries the INSERT and its parameters, so letting it
            # reach the application's generic handler writes this session's digest into the log,
            # which is the one value this table exists to keep in one place.
            await self._session.rollback()
            raise ConflictError(
                "console_session_exists",
                "that session credential is already registered",
                resource=CONSOLE_SESSION_RESOURCE,
                identifier=str(merchant_id),
            ) from None
        await self._session.refresh(record)
        return record

    async def authenticate(self, presented: str) -> AuthenticatedMerchant | None:
        """Turn a presented session verifier into a principal, or into nothing.

        One statement, and every condition that could refuse is in it. An unknown session, an
        expired one, a revoked one and one whose merchant credential has since been revoked are
        all the same absence, decided by PostgreSQL rather than by a loaded row this code then
        inspects. A caller learns that authentication failed and learns nothing else.

        Expiry is compared against `now()`, so the deciding clock is the database's for every
        console process at once. A console with a skewed clock cannot extend a session and cannot
        end one early.

        The credential row is joined and not selected. `credential_id` is already on the session
        row and the foreign key makes them the same value, so loading the credential would pull
        that merchant's `secret_hash` into this process on every authenticated page render for a
        value it already has.

        No benchmark capability is ever attached. A console session is opened from an ordinary
        merchant credential, and a benchmark-bound credential is refused a session at the route
        that opens one, so there is no path by which a buyer's credential becomes a browser
        session.
        """
        if not is_console_session_verifier(presented):
            return None
        record = await self._open(presented)
        if record is None:
            return None
        return AuthenticatedMerchant(
            merchant_id=record.merchant_id,
            credential_id=record.credential_id,
            benchmark_capability=None,
        )

    async def current(self, presented: str) -> MerchantConsoleSession | None:
        """The open session this verifier names, for a console asking how long it has.

        The same read `authenticate` makes, stated once so the two cannot answer differently
        about the same verifier in the same instant.
        """
        return await self._open(presented)

    async def _open(self, presented: str) -> MerchantConsoleSession | None:
        """The one statement that decides whether a presented verifier names an open session.

        Every condition that could refuse is in it: unknown, revoked, expired, and a merchant
        credential that has since been revoked, all decided by PostgreSQL rather than by a loaded
        row this code then inspects.

        The lookup is by the unique index on the digest of the presented value, which is what
        makes this one indexed read rather than a scan. There is deliberately no comparison after
        it. An earlier version called `verify_secret` here and described it as the constant time
        half of a two-step check, which read well and was not true: the row had been selected by
        exact equality against that same digest, so the comparison could not fail and was doing
        nothing. A merchant key's two steps are different, and genuinely two: it looks up by a
        credential identifier that travels in the token and is not secret, and the constant time
        comparison of the secret is the whole of the authentication. Here the lookup is the
        authentication, and what protects it is that 256 bits of HMAC output has no useful
        prefix to walk towards.
        """
        if not is_console_session_verifier(presented):
            return None
        return (
            await self._session.execute(
                select(MerchantConsoleSession)
                .join(
                    MerchantApiCredential,
                    MerchantConsoleSession.credential_id == MerchantApiCredential.id,
                )
                .where(
                    MerchantConsoleSession.verifier_hash == hash_secret(presented),
                    MerchantConsoleSession.revoked_at.is_(None),
                    MerchantConsoleSession.expires_at > func.now(),
                    MerchantApiCredential.revoked_at.is_(None),
                )
            )
        ).scalar_one_or_none()

    async def revoke(self, presented: str) -> bool:
        """Close the session this verifier names, and say whether this call is what closed it.

        Idempotent, and deliberately silent about everything else. Signing out twice, signing out
        of a session that expired an hour ago and presenting a verifier that never existed all
        answer the same way, because the console has nothing useful to do with the difference and
        an endpoint that reported it would be an oracle for which sessions exist.
        """
        if not is_console_session_verifier(presented):
            return False
        record = (
            await self._session.execute(
                select(MerchantConsoleSession)
                .where(
                    MerchantConsoleSession.verifier_hash == hash_secret(presented),
                    MerchantConsoleSession.revoked_at.is_(None),
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if record is None:
            await self._session.rollback()
            return False
        record.revoked_at = func.now()
        await self._session.commit()
        return True

    async def revoke_for_merchant(self, merchant_id: uuid.UUID) -> int:
        """Close every open session one merchant holds, and report how many were closed.

        The operator answer to a console key that leaked, and the reason it exists separately
        from credential revocation is that a merchant may want their own sessions ended without
        their integrations losing the key they authenticate with.
        """
        records = list(
            (
                await self._session.execute(
                    select(MerchantConsoleSession)
                    .where(
                        MerchantConsoleSession.merchant_id == merchant_id,
                        MerchantConsoleSession.revoked_at.is_(None),
                    )
                    .with_for_update()
                )
            )
            .scalars()
            .all()
        )
        for record in records:
            record.revoked_at = func.now()
        await self._session.commit()
        return len(records)

    async def purge_settled(self, *, older_than: timedelta, limit: int) -> int:
        """Delete sessions that stopped being usable long enough ago to be uninteresting.

        Bounded on both sides. `older_than` keeps a recently expired session readable while an
        operator is still investigating why a merchant was signed out; `limit` keeps one call
        from being an unbounded delete on a table an operator runs this against by hand.

        Only settled rows. An open session is never touched, so there is no argument under which
        this signs a working merchant out, and nothing outside this table is deleted: a session
        is not evidence about a benchmark, a source snapshot or a compiler run and holds no
        reference any of them depend on.

        Both bounds are checked here rather than only at the caller, because that sentence is an
        invariant of this method and not of the command line above it. A negative `older_than`
        moves the cutoff into the future and `expires_at < cutoff` then matches every session
        opened in the last twelve hours, which is every open session: the promise would be
        broken by arithmetic rather than by intent, which is exactly the kind of promise worth
        enforcing where it is made.
        """
        if older_than < timedelta(0):
            raise ValueError(
                "older_than must not be negative: a cutoff in the future would "
                "select sessions that are still open"
            )
        if limit < 1:
            raise ValueError("limit must be at least 1")
        cutoff = func.now() - older_than
        settled = (
            select(MerchantConsoleSession.id)
            .where(
                (MerchantConsoleSession.expires_at < cutoff)
                | (MerchantConsoleSession.revoked_at < cutoff)
            )
            .order_by(MerchantConsoleSession.expires_at)
            .limit(limit)
        )
        doomed = list((await self._session.execute(settled)).scalars().all())
        if not doomed:
            await self._session.rollback()
            return 0
        # One statement rather than a load and a delete per row. Two operators running this at
        # once is an ordinary thing to happen and a per-row `get_one` would raise on the rows the
        # other one already removed, turning routine cleanup into an exception. `rowcount` is what
        # this deployment actually deleted, which is the honest number to report.
        removed = await self._session.execute(
            delete(MerchantConsoleSession)
            .where(MerchantConsoleSession.id.in_(doomed))
            .returning(MerchantConsoleSession.id)
        )
        deleted = len(list(removed.scalars().all()))
        await self._session.commit()
        return deleted

    async def clock(self) -> datetime:
        """The database's own clock, for a caller that has to report an expiry beside a row."""
        observed: datetime = (await self._session.execute(select(func.now()))).scalar_one()
        return observed
