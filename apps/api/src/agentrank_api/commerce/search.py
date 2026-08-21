"""Product search inputs and results.

These types are the seam between the API and however retrieval happens to be implemented.
Today retrieval is deterministic SQL matching. Replacing it with something better means
replacing `CatalogRepository.search_products`, not the criteria, the result, or the API.
"""

import uuid
from dataclasses import dataclass, field

from agentrank_api.commerce.models import Product, Variant

DEFAULT_SEARCH_LIMIT = 20
MAX_SEARCH_LIMIT = 100

# A query is split into tokens and every token must match. The cap keeps a pathological
# query from turning into an arbitrarily large SQL statement.
MAX_QUERY_TOKENS = 10


def validate_price_filter(max_price_amount_minor: int | None, currency: str | None) -> None:
    """Reject a price ceiling that does not say which currency it is in.

    Comparing 500000 against a price without knowing the currency is meaningless: the
    answer differs by three orders of magnitude between INR and, say, KWD. Rather than
    assume a default, the filter is refused.
    """
    if max_price_amount_minor is not None and currency is None:
        raise ValueError("max_price_amount_minor requires currency")


@dataclass(frozen=True, slots=True)
class ProductSearchCriteria:
    """What a caller is looking for.

    `include_inactive` is off by default, and it governs both products and variants: an
    inactive variant cannot make a product eligible, and it is never returned as a match.
    """

    merchant_id: uuid.UUID
    query: str | None = None
    max_price_amount_minor: int | None = None
    currency: str | None = None
    include_inactive: bool = False
    limit: int = DEFAULT_SEARCH_LIMIT

    def __post_init__(self) -> None:
        validate_price_filter(self.max_price_amount_minor, self.currency)

    def tokens(self) -> list[str]:
        """The query split into terms that must all match."""
        if self.query is None:
            return []
        return self.query.split()[:MAX_QUERY_TOKENS]


@dataclass(frozen=True, slots=True)
class ProductMatch:
    """A product together with the variants that actually satisfied the filters.

    The distinction matters. A product is returned because at least one of its variants
    qualified, and the caller needs to know which ones rather than assume the product as a
    whole is within the price ceiling.
    """

    product: Product
    eligible_variants: tuple[Variant, ...] = field(default_factory=tuple)
