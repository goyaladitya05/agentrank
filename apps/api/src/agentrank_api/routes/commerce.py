"""Commerce catalog endpoints.

Routes validate, delegate and serialize. There is no SQL here and no business rule: the
service decides what a search means, and the exception handlers installed by `create_app`
decide what an error looks like.

Retrieval only. AgentRank is not an admin CRUD surface, so there is no endpoint per table.
Catalog data is created by migrations, fixtures and later the Merchant Compiler.

Both operations require an authenticated merchant and return only that merchant's catalog.
Phase 1H asked whether they should, and the answer is worth writing where the routes are:

- these responses carry `inventory_quantity`, which is an exact stock level
- `include_inactive` returns products and variants the merchant has deactivated, which are by
  definition not on a storefront
- `external_id` is the merchant's own identifier for a product, from their own systems

A storefront is public. Those three fields are not a storefront, they are a merchant's
commercial position, and publishing them is a product decision nobody has made. So the catalog
is merchant private, deliberately, and a genuinely public projection for buyer agents is a
different response shape that Phase 4 gets to design when there is a buyer agent to design it
for. Opening a route later is easy; closing one that agents already depend on is not. See
docs/architecture.md.
"""

import uuid
from typing import Any

from fastapi import APIRouter, status

from agentrank_api.commerce.schemas import (
    ProductDetail,
    ProductSearchRequest,
    ProductSearchResponse,
)
from agentrank_api.commerce.service import CatalogService
from agentrank_api.dependencies import MerchantDep, SessionDep
from agentrank_api.errors import ErrorResponse

router = APIRouter(prefix="/api/v1/commerce", tags=["commerce"])

# Annotated because FastAPI types this parameter as an invariant mapping of Any.
UNAUTHENTICATED: dict[int | str, dict[str, Any]] = {
    status.HTTP_401_UNAUTHORIZED: {"model": ErrorResponse}
}
NOT_FOUND: dict[int | str, dict[str, Any]] = UNAUTHENTICATED | {
    status.HTTP_404_NOT_FOUND: {"model": ErrorResponse}
}


@router.get("/products/{product_id}", response_model=ProductDetail, responses=NOT_FOUND)
async def get_product(
    product_id: uuid.UUID, session: SessionDep, merchant: MerchantDep
) -> ProductDetail:
    """Fetch one of this merchant's products, with its merchant and all of its variants.

    Another merchant's product answers 404, and so does an identifier nobody has ever used.
    """
    product = await CatalogService(session).get_product(
        product_id, merchant_id=merchant.merchant_id
    )
    return ProductDetail.from_model(product)


@router.post("/products/search", response_model=ProductSearchResponse, responses=UNAUTHENTICATED)
async def search_products(
    request: ProductSearchRequest, session: SessionDep, merchant: MerchantDep
) -> ProductSearchResponse:
    """Search this merchant's catalog.

    A POST because the criteria are a structured document rather than a handful of scalars,
    and they will grow. It is a read: nothing is modified.

    The merchant comes from the credential and the body cannot name one, so a search cannot be
    pointed at somebody else's shelves. There is no cross merchant 404 to write about here,
    because there is no identifier to guess: an unauthorized search returns this merchant's
    catalog or nothing at all.
    """
    criteria = request.to_criteria(merchant.merchant_id)
    matches = await CatalogService(session).search_products(criteria)
    return ProductSearchResponse.from_matches(matches, limit=criteria.limit)
