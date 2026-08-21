"""create payment attempt

The durable record a provider call is made from, and the lifecycle states that record needs
around it. Points worth knowing when reading this:

- payment_attempt references checkout_session through the composite (checkout_id,
  merchant_id, mandate_id, currency, amount_minor). That one foreign key is four invariants:
  an attempt cannot name another merchant's quote, cannot claim a mandate the quote was not
  written against, and cannot carry an amount or a currency that differ from what was quoted
  and authorized. Every column it points at is immutable at the database, so the frozen
  amount cannot drift from the authorized amount. checkout_session gains the matching unique
  constraint first, because PostgreSQL needs one on exactly those five columns to point at.
  Adding it changes no row: all five are already unique because id alone is.
- payment_attempt references inventory_reservation through (reservation_id, merchant_id,
  checkout_id), so an attempt cannot be bound to another merchant's hold or to a hold taken
  for a different checkout. inventory_reservation gains the matching unique constraint for
  the same reason and with the same non effect.
- There is no foreign key to merchant and none to spending_mandate. Both are transitive
  through checkout_session, which already reaches the mandate, which already reaches the
  merchant. A second path to the same fact is a second thing that can disagree.
- Both foreign keys are named explicitly. The metadata naming convention would produce names
  longer than 63 bytes and PostgreSQL truncates identifiers there, so the name written here
  and the name in the database would silently disagree.
- Three partial unique indexes carry the rules this phase exists to make structural.
  uq_payment_attempt_mandate_succeeded is the single purchase mandate rule: at most one
  attempt under a mandate may be SUCCEEDED. uq_payment_attempt_checkout_succeeded is the same
  for a checkout. uq_payment_attempt_mandate_open allows at most one non terminal attempt per
  mandate, which is what stops two candidate checkouts under one mandate from both reaching a
  provider. Every predicate is static, which is what makes it indexable: a predicate calling
  now() is not immutable and PostgreSQL will not index on one.
- uq_payment_attempt_identity is an ordinary unique constraint on (checkout_id,
  idempotency_key). One logical payment operation exists once, so a retried request resolves
  to the row the first one wrote rather than to a second provider call.
- checkout_session gains PAID and paid_at. inventory_reservation gains COMMITTED, CONSUMED
  and consumed_at. audit_event gains the PAYMENT_PROVIDER actor. Each is an ordinary check
  constraint change rather than an ALTER TYPE, which is exactly why none of these
  enumerations was ever a native PostgreSQL enum.
- uq_inventory_reservation_active_checkout is rebuilt over the wider predicate. It used to
  say "one ACTIVE reservation per checkout" and now says "one holding reservation per
  checkout", where holding is ACTIVE or COMMITTED. Without the rebuild a committed hold would
  stop excluding a second one, and a checkout could hold stock twice.
- The three existing status guards are rewritten from blacklists into whitelists, in this
  migration rather than after it. Each used to protect terminality by naming one value:
  OLD.status = 'REVOKED', 'CANCELLED', 'RELEASED'. That is correct only while every table has
  exactly two statuses and exactly one of them is terminal, and this is the migration that
  stops being true. A PAID checkout or a CONSUMED reservation under the old guards would be
  terminal in the domain and freely updatable at the database. Each guard now enumerates the
  transitions it permits and refuses everything else, so the next status added without a
  decision about what may follow it fails closed rather than open.
- payment_attempt gets a guard of the same shape from the start. Ownership, money, identity
  and every timestamp already written are immutable; the lifecycle is a whitelist; SUCCEEDED
  and FAILED accept no update at all, not even one that changes nothing.
- No guard refuses DELETE, so cascades still work, and DROP is not an UPDATE, so a downgrade
  does too.
- The downgrade is conditionally irreversible and says so rather than discovering it. Once a
  payment has been taken, this schema holds facts the previous one has no words for, and the
  reversal refuses instead of inventing a mapping for them. See `downgrade`.

Revision ID: ab60fc05d747
Revises: 637598637298
Created: 2026-08-21 21:55:44.873942
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "ab60fc05d747"
down_revision: str | None = "637598637298"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

HOLDING_PREDICATE = "status IN ('ACTIVE', 'COMMITTED')"
ACTIVE_PREDICATE = "status = 'ACTIVE'"
SUCCEEDED_PREDICATE = "status = 'SUCCEEDED'"
OPEN_PREDICATE = "status IN ('ADMITTED', 'IN_FLIGHT', 'UNKNOWN')"

# The checkout guard, rewritten as a whitelist. The immutable field list is unchanged; what
# changes is the second half, which used to say "a CANCELLED checkout cannot be changed" and
# now says which transitions exist at all. OPEN may be cancelled or paid. Both are terminal,
# and terminal here means no UPDATE succeeds, not merely that the status may not move.
CHECKOUT_GUARD = """
CREATE OR REPLACE FUNCTION checkout_session_quote_guard() RETURNS trigger AS $$
BEGIN
    IF NEW.id IS DISTINCT FROM OLD.id
        OR NEW.merchant_id IS DISTINCT FROM OLD.merchant_id
        OR NEW.mandate_id IS DISTINCT FROM OLD.mandate_id
        OR NEW.currency IS DISTINCT FROM OLD.currency
        OR NEW.subtotal_amount_minor IS DISTINCT FROM OLD.subtotal_amount_minor
        OR NEW.shipping_amount_minor IS DISTINCT FROM OLD.shipping_amount_minor
        OR NEW.discount_amount_minor IS DISTINCT FROM OLD.discount_amount_minor
        OR NEW.total_amount_minor IS DISTINCT FROM OLD.total_amount_minor
        OR NEW.created_at IS DISTINCT FROM OLD.created_at
        OR NEW.expires_at IS DISTINCT FROM OLD.expires_at
    THEN
        RAISE EXCEPTION 'checkout quote fields are immutable';
    END IF;

    IF OLD.status IN ('CANCELLED', 'PAID') THEN
        RAISE EXCEPTION 'a % checkout cannot be changed', lower(OLD.status);
    END IF;

    IF (OLD.status, NEW.status) NOT IN (('OPEN', 'OPEN'), ('OPEN', 'CANCELLED'), ('OPEN', 'PAID'))
    THEN
        RAISE EXCEPTION 'checkout status cannot go from % to %', OLD.status, NEW.status;
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql
"""

CHECKOUT_GUARD_BLACKLIST = """
CREATE OR REPLACE FUNCTION checkout_session_quote_guard() RETURNS trigger AS $$
BEGIN
    IF NEW.id IS DISTINCT FROM OLD.id
        OR NEW.merchant_id IS DISTINCT FROM OLD.merchant_id
        OR NEW.mandate_id IS DISTINCT FROM OLD.mandate_id
        OR NEW.currency IS DISTINCT FROM OLD.currency
        OR NEW.subtotal_amount_minor IS DISTINCT FROM OLD.subtotal_amount_minor
        OR NEW.shipping_amount_minor IS DISTINCT FROM OLD.shipping_amount_minor
        OR NEW.discount_amount_minor IS DISTINCT FROM OLD.discount_amount_minor
        OR NEW.total_amount_minor IS DISTINCT FROM OLD.total_amount_minor
        OR NEW.created_at IS DISTINCT FROM OLD.created_at
        OR NEW.expires_at IS DISTINCT FROM OLD.expires_at
    THEN
        RAISE EXCEPTION 'checkout quote fields are immutable';
    END IF;

    IF OLD.status = 'CANCELLED'
        AND (NEW.status IS DISTINCT FROM OLD.status
             OR NEW.cancelled_at IS DISTINCT FROM OLD.cancelled_at)
    THEN
        RAISE EXCEPTION 'a cancelled checkout cannot be changed';
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql
"""

# The reservation guard, rewritten the same way. ACTIVE may be committed to a payment or
# released. COMMITTED may be released, when the payment it was bound to definitively failed,
# or consumed, when it definitively succeeded. RELEASED and CONSUMED are terminal.
RESERVATION_GUARD = """
CREATE OR REPLACE FUNCTION inventory_reservation_guard() RETURNS trigger AS $$
BEGIN
    IF NEW.id IS DISTINCT FROM OLD.id
        OR NEW.merchant_id IS DISTINCT FROM OLD.merchant_id
        OR NEW.checkout_id IS DISTINCT FROM OLD.checkout_id
        OR NEW.created_at IS DISTINCT FROM OLD.created_at
        OR NEW.expires_at IS DISTINCT FROM OLD.expires_at
    THEN
        RAISE EXCEPTION 'inventory reservation ownership and expiry are immutable';
    END IF;

    IF OLD.status IN ('RELEASED', 'CONSUMED') THEN
        RAISE EXCEPTION 'a % reservation cannot be changed', lower(OLD.status);
    END IF;

    IF (OLD.status, NEW.status) NOT IN (
        ('ACTIVE', 'ACTIVE'),
        ('ACTIVE', 'COMMITTED'),
        ('ACTIVE', 'RELEASED'),
        ('COMMITTED', 'COMMITTED'),
        ('COMMITTED', 'RELEASED'),
        ('COMMITTED', 'CONSUMED')
    ) THEN
        RAISE EXCEPTION 'reservation status cannot go from % to %', OLD.status, NEW.status;
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql
"""

RESERVATION_GUARD_BLACKLIST = """
CREATE OR REPLACE FUNCTION inventory_reservation_guard() RETURNS trigger AS $$
BEGIN
    IF NEW.id IS DISTINCT FROM OLD.id
        OR NEW.merchant_id IS DISTINCT FROM OLD.merchant_id
        OR NEW.checkout_id IS DISTINCT FROM OLD.checkout_id
        OR NEW.created_at IS DISTINCT FROM OLD.created_at
        OR NEW.expires_at IS DISTINCT FROM OLD.expires_at
    THEN
        RAISE EXCEPTION 'inventory reservation ownership and expiry are immutable';
    END IF;

    IF OLD.status = 'RELEASED'
        AND (NEW.status IS DISTINCT FROM OLD.status
             OR NEW.released_at IS DISTINCT FROM OLD.released_at)
    THEN
        RAISE EXCEPTION 'a released reservation cannot be changed';
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql
"""

# The mandate guard gains no new status, and is rewritten anyway. Leaving one blacklist
# behind would leave the next person to add a mandate status with a guard that looks like the
# other two and behaves differently.
MANDATE_GUARD = """
CREATE OR REPLACE FUNCTION spending_mandate_authorization_guard() RETURNS trigger AS $$
BEGIN
    IF NEW.id IS DISTINCT FROM OLD.id
        OR NEW.merchant_id IS DISTINCT FROM OLD.merchant_id
        OR NEW.max_total_amount_minor IS DISTINCT FROM OLD.max_total_amount_minor
        OR NEW.currency IS DISTINCT FROM OLD.currency
        OR NEW.max_quantity IS DISTINCT FROM OLD.max_quantity
        OR NEW.valid_from IS DISTINCT FROM OLD.valid_from
        OR NEW.valid_until IS DISTINCT FROM OLD.valid_until
        OR NEW.created_at IS DISTINCT FROM OLD.created_at
    THEN
        RAISE EXCEPTION 'spending mandate authorization fields are immutable';
    END IF;

    IF OLD.status = 'REVOKED' THEN
        RAISE EXCEPTION 'a revoked spending mandate cannot be changed';
    END IF;

    IF (OLD.status, NEW.status) NOT IN (('ACTIVE', 'ACTIVE'), ('ACTIVE', 'REVOKED')) THEN
        RAISE EXCEPTION 'mandate status cannot go from % to %', OLD.status, NEW.status;
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql
"""

MANDATE_GUARD_BLACKLIST = """
CREATE OR REPLACE FUNCTION spending_mandate_authorization_guard() RETURNS trigger AS $$
BEGIN
    IF NEW.id IS DISTINCT FROM OLD.id
        OR NEW.merchant_id IS DISTINCT FROM OLD.merchant_id
        OR NEW.max_total_amount_minor IS DISTINCT FROM OLD.max_total_amount_minor
        OR NEW.currency IS DISTINCT FROM OLD.currency
        OR NEW.max_quantity IS DISTINCT FROM OLD.max_quantity
        OR NEW.valid_from IS DISTINCT FROM OLD.valid_from
        OR NEW.valid_until IS DISTINCT FROM OLD.valid_until
        OR NEW.created_at IS DISTINCT FROM OLD.created_at
    THEN
        RAISE EXCEPTION 'spending mandate authorization fields are immutable';
    END IF;

    IF OLD.status = 'REVOKED'
        AND (NEW.status IS DISTINCT FROM OLD.status
             OR NEW.revoked_at IS DISTINCT FROM OLD.revoked_at)
    THEN
        RAISE EXCEPTION 'a revoked spending mandate cannot be changed';
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql
"""

# The payment guard. Everything that identifies the payment, everything that says what it
# costs, and every timestamp already written are immutable, so a dispatch instant or a
# resolution instant cannot be moved once it exists. The lifecycle is a whitelist: an
# ADMITTED attempt may only be dispatched, a dispatched one may reach any of the three
# outcomes, and an UNKNOWN one may only be resolved by a definitive answer. SUCCEEDED and
# FAILED accept no update at all.
PAYMENT_GUARD = """
CREATE FUNCTION payment_attempt_guard() RETURNS trigger AS $$
BEGIN
    IF NEW.id IS DISTINCT FROM OLD.id
        OR NEW.merchant_id IS DISTINCT FROM OLD.merchant_id
        OR NEW.checkout_id IS DISTINCT FROM OLD.checkout_id
        OR NEW.mandate_id IS DISTINCT FROM OLD.mandate_id
        OR NEW.reservation_id IS DISTINCT FROM OLD.reservation_id
        OR NEW.idempotency_key IS DISTINCT FROM OLD.idempotency_key
        OR NEW.amount_minor IS DISTINCT FROM OLD.amount_minor
        OR NEW.currency IS DISTINCT FROM OLD.currency
        OR NEW.created_at IS DISTINCT FROM OLD.created_at
    THEN
        RAISE EXCEPTION 'payment attempt ownership, money and identity are immutable';
    END IF;

    IF OLD.dispatched_at IS NOT NULL AND NEW.dispatched_at IS DISTINCT FROM OLD.dispatched_at
    THEN
        RAISE EXCEPTION 'a payment attempt dispatch time cannot be moved';
    END IF;

    IF OLD.resolved_at IS NOT NULL AND NEW.resolved_at IS DISTINCT FROM OLD.resolved_at THEN
        RAISE EXCEPTION 'a payment attempt resolution time cannot be moved';
    END IF;

    IF OLD.status IN ('SUCCEEDED', 'FAILED') THEN
        RAISE EXCEPTION 'a % payment attempt cannot be changed', lower(OLD.status);
    END IF;

    IF (OLD.status, NEW.status) NOT IN (
        ('ADMITTED', 'ADMITTED'),
        ('ADMITTED', 'IN_FLIGHT'),
        ('IN_FLIGHT', 'IN_FLIGHT'),
        ('IN_FLIGHT', 'SUCCEEDED'),
        ('IN_FLIGHT', 'FAILED'),
        ('IN_FLIGHT', 'UNKNOWN'),
        ('UNKNOWN', 'UNKNOWN'),
        ('UNKNOWN', 'SUCCEEDED'),
        ('UNKNOWN', 'FAILED')
    ) THEN
        RAISE EXCEPTION 'payment attempt status cannot go from % to %', OLD.status, NEW.status;
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql
"""

ATTACH_PAYMENT_GUARD = """
CREATE TRIGGER payment_attempt_guard
BEFORE UPDATE ON payment_attempt
FOR EACH ROW EXECUTE FUNCTION payment_attempt_guard()
"""


def upgrade() -> None:
    op.create_unique_constraint(
        "uq_checkout_session_payment_target",
        "checkout_session",
        ["id", "merchant_id", "mandate_id", "currency", "total_amount_minor"],
    )
    op.add_column(
        "checkout_session", sa.Column("paid_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.drop_constraint(op.f("ck_checkout_session_status_known"), "checkout_session", type_="check")
    op.create_check_constraint(
        op.f("ck_checkout_session_status_known"),
        "checkout_session",
        "status IN ('OPEN', 'CANCELLED', 'PAID')",
    )
    op.create_check_constraint(
        op.f("ck_checkout_session_paid_at_matches_status"),
        "checkout_session",
        "(status = 'PAID') = (paid_at IS NOT NULL)",
    )

    op.create_unique_constraint(
        op.f("uq_inventory_reservation_id_merchant_id_checkout_id"),
        "inventory_reservation",
        ["id", "merchant_id", "checkout_id"],
    )
    op.add_column(
        "inventory_reservation", sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.drop_constraint(
        op.f("ck_inventory_reservation_status_known"), "inventory_reservation", type_="check"
    )
    op.create_check_constraint(
        op.f("ck_inventory_reservation_status_known"),
        "inventory_reservation",
        "status IN ('ACTIVE', 'COMMITTED', 'RELEASED', 'CONSUMED')",
    )
    op.create_check_constraint(
        op.f("ck_inventory_reservation_consumed_at_matches_status"),
        "inventory_reservation",
        "(status = 'CONSUMED') = (consumed_at IS NOT NULL)",
    )
    # Rebuilt over the wider predicate. One holding reservation per checkout, where holding
    # now means ACTIVE or COMMITTED, so a committed hold keeps excluding a second one.
    op.drop_index(
        "uq_inventory_reservation_active_checkout",
        table_name="inventory_reservation",
        postgresql_where=sa.text(ACTIVE_PREDICATE),
    )
    op.create_index(
        "uq_inventory_reservation_active_checkout",
        "inventory_reservation",
        ["checkout_id"],
        unique=True,
        postgresql_where=sa.text(HOLDING_PREDICATE),
    )

    op.drop_constraint(op.f("ck_audit_event_actor_type_known"), "audit_event", type_="check")
    op.create_check_constraint(
        op.f("ck_audit_event_actor_type_known"),
        "audit_event",
        "actor_type IN ('SYSTEM', 'BUYER', 'PAYMENT_PROVIDER')",
    )

    op.create_table(
        "payment_attempt",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("merchant_id", sa.Uuid(), nullable=False),
        sa.Column("checkout_id", sa.Uuid(), nullable=False),
        sa.Column("mandate_id", sa.Uuid(), nullable=False),
        sa.Column("reservation_id", sa.Uuid(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=64), nullable=False),
        sa.Column("amount_minor", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "ADMITTED",
                "IN_FLIGHT",
                "SUCCEEDED",
                "FAILED",
                "UNKNOWN",
                name="payment_attempt_status",
                native_enum=False,
                create_constraint=False,
                length=16,
            ),
            nullable=False,
        ),
        sa.Column("provider_reference", sa.String(length=128), nullable=True),
        sa.Column("failure_code", sa.String(length=64), nullable=True),
        sa.Column(
            "outcome_source",
            sa.Enum(
                "EXECUTION",
                "RECONCILIATION",
                name="outcome_source",
                native_enum=False,
                create_constraint=False,
                length=16,
            ),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("dispatched_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "(status = 'ADMITTED') = (dispatched_at IS NULL)",
            name=op.f("ck_payment_attempt_dispatched_at_matches_status"),
        ),
        sa.CheckConstraint(
            "(status IN ('ADMITTED', 'IN_FLIGHT')) = (outcome_source IS NULL)",
            name=op.f("ck_payment_attempt_outcome_source_matches_status"),
        ),
        sa.CheckConstraint(
            "(status IN ('SUCCEEDED', 'FAILED')) = (resolved_at IS NOT NULL)",
            name=op.f("ck_payment_attempt_resolved_at_matches_status"),
        ),
        sa.CheckConstraint(
            "currency ~ '^[A-Z]{3}$'", name=op.f("ck_payment_attempt_currency_format")
        ),
        sa.CheckConstraint(
            "failure_code IS NULL OR status = 'FAILED'",
            name=op.f("ck_payment_attempt_failure_code_only_when_failed"),
        ),
        sa.CheckConstraint(
            "idempotency_key ~ '^[A-Za-z0-9][A-Za-z0-9_.:-]{7,63}$'",
            name=op.f("ck_payment_attempt_idempotency_key_format"),
        ),
        sa.CheckConstraint(
            "outcome_source IS NULL OR outcome_source IN ('EXECUTION', 'RECONCILIATION')",
            name=op.f("ck_payment_attempt_outcome_source_known"),
        ),
        sa.CheckConstraint(
            "status IN ('ADMITTED', 'IN_FLIGHT', 'SUCCEEDED', 'FAILED', 'UNKNOWN')",
            name=op.f("ck_payment_attempt_status_known"),
        ),
        sa.CheckConstraint(
            "amount_minor >= 0", name=op.f("ck_payment_attempt_amount_not_negative")
        ),
        sa.CheckConstraint(
            "provider_reference IS NULL OR length(btrim(provider_reference)) > 0",
            name=op.f("ck_payment_attempt_provider_reference_not_blank"),
        ),
        sa.ForeignKeyConstraint(
            ["checkout_id", "merchant_id", "mandate_id", "currency", "amount_minor"],
            [
                "checkout_session.id",
                "checkout_session.merchant_id",
                "checkout_session.mandate_id",
                "checkout_session.currency",
                "checkout_session.total_amount_minor",
            ],
            name="fk_payment_attempt_checkout",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["reservation_id", "merchant_id", "checkout_id"],
            [
                "inventory_reservation.id",
                "inventory_reservation.merchant_id",
                "inventory_reservation.checkout_id",
            ],
            name="fk_payment_attempt_reservation",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_payment_attempt")),
        sa.UniqueConstraint("checkout_id", "idempotency_key", name="uq_payment_attempt_identity"),
    )
    op.create_index(
        op.f("ix_payment_attempt_checkout_id"), "payment_attempt", ["checkout_id"], unique=False
    )
    op.create_index(
        op.f("ix_payment_attempt_reservation_id"),
        "payment_attempt",
        ["reservation_id"],
        unique=False,
    )
    op.create_index(
        "uq_payment_attempt_checkout_succeeded",
        "payment_attempt",
        ["checkout_id"],
        unique=True,
        postgresql_where=sa.text(SUCCEEDED_PREDICATE),
    )
    op.create_index(
        "uq_payment_attempt_mandate_open",
        "payment_attempt",
        ["mandate_id"],
        unique=True,
        postgresql_where=sa.text(OPEN_PREDICATE),
    )
    op.create_index(
        "uq_payment_attempt_mandate_succeeded",
        "payment_attempt",
        ["mandate_id"],
        unique=True,
        postgresql_where=sa.text(SUCCEEDED_PREDICATE),
    )

    op.execute(MANDATE_GUARD)
    op.execute(CHECKOUT_GUARD)
    op.execute(RESERVATION_GUARD)
    op.execute(PAYMENT_GUARD)
    op.execute(ATTACH_PAYMENT_GUARD)


# The states this schema can hold that the previous one cannot express at all. Each is a fact
# about money or about a merchant's stock, and each has exactly two ways to force it through a
# downgrade: map it onto something that means a different thing, or delete it. Both falsify the
# record, so the downgrade refuses instead. Stated as a table because the message a person
# reads has to name what was found, not just that something was.
IRREVERSIBLE_STATE = (
    (
        "checkout_session",
        "status = 'PAID'",
        "a paid checkout, which the previous schema can only call OPEN or CANCELLED",
    ),
    (
        "inventory_reservation",
        "status IN ('COMMITTED', 'CONSUMED')",
        "a hold bound to a payment or already sold, which the previous schema can only call"
        " ACTIVE or RELEASED",
    ),
    (
        "payment_attempt",
        "status IN ('IN_FLIGHT', 'SUCCEEDED', 'FAILED', 'UNKNOWN')",
        "a payment attempt that has reached or may have reached a provider, which the previous"
        " schema has no table for",
    ),
)


def downgrade() -> None:
    """Reverse this migration, or refuse because reversing it would falsify the record.

    Irreversible once a payment has been taken, and deliberately so. The previous schema has no
    PAID checkout, no COMMITTED or CONSUMED reservation and no payment_attempt table at all.
    Mapping PAID onto OPEN would say a sale never happened; mapping CONSUMED onto ACTIVE would
    put sold units back on a shelf; dropping a settled attempt would erase the record of money
    moving. None of those is a downgrade, they are a rewrite of financial history, so this
    checks first and refuses with a message naming what it found.

    An ADMITTED attempt is the one payment state that does not block the reversal. It has
    provably never reached a provider, because IN_FLIGHT is committed before any network call,
    so dropping it loses an authorization rather than a movement of money. In practice
    admission also commits the hold it names, so a real ADMITTED attempt arrives here beside a
    COMMITTED reservation and the second check refuses anyway.

    The refusal is intentional rather than a constraint violation discovered halfway through.
    Letting the narrowing fail on its own would produce a check constraint error naming a
    constraint, with nothing about what it means or what to do, after some of the reversal had
    already been attempted. The whole run is one transaction, so nothing is half applied and
    the database stays at head either way.
    """
    _require_reversible()

    # The guards go back to the blacklists they were, which is correct again once the
    # statuses they did not cover no longer exist. Restoring them by CREATE OR REPLACE rather
    # than dropping and recreating keeps the triggers attached throughout.
    op.execute(RESERVATION_GUARD_BLACKLIST)
    op.execute(CHECKOUT_GUARD_BLACKLIST)
    op.execute(MANDATE_GUARD_BLACKLIST)

    # Dropping the table takes its trigger with it, and DROP is not an UPDATE, so the guard
    # does not stand in the way. The function is schema level and has to be dropped by name,
    # otherwise a downgrade leaves an orphan behind and the next upgrade fails on
    # CREATE FUNCTION.
    op.drop_index(
        "uq_payment_attempt_mandate_succeeded",
        table_name="payment_attempt",
        postgresql_where=sa.text(SUCCEEDED_PREDICATE),
    )
    op.drop_index(
        "uq_payment_attempt_mandate_open",
        table_name="payment_attempt",
        postgresql_where=sa.text(OPEN_PREDICATE),
    )
    op.drop_index(
        "uq_payment_attempt_checkout_succeeded",
        table_name="payment_attempt",
        postgresql_where=sa.text(SUCCEEDED_PREDICATE),
    )
    op.drop_index(op.f("ix_payment_attempt_reservation_id"), table_name="payment_attempt")
    op.drop_index(op.f("ix_payment_attempt_checkout_id"), table_name="payment_attempt")
    op.drop_table("payment_attempt")
    op.execute("DROP FUNCTION payment_attempt_guard()")

    op.drop_constraint(op.f("ck_audit_event_actor_type_known"), "audit_event", type_="check")
    op.create_check_constraint(
        op.f("ck_audit_event_actor_type_known"),
        "audit_event",
        "actor_type IN ('SYSTEM', 'BUYER')",
    )

    op.drop_index(
        "uq_inventory_reservation_active_checkout",
        table_name="inventory_reservation",
        postgresql_where=sa.text(HOLDING_PREDICATE),
    )
    op.create_index(
        "uq_inventory_reservation_active_checkout",
        "inventory_reservation",
        ["checkout_id"],
        unique=True,
        postgresql_where=sa.text(ACTIVE_PREDICATE),
    )
    op.drop_constraint(
        op.f("ck_inventory_reservation_consumed_at_matches_status"),
        "inventory_reservation",
        type_="check",
    )
    op.drop_constraint(
        op.f("ck_inventory_reservation_status_known"), "inventory_reservation", type_="check"
    )
    op.create_check_constraint(
        op.f("ck_inventory_reservation_status_known"),
        "inventory_reservation",
        "status IN ('ACTIVE', 'RELEASED')",
    )
    op.drop_column("inventory_reservation", "consumed_at")
    op.drop_constraint(
        op.f("uq_inventory_reservation_id_merchant_id_checkout_id"),
        "inventory_reservation",
        type_="unique",
    )

    op.drop_constraint(
        op.f("ck_checkout_session_paid_at_matches_status"), "checkout_session", type_="check"
    )
    op.drop_constraint(op.f("ck_checkout_session_status_known"), "checkout_session", type_="check")
    op.create_check_constraint(
        op.f("ck_checkout_session_status_known"),
        "checkout_session",
        "status IN ('OPEN', 'CANCELLED')",
    )
    op.drop_column("checkout_session", "paid_at")
    op.drop_constraint("uq_checkout_session_payment_target", "checkout_session", type_="unique")


def _require_reversible() -> None:
    """Refuse the downgrade if any row holds a fact the previous schema cannot state."""
    connection = op.get_bind()
    found = []
    for table, predicate, description in IRREVERSIBLE_STATE:
        # Both halves are constants in this module. Nothing here is caller supplied.
        count = connection.exec_driver_sql(
            f"SELECT count(*) FROM {table} WHERE {predicate}"  # noqa: S608
        ).scalar_one()
        if count:
            found.append(f"{count} row(s) in {table}: {description}")

    if not found:
        return

    raise RuntimeError(
        "this downgrade is not lossless while Phase 1F payment state exists. Found "
        + "; ".join(found)
        + ". Mapping these onto the previous schema would falsify financial history, so no"
        " mapping is applied and nothing has been changed. Resolve these rows deliberately"
        " before downgrading past this revision."
    )
