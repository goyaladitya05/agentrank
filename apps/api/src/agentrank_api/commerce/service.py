"""Catalog application service.

Business facing catalog behavior lives here, not in routes and not in the repository.
Routes validate a request, call one of these methods and serialize the result.
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

    async def get_product(self, product_id: uuid.UUID) -> Product:
        """Fetch a product with its merchant and variants.

        Raises rather than returning None. A caller asking for a specific product by
        identifier has already decided it should exist, and every caller turning None into
        the same error is worse than raising it once.
        """
        product = await self._catalog.get_product(product_id)
        if product is None:
            raise NotFoundError("product", str(product_id))
        return product

    async def search_products(self, criteria: ProductSearchCriteria) -> list[ProductMatch]:
        """Find products in one merchant's catalog.

        Merchant scope is part of the criteria and is applied in the query, so a search
        cannot return another merchant's catalog even by accident.
        """
        return await self._catalog.search_products(criteria)
