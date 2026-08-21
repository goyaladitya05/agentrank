"""Commerce catalog persistence models.

These are storage objects. They never leave the repository layer as themselves; the API
serializes them through the schemas in `agentrank_api.commerce.schemas`.
"""

import uuid

from sqlalchemy import CheckConstraint, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from agentrank_api.models import Base, TimestampMixin

SLUG_PATTERN = r"^[a-z0-9]+(-[a-z0-9]+)*$"


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
