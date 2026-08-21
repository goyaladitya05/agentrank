"""Authoritative semantic constraint persistence.

An `IntentConstraintSet` is the second half of an authorization. A `SpendingMandate` says
how much may be spent; this says what may be bought. Both are authoritative persisted
rows, both are immutable once written, and a future payment has to satisfy both.

It exists because a `BuyerIntent` is not authorization data. An intent is a desire, it is
not persisted, and the only place one survives is an audit payload. Reading a constraint
back out of the append only log to enforce it would make the log the authorization
database, which inverts what append only storage is for. So the constraints that must be
obeyed are extracted, validated and written to their own tables. See docs/security.md.

Four properties are enforced by the database rather than by application code, because the
database is the only layer that cannot be bypassed:

- a constraint set belongs to exactly one mandate, and to that mandate's merchant, both
  through composite foreign keys and a unique constraint
- a constraint belongs to the same merchant as its set, again structurally
- a constraint's kind, operator and value shape agree with each other, so a `GTE` whose
  value is text or an allowed category with no list is not representable
- neither table can be updated or deleted, so an authorization cannot be loosened after
  it was granted

The last one is a trigger rather than a constraint, because it is a rule about a
transition rather than about a row. See the migration for the statement itself.
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKeyConstraint,
    Index,
    String,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from agentrank_api.constraints.rules import (
    MAX_ATTRIBUTE_KEY_LENGTH,
    ConstraintOperator,
    ConstraintValue,
    IntentConstraintSpec,
    PersistedConstraintKind,
)
from agentrank_api.models import Base

# Stored as text with check constraints rather than as native PostgreSQL enums, for the
# same reason as every other enumeration in this repository: adding a value should be an
# ordinary constraint change rather than an ALTER TYPE.
# `values_callable` is what makes the column hold `allowed_category` rather than
# `ALLOWED_CATEGORY`. SQLAlchemy stores an enumeration's member names by default, and
# every other enumeration in this repository happens to name its members after their
# values. This one does not: the stored value is the same identifier a `BuyerIntent`
# constraint uses, so one vocabulary spans the request, the row and the audit payload.
CONSTRAINT_KIND = Enum(
    PersistedConstraintKind,
    native_enum=False,
    create_constraint=False,
    validate_strings=True,
    values_callable=lambda kinds: [kind.value for kind in kinds],
    length=32,
    name="intent_constraint_kind",
)

CONSTRAINT_OPERATOR = Enum(
    ConstraintOperator,
    native_enum=False,
    create_constraint=False,
    validate_strings=True,
    length=8,
    name="intent_constraint_operator",
)

_KIND_VALUES = ", ".join(f"'{kind.value}'" for kind in PersistedConstraintKind)
_OPERATOR_VALUES = ", ".join(f"'{operator.value}'" for operator in ConstraintOperator)


class IntentConstraintSet(Base):
    """The hard constraints one authorization must satisfy, as one indivisible group.

    There is no `updated_at` and no status. A constraint set has no lifecycle at all: it
    is written once with the mandate it belongs to and then only read. Changing what a
    buyer requires means a new mandate with a new set, which leaves the original intact
    and auditable rather than rewriting what was authorized.

    One set per mandate, enforced by a unique constraint. Without it a caller could
    evaluate a checkout against whichever of several sets happened to be convenient, and
    an authorization someone can choose the terms of is not an authorization.
    """

    __tablename__ = "intent_constraint_set"
    __table_args__ = (
        # Named explicitly. The metadata convention would generate a 64 character name and
        # PostgreSQL truncates identifiers at 63 bytes, so the name in the migration and
        # the name in the database would silently disagree.
        #
        # The mandate is reached through (id, merchant_id), so a constraint set cannot be
        # attached to a mandate granted to a different merchant. Merchant integrity is
        # transitive through it, since spending_mandate.merchant_id already references
        # merchant, which is why there is no second foreign key straight to merchant.
        #
        # RESTRICT, not CASCADE. Authorization data is not catalog data.
        ForeignKeyConstraint(
            ["mandate_id", "merchant_id"],
            ["spending_mandate.id", "spending_mandate.merchant_id"],
            name="fk_intent_constraint_set_mandate",
            ondelete="RESTRICT",
        ),
        # One set per mandate, and the index for looking a set up by its mandate, which is
        # the only way anything ever finds one.
        UniqueConstraint("mandate_id"),
        # Redundant against the primary key, and present only as a composite foreign key
        # target, so a constraint row carries its merchant structurally.
        UniqueConstraint("id", "merchant_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid7)
    merchant_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    mandate_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    constraints: Mapped[list[IntentConstraint]] = relationship(
        back_populates="constraint_set",
        lazy="raise_on_sql",
        cascade="all, delete-orphan",
        # Version 7 identifiers are time ordered, so this is the order the constraints
        # were authorized in rather than an arbitrary but stable one.
        order_by="IntentConstraint.id",
    )


class IntentConstraint(Base):
    """One rule a checkout must satisfy, stored so that it can be evaluated, not parsed.

    `kind` and `operator` are columns rather than fields inside the JSON document, so the
    shape of an authorization is readable in SQL and enforceable by check constraints.
    Only the comparison value is JSONB, because it is genuinely heterogeneous: `"black"`,
    `100`, `true` and `["chargers", "cables"]` are all legitimate.
    """

    __tablename__ = "intent_constraint"
    __table_args__ = (
        # Named explicitly, for the same length reason as the set's foreign key above.
        ForeignKeyConstraint(
            ["constraint_set_id", "merchant_id"],
            ["intent_constraint_set.id", "intent_constraint_set.merchant_id"],
            name="fk_intent_constraint_constraint_set",
            ondelete="CASCADE",
        ),
        # One rule per target. Two EQ constraints on one attribute are a contradiction
        # rather than a tighter bound, and two allowed category rows would split a rule
        # whose members mean "any one of these". NULLS NOT DISTINCT is what makes the
        # second case reachable by this constraint at all, since attribute_key is null
        # for an allowed category. A range still works: GTE and LTE differ in operator.
        UniqueConstraint(
            "constraint_set_id",
            "kind",
            "attribute_key",
            "operator",
            name="uq_intent_constraint_target",
            postgresql_nulls_not_distinct=True,
        ),
        CheckConstraint(f"kind IN ({_KIND_VALUES})", name="kind_known"),
        CheckConstraint(f"operator IN ({_OPERATOR_VALUES})", name="operator_known"),
        # CASE rather than OR, because PostgreSQL does not promise to evaluate the sides
        # of an OR in order and jsonb_array_length raises on a value that is not an array.
        CheckConstraint(
            "jsonb_typeof(value) IN ('string', 'number', 'boolean', 'array')"
            " AND CASE WHEN jsonb_typeof(value) = 'array'"
            " THEN jsonb_array_length(value) > 0 ELSE true END",
            name="value_shape",
        ),
        # An allowed category names no attribute and always compares with IN. A required
        # attribute always names one. Neither shape can be written as the other.
        CheckConstraint(
            "CASE kind"
            " WHEN 'allowed_category'"
            " THEN attribute_key IS NULL AND operator = 'IN'"
            " WHEN 'required_attribute'"
            " THEN attribute_key IS NOT NULL AND length(btrim(attribute_key)) > 0"
            " ELSE false END",
            name="kind_shape",
        ),
        # The comparison the operator promises has to be possible. IN needs a list, an
        # ordering comparison needs a number, and everything else needs a single value.
        CheckConstraint(
            "CASE operator"
            " WHEN 'IN' THEN jsonb_typeof(value) = 'array'"
            " WHEN 'GTE' THEN jsonb_typeof(value) = 'number'"
            " WHEN 'LTE' THEN jsonb_typeof(value) = 'number'"
            " ELSE jsonb_typeof(value) <> 'array' END",
            name="operator_value_shape",
        ),
        # The composite foreign key does not create an index on the referencing side, and
        # both loading a set's constraints and the cascade delete need one.
        Index(None, "constraint_set_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid7)
    constraint_set_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    merchant_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    kind: Mapped[PersistedConstraintKind] = mapped_column(CONSTRAINT_KIND, nullable=False)
    attribute_key: Mapped[str | None] = mapped_column(
        String(MAX_ATTRIBUTE_KEY_LENGTH), nullable=True
    )
    operator: Mapped[ConstraintOperator] = mapped_column(CONSTRAINT_OPERATOR, nullable=False)
    # No server default. A constraint with no value authorizes nothing and asks nothing,
    # and defaulting one into existence would be a rule that silently passes.
    value: Mapped[Any] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    constraint_set: Mapped[IntentConstraintSet] = relationship(
        back_populates="constraints", lazy="raise_on_sql"
    )

    def to_spec(self) -> IntentConstraintSpec:
        """The validated domain form of this row.

        Building the spec revalidates the row, so a constraint that reached the table
        around the application still cannot reach the evaluator misshapen. The evaluator
        works on specs rather than on ORM objects for exactly that reason.
        """
        stored: ConstraintValue = tuple(self.value) if isinstance(self.value, list) else self.value
        return IntentConstraintSpec(
            kind=self.kind,
            attribute_key=self.attribute_key,
            operator=self.operator,
            value=stored,
        )
