"""Merchant API credential persistence.

A credential is the row that ties secret material to one merchant. It holds a verifier and
never the secret itself, so a copy of this table is not a set of working keys.

Three properties are enforced by the database rather than by application code, because the
database is the only layer that cannot be bypassed:

- the stored verifier is a labelled digest of a known algorithm, never a raw token
- a credential belongs to one merchant and cannot be moved to another
- revocation is terminal, and everything else about a credential is immutable

The last one is a trigger rather than a constraint, because it is a rule about a transition
rather than about a row. See the migration for the statement itself.

There is no status column. A credential has exactly two states and `revoked_at` already
distinguishes them, so a second column would only be a thing that could disagree with it. The
mandate carries both because its status is an enumeration a domain rule reads and compares;
this is a boolean fact with a timestamp attached.
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    String,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from agentrank_api.auth.tokens import HASH_PATTERN
from agentrank_api.models import Base

# `sha256:` and sixty four hexadecimal characters. Sized exactly rather than left as text, so a
# value of the wrong shape cannot be stored even if the check constraint were ever dropped.
SECRET_HASH_LENGTH = 71

MAX_LABEL_LENGTH = 100


class MerchantApiCredential(Base):
    """One API key issued to one merchant, as everything about it except the key.

    There is no `updated_at`. Every field except `revoked_at` is immutable and revocation
    stamps its own timestamp, so a general purpose modification time would only be a second
    name for it.

    `label` is required and non blank. A credential nobody can identify is a credential nobody
    will ever revoke, because the operator looking at a listing cannot tell which of three keys
    belongs to the integration that leaked.

    The identifier is public. It travels inside every token, it is printed by the operator
    listing and it appears in audit events, and none of that weakens anything: knowing which
    credential exists is not knowing its secret, and a request carrying an identifier and no
    matching secret is refused exactly as one carrying neither is.
    """

    __tablename__ = "merchant_api_credential"
    __table_args__ = (
        CheckConstraint(f"secret_hash ~ '{HASH_PATTERN}'", name="secret_hash_format"),
        CheckConstraint("length(btrim(label)) > 0", name="label_not_blank"),
        # A benchmark credential may name only a run belonging to the same merchant.  The
        # nullable column is otherwise empty for an ordinary merchant credential.
        ForeignKeyConstraint(
            ["benchmark_run_id", "merchant_id"],
            ["benchmark_run.id", "benchmark_run.merchant_id"],
            name="fk_merchant_api_credential_benchmark_run",
            ondelete="RESTRICT",
        ),
        Index(None, "benchmark_run_id"),
        # One secret, one credential. Two rows carrying one verifier would mean one key
        # authenticating as two merchants, which is the one thing this table exists to make
        # impossible. Unreachable by chance at 256 bits, and this is what makes it unreachable
        # by a copy as well.
        Index(None, "secret_hash", unique=True),
        # The listing read, and the RESTRICT check when a merchant is deleted.
        Index(None, "merchant_id"),
        # What lets another table name a credential and its merchant together in one foreign
        # key. A console session is bound to both, and a binding the database enforces is what
        # makes "this session belongs to that merchant's credential" impossible to get wrong.
        UniqueConstraint("id", "merchant_id", name="uq_merchant_api_credential_binding"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid7)
    # RESTRICT, not CASCADE. A credential is the evidence that explains authenticated history,
    # and it must not disappear as a side effect of removing the merchant it was issued to.
    merchant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("merchant.id", ondelete="RESTRICT"),
        nullable=False,
    )
    benchmark_run_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    secret_hash: Mapped[str] = mapped_column(String(SECRET_HASH_LENGTH), nullable=False)
    label: Mapped[str] = mapped_column(String(MAX_LABEL_LENGTH), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    @property
    def is_active(self) -> bool:
        """Whether this credential still authenticates anything.

        Derived rather than stored, so it cannot disagree with the timestamp beside it. The
        authentication read does not use this: it puts the same condition in the SQL, so a
        revoked credential is never loaded at all. This is for the operator listing, which
        loads every credential a merchant has on purpose.
        """
        return self.revoked_at is None


class MerchantConsoleSession(Base):
    """One durable browser session for the merchant console.

    The row a cookie resolves to, held by PostgreSQL rather than by whichever Next.js process
    happened to answer the sign-in request. That is the whole point of it: any console process
    can resolve any session, and a restart signs nobody out.

    It holds a verifier and never a working credential, exactly like `merchant_api_credential`
    beside it, and it holds no merchant API key at all. See `agentrank_api.auth.console` for what
    that verifier is derived from and why the browser's cookie is not it.

    Two revocation paths reach a session and both are terminal. Signing out stamps `revoked_at`
    here; revoking the merchant credential this session was opened from closes it too, through
    the join the authentication read makes rather than through anything written on this row. A
    session therefore cannot outlive the credential that authorized it, which is what makes
    revoking a leaked console key an actual remedy.

    There is no `last_seen_at` and no sliding expiry. `expires_at` is computed once from the
    database clock when the session opens, so authenticating one is a read and never a write, and
    a console screen that renders six panels does not write six rows.
    """

    __tablename__ = "merchant_console_session"
    __table_args__ = (
        CheckConstraint(f"verifier_hash ~ '{HASH_PATTERN}'", name="verifier_hash_format"),
        # A session belongs to one merchant's credential, and the pair is checked by the database
        # rather than by the service that writes it. A session naming one merchant and a
        # credential belonging to another is the one thing this table must not be able to hold.
        ForeignKeyConstraint(
            ["credential_id", "merchant_id"],
            ["merchant_api_credential.id", "merchant_api_credential.merchant_id"],
            name="fk_merchant_console_session_credential",
            ondelete="RESTRICT",
        ),
        # One verifier, one session. The authentication read is this index, so resolving a cookie
        # costs one lookup rather than a scan of every session ever opened.
        Index(None, "verifier_hash", unique=True),
        # The operator revocation sweep and the RESTRICT check when a merchant is removed.
        Index(None, "merchant_id"),
        # The cleanup read, which walks settled sessions oldest first.
        Index(None, "expires_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid7)
    merchant_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    credential_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    verifier_hash: Mapped[str] = mapped_column(String(SECRET_HASH_LENGTH), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
