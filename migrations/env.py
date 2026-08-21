"""Alembic environment.

Migrations run through a synchronous engine. Alembic's own API is synchronous, and DDL
gains nothing from async, so the async engine used by the application is not reused here.
Both use the psycopg 3 dialect, so the same URL works for either.
"""

from alembic import context
from sqlalchemy import Connection, create_engine, pool

from agentrank_api.config import Settings, get_settings
from agentrank_api.models import Base

# Importing the model modules is what registers their tables on Base.metadata.
# Autogenerate and `alembic check` see an empty schema without this.
from agentrank_api.audit import models as audit_models  # noqa: F401  isort:skip
from agentrank_api.checkout import models as checkout_models  # noqa: F401  isort:skip
from agentrank_api.commerce import models as commerce_models  # noqa: F401  isort:skip
from agentrank_api.constraints import models as constraint_models  # noqa: F401  isort:skip
from agentrank_api.inventory import models as inventory_models  # noqa: F401  isort:skip
from agentrank_api.mandates import models as mandate_models  # noqa: F401  isort:skip
from agentrank_api.payments import models as payment_models  # noqa: F401  isort:skip

config = context.config
target_metadata = Base.metadata

# Tests inject settings for a throwaway database through config.attributes rather than
# writing a URL, which keeps the password out of any config object.
settings: Settings = config.attributes.get("settings") or get_settings()


def run_migrations_offline() -> None:
    """Emit SQL to stdout without connecting, for review before a production change."""
    context.configure(
        url=settings.database_url.render_as_string(hide_password=False),
        target_metadata=target_metadata,
        literal_binds=True,
        compare_type=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Apply migrations against the configured database."""
    engine = create_engine(settings.database_url, poolclass=pool.NullPool)
    try:
        with engine.connect() as connection:
            do_run_migrations(connection)
    finally:
        engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
