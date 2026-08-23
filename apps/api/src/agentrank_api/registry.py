"""Importing this registers every persistent table on `Base.metadata`.

SQLAlchemy learns about a table when the module defining it is imported, and a composite foreign
key cannot be resolved until the table it points at is registered. This schema is full of them,
so a process that imports one package and not another holds metadata that raises
`NoReferencedTableError` at first use rather than failing at import, which is a confusing way to
discover a missing import.

One list, in one place. It used to be repeated in the Alembic environment, in the test fixtures
and in every script, which meant a new package was registered wherever somebody remembered.
"""

from agentrank_api.audit import models as audit_models  # noqa: F401
from agentrank_api.auth import models as auth_models  # noqa: F401
from agentrank_api.benchmark import models as benchmark_models  # noqa: F401
from agentrank_api.checkout import models as checkout_models  # noqa: F401
from agentrank_api.commerce import models as commerce_models  # noqa: F401
from agentrank_api.constraints import models as constraint_models  # noqa: F401
from agentrank_api.inventory import models as inventory_models  # noqa: F401
from agentrank_api.mandates import models as mandate_models  # noqa: F401
from agentrank_api.models import Base
from agentrank_api.payments import models as payment_models  # noqa: F401
from agentrank_api.razorpay import models as razorpay_models  # noqa: F401
from agentrank_api.representation import models as representation_models  # noqa: F401

__all__ = ["Base"]
