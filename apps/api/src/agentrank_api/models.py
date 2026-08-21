"""Declarative base and shared metadata conventions.

The naming convention is fixed here, before the first table exists. Without it,
PostgreSQL invents names for indexes, unique constraints, check constraints and foreign
keys, and an Alembic downgrade cannot reliably drop a constraint it cannot name. Changing
the convention after tables exist means renaming every constraint in a migration.
"""

from sqlalchemy import MetaData
from sqlalchemy.orm import DeclarativeBase

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
