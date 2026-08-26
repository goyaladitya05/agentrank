"""Catalog application service.

Business facing catalog behavior lives here, not in routes and not in the repository.
Routes validate a request, call one of these methods and serialize the result.

Both methods are merchant scoped and both require an authenticated merchant over HTTP. That is
Phase 1H's explicit answer to a question the code had never been asked: whether a catalog read
is public. It is not, because of what these particular responses contain rather than because a
catalog is secret. See docs/architecture.md.
"""

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from agentrank_api.commerce.models import Product
from agentrank_api.commerce.repository import CatalogRepository
from agentrank_api.commerce.search import ProductMatch, ProductSearchCriteria
from agentrank_api.errors import NotFoundError


class CatalogService:
    def __init__(self, session: AsyncSession) -> None:
        self._catalog = CatalogRepository(session)

    async def get_product(self, product_id: uuid.UUID, *, merchant_id: uuid.UUID) -> Product:
        """Fetch one merchant's product with its merchant and variants.

        Raises rather than returning None. A caller asking for a specific product by
        identifier has already decided it should exist, and every caller turning None into
        the same error is worse than raising it once.

        Another merchant's product raises the same error as one that does not exist. The
        catalog is merchant private in this system, which is a decision rather than an
        oversight: what these responses carry is stock levels, deactivated products and a
        merchant's own external identifiers, none of which a storefront publishes. See
        docs/architecture.md.
        """
        product = await self._catalog.get_product(product_id, merchant_id=merchant_id)
        if product is None:
            raise NotFoundError("product", str(product_id))
        return product

    async def search_products(self, criteria: ProductSearchCriteria) -> list[ProductMatch]:
        """Find products in one merchant's catalog.

        Merchant scope is part of the criteria and is applied in the query, so a search
        cannot return another merchant's catalog even by accident.
        """
        return await self._catalog.search_products(criteria)
