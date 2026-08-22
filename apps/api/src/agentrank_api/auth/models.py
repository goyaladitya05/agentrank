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
