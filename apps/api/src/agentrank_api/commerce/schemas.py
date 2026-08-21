"""API request and response models for the catalog.

Separate from the persistence models on purpose. These are the contract; the tables are an
implementation detail, and the two are free to diverge. Mapping is written out rather than
inferred from attributes so that adding a column never silently changes the API.

Everything a buyer needs is a field. Nothing here requires parsing a presentation string:
an amount is an integer of minor units and its currency is next to it.
"""

import uuid
from typing import Any, Self

from pydantic import BaseModel, Field, model_validator

from agentrank_api.commerce.models import Merchant, Product, Variant
from agentrank_api.commerce.search import (
    DEFAULT_SEARCH_LIMIT,
    MAX_SEARCH_LIMIT,
    ProductMatch,
    ProductSearchCriteria,
    validate_price_filter,
)


class MerchantSummary(BaseModel):
    id: uuid.UUID
    slug: str
    name: str

    @classmethod
    def from_model(cls, merchant: Merchant) -> Self:
        return cls(id=merchant.id, slug=merchant.slug, name=merchant.name)


class VariantView(BaseModel):
    id: uuid.UUID
    sku: str
    label: str | None
    attributes: dict[str, Any]
    price_amount_minor: int
    currency: str
    inventory_quantity: int
    is_active: bool

    @classmethod
    def from_model(cls, variant: Variant) -> Self:
        return cls(
            id=variant.id,
            sku=variant.sku,
            label=variant.label,
            attributes=variant.attributes,
            price_amount_minor=variant.price_amount_minor,
            currency=variant.currency,
            inventory_quantity=variant.inventory_quantity,
            is_active=variant.is_active,
        )


class ProductIdentity(BaseModel):
    """The product fields both responses share."""

    id: uuid.UUID
    external_id: str
    title: str
    description: str | None
    category: str | None
    is_active: bool
    merchant: MerchantSummary


class ProductDetail(ProductIdentity):
    """A single product with every variant it has, active or not."""

    variants: list[VariantView]

    @classmethod
    def from_model(cls, product: Product) -> Self:
        return cls(
            id=product.id,
            external_id=product.external_id,
            title=product.title,
            description=product.description,
            category=product.category,
            is_active=product.is_active,
            merchant=MerchantSummary.from_model(product.merchant),
            variants=[VariantView.from_model(variant) for variant in product.variants],
        )


class ProductSearchResult(ProductIdentity):
    """A search hit.

    `eligible_variants` holds only the variants that satisfied the filters, which is not
    the same thing as the product's variants. A caller must quote a price from here, not
    from the product as a whole.
    """

    eligible_variants: list[VariantView]

    @classmethod
    def from_match(cls, match: ProductMatch) -> Self:
        return cls(
            id=match.product.id,
            external_id=match.product.external_id,
            title=match.product.title,
            description=match.product.description,
            category=match.product.category,
            is_active=match.product.is_active,
            merchant=MerchantSummary.from_model(match.product.merchant),
            eligible_variants=[
                VariantView.from_model(variant) for variant in match.eligible_variants
            ],
        )


class ProductSearchRequest(BaseModel):
    merchant_id: uuid.UUID
    query: str | None = Field(default=None, max_length=200)
    max_price_amount_minor: int | None = Field(default=None, ge=0)
    currency: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")
    include_inactive: bool = False
    limit: int = Field(default=DEFAULT_SEARCH_LIMIT, ge=1, le=MAX_SEARCH_LIMIT)

    @model_validator(mode="after")
    def price_filter_states_its_currency(self) -> Self:
        # The same rule the domain criteria enforce. Checking it here as well is what turns
        # the refusal into a 422 that names the field rather than a 500.
        validate_price_filter(self.max_price_amount_minor, self.currency)
        return self

    def to_criteria(self) -> ProductSearchCriteria:
        return ProductSearchCriteria(
            merchant_id=self.merchant_id,
            query=self.query,
            max_price_amount_minor=self.max_price_amount_minor,
            currency=self.currency,
            include_inactive=self.include_inactive,
            limit=self.limit,
        )


class ProductSearchResponse(BaseModel):
    """Results plus the bound that produced them.

    `limit` is echoed so a caller can tell a short result set from a truncated one.
    """

    results: list[ProductSearchResult]
    count: int
    limit: int

    @classmethod
    def from_matches(cls, matches: list[ProductMatch], *, limit: int) -> Self:
        results = [ProductSearchResult.from_match(match) for match in matches]
        return cls(results=results, count=len(results), limit=limit)
