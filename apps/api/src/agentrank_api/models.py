"""Declarative base and shared metadata conventions.

The naming convention is fixed here, before the first table exists. Without it,
PostgreSQL invents names for indexes, unique constraints, check constraints and foreign
keys, and an Alembic downgrade cannot reliably drop a constraint it cannot name. Changing
the convention after tables exist means renaming every constraint in a migration.
"""

from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, MetaData, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """Base class for every persistent model."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)

    # Server generated values are fetched with RETURNING instead of being left expired.
    # An expired attribute would be lazy loaded on first access, and a lazy load inside
    # an async session raises MissingGreenlet rather than quietly emitting a query.
    #
    # RUF012 wants ClassVar here, but DeclarativeBase already declares __mapper_args__ as
    # an instance variable and mypy rejects narrowing it to a class variable.
    __mapper_args__: dict[str, Any] = {"eager_defaults": True}  # noqa: RUF012


class TimestampMixin:
    """Creation and modification timestamps, generated the same way everywhere.

    Both defaults are server side so that the database clock is the single source of
    truth. `updated_at` is refreshed by SQLAlchemy on ORM updates; a statement issued
    outside the ORM will not touch it.
    """

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
