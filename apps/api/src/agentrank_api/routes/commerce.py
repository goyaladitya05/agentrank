"""Commerce catalog endpoints.

Routes validate, delegate and serialize. There is no SQL here and no business rule: the
service decides what a search means, and the exception handlers installed by `create_app`
decide what an error looks like.

Retrieval only. AgentRank is not an admin CRUD surface, so there is no endpoint per table.
Catalog data is created by migrations, fixtures and later the Merchant Compiler.
"""

import uuid

from fastapi import APIRouter, status

from agentrank_api.commerce.schemas import (
    ProductDetail,
    ProductSearchRequest,
    ProductSearchResponse,
)
from agentrank_api.commerce.service import CatalogService
from agentrank_api.dependencies import SessionDep
from agentrank_api.errors import ErrorResponse

router = APIRouter(prefix="/api/v1/commerce", tags=["commerce"])


@router.get(
    "/products/{product_id}",
    response_model=ProductDetail,
    responses={status.HTTP_404_NOT_FOUND: {"model": ErrorResponse}},
)
async def get_product(product_id: uuid.UUID, session: SessionDep) -> ProductDetail:
    """Fetch one product with its merchant and all of its variants."""
    product = await CatalogService(session).get_product(product_id)
    return ProductDetail.from_model(product)


@router.post("/products/search", response_model=ProductSearchResponse)
async def search_products(
    request: ProductSearchRequest, session: SessionDep
) -> ProductSearchResponse:
    """Search one merchant's catalog.

    A POST because the criteria are a structured document rather than a handful of scalars,
    and they will grow. It is a read: nothing is modified.
    """
    criteria = request.to_criteria()
    matches = await CatalogService(session).search_products(criteria)
    return ProductSearchResponse.from_matches(matches, limit=criteria.limit)
