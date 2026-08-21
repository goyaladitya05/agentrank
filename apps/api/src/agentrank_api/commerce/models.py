"""Commerce catalog persistence models.

These are storage objects. They never leave the repository layer as themselves; the API
serializes them through the schemas in `agentrank_api.commerce.schemas`.

Two rules are enforced by the database rather than by application code, because the
database is the only place that cannot be bypassed:

- money is a non negative integer count of minor units, and its currency is never absent
- a variant belongs to the same merchant as its product, structurally
"""

import uuid
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from agentrank_api.models import Base, TimestampMixin

SLUG_PATTERN = r"^[a-z0-9]+(-[a-z0-9]+)*$"
CURRENCY_PATTERN = r"^[A-Z]{3}$"


class Merchant(TimestampMixin, Base):
    """A seller whose catalog AgentRank benchmarks.

    Deliberately minimal. Authentication, billing, addresses and user accounts are not
    catalog concerns and do not belong here.
    """

    __tablename__ = "merchant"
    __table_args__ = (
        CheckConstraint(f"slug ~ '{SLUG_PATTERN}'", name="slug_format"),
        CheckConstraint("length(btrim(name)) > 0", name="name_not_blank"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid7)
    slug: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)


class Product(TimestampMixin, Base):
    """A merchant catalog entry.

    `description` and `category` are nullable on purpose. AgentRank exists to benchmark
    real merchants, including ones whose catalogs are incomplete, so the schema must be
    able to represent a product that is missing them rather than refusing to store it.
    """

    __tablename__ = "product"
    __table_args__ = (
        UniqueConstraint("merchant_id", "external_id"),
        # Redundant against the primary key, but a composite foreign key needs a matching
        # unique target. This is what lets a variant be tied to its merchant structurally.
        UniqueConstraint("id", "merchant_id"),
        CheckConstraint("length(btrim(external_id)) > 0", name="external_id_not_blank"),
        CheckConstraint("length(btrim(title)) > 0", name="title_not_blank"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid7)
    merchant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("merchant.id", ondelete="CASCADE"),
        nullable=False,
    )
    external_id: Mapped[str] = mapped_column(String(128), nullable=False)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    category: Mapped[str | None] = mapped_column(String(120), nullable=True)
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true"), default=True
    )

    merchant: Mapped[Merchant] = relationship(lazy="raise_on_sql")
    variants: Mapped[list[Variant]] = relationship(
        back_populates="product",
        lazy="raise_on_sql",
        cascade="all, delete-orphan",
        order_by="(Variant.price_amount_minor, Variant.sku)",
    )


class Variant(TimestampMixin, Base):
    """A purchasable configuration of a product.

    `attributes` is JSONB rather than a column per property. Merchant catalogs are
    heterogeneous: wattage, connector, colour and length are not a fixed set, and adding a
    column for each one would make every new merchant a migration.
    """

    __tablename__ = "variant"
    __table_args__ = (
        # The composite target ties a variant to its product and to that product's
        # merchant in one constraint, so a variant cannot be attributed to a merchant that
        # does not own its product.
        ForeignKeyConstraint(
            ["product_id", "merchant_id"],
            ["product.id", "product.merchant_id"],
            ondelete="CASCADE",
        ),
        UniqueConstraint("merchant_id", "sku"),
        CheckConstraint("length(btrim(sku)) > 0", name="sku_not_blank"),
        CheckConstraint("price_amount_minor >= 0", name="price_not_negative"),
        CheckConstraint(f"currency ~ '{CURRENCY_PATTERN}'", name="currency_format"),
        CheckConstraint("inventory_quantity >= 0", name="inventory_not_negative"),
        CheckConstraint("jsonb_typeof(attributes) = 'object'", name="attributes_are_an_object"),
        # Named by the metadata convention. The composite foreign key does not create an
        # index on the referencing side, and both variant lookup by product and cascading
        # deletes need one.
        Index(None, "product_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid7)
    product_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    merchant_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    sku: Mapped[str] = mapped_column(String(128), nullable=False)
    label: Mapped[str | None] = mapped_column(String(200), nullable=True)
    attributes: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb"), default=dict
    )
    price_amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    inventory_quantity: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0"), default=0
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true"), default=True
    )

    product: Mapped[Product] = relationship(back_populates="variants", lazy="raise_on_sql")
