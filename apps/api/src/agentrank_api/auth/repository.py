"""Persistence access for merchant API credentials.

The repository owns SQLAlchemy and does not commit: the caller sets the transaction boundary.

Three reads and two writes, and the one that matters is `get_active`. It is the statement every
authenticated request runs, so it is a primary key lookup with the revocation condition in the
SQL rather than a scan of a table that grows with every key ever issued, and rather than a load
followed by a check in Python. A revoked credential is not returned at all, which is what makes
"revoked" and "never existed" the same answer without any code deciding to make them the same.

There is deliberately no method that returns a secret, because no row holds one, and no method
that changes a verifier. Rotating a key means issuing a second credential and revoking the
first, which is the whole reason a merchant may hold several.
"""

import uuid
from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from agentrank_api.auth.models import MerchantApiCredential


class MerchantCredentialRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self,
        *,
        merchant_id: uuid.UUID,
        secret_hash: str,
        label: str,
        benchmark_run_id: uuid.UUID | None = None,
    ) -> MerchantApiCredential:
        """Write a credential and flush so that its generated columns are set.

        The verifier is passed in already derived. This layer never sees secret material and
        has no way to produce a verifier, which is what keeps the one derivation in the one
        module that documents why it is the derivation it is.

        A credential is always created active. There is no parameter for `revoked_at`, so a
        credential that was never usable cannot be brought into existence, only arrived at.
        """
        credential = MerchantApiCredential(
            merchant_id=merchant_id,
            secret_hash=secret_hash,
            label=label,
            benchmark_run_id=benchmark_run_id,
        )
        self._session.add(credential)
        await self._session.flush()
        return credential

    async def get_active(self, credential_id: uuid.UUID) -> MerchantApiCredential | None:
        """The credential this identifier names, if it exists and has not been revoked.

        The statement every authenticated request runs. A primary key lookup, so the cost does
        not grow with the number of credentials in the table and no cache is needed to keep it
        from growing.

        Revocation is a condition in the SQL rather than a test on a loaded row. That is not a
        style preference: it is what makes a revoked credential indistinguishable from one that
        never existed, at the layer where the difference would otherwise have to be deliberately
        thrown away by every caller.

        When this takes effect is decided by the transaction this runs in. A revocation that
        commits after this statement has read is not observed by this request, which is already
        authenticated by then. A request whose read happens after that commit finds nothing. See
        SECURITY.md.
        """
        statement = select(MerchantApiCredential).where(
            MerchantApiCredential.id == credential_id,
            MerchantApiCredential.revoked_at.is_(None),
        )
        return (await self._session.execute(statement)).scalar_one_or_none()

    async def get(self, credential_id: uuid.UUID) -> MerchantApiCredential | None:
        """One credential whatever state it is in, for the operator tooling.

        Deliberately not the authentication read. An operator revoking a key has to be able to
        see one that is already revoked, and answering "no such credential" for a key they are
        holding in their hand would send them looking for a bug.
        """
        return await self._session.get(MerchantApiCredential, credential_id)

    async def list_for_merchant(self, merchant_id: uuid.UUID) -> Sequence[MerchantApiCredential]:
        """Every credential one merchant has ever been issued, oldest first.

        Revoked ones included. A listing that hid them would be a listing that could not answer
        "was this key ever ours", which is the first question asked about a leak.

        Version 7 identifiers are time ordered, so this is the order they were issued in.
        """
        statement = (
            select(MerchantApiCredential)
            .where(MerchantApiCredential.merchant_id == merchant_id)
            .order_by(MerchantApiCredential.id)
        )
        return (await self._session.execute(statement)).scalars().all()

    async def revoke(self, credential: MerchantApiCredential) -> bool:
        """Revoke a credential, and report whether this call is what changed it.

        Idempotent: revoking an already revoked credential is not an error and does not move
        `revoked_at`. The return value exists so that the caller can record exactly one event
        for exactly one real transition.

        Terminal. There is no counterpart that restores one, and the database trigger refuses
        the update, so a key that has been withdrawn stays withdrawn.

        The timestamp comes from the database clock. Inside one transaction `now()` is the
        transaction time, so a revocation and anything recorded beside it carry the same instant
        rather than two clock readings that merely look simultaneous.
        """
        if credential.revoked_at is not None:
            return False

        credential.revoked_at = func.now()
        await self._session.flush()
        # Explicitly reloaded rather than left expired. A SQL expression assigned to an
        # attribute is not readable until it is fetched back, and an implicit fetch inside an
        # async session raises MissingGreenlet.
        await self._session.refresh(credential, ["revoked_at"])
        return True

    async def clock(self) -> datetime:
        """The database's own clock, for a caller that has to stamp something beside a row.

        Here rather than read from the process, for the same reason the payment listing reads
        it here: an operator machine whose clock is minutes off the database's must not be able
        to make a credential look older or newer than it is.
        """
        observed: datetime = (await self._session.execute(select(func.now()))).scalar_one()
        return observed
