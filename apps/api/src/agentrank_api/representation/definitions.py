"""Narrow, validated merchant-source and Commerce IR documents.

The source document describes what a merchant supplied.  The IR document is a separate,
agent-facing semantic interpretation of that source.  Neither shape admits benchmark data.
"""

import hashlib
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from agentrank_api.benchmark.definitions import validate_key
from agentrank_api.benchmark.identity import HASH_ALGORITHM, canonical_json
from agentrank_api.money import CURRENCY_PATTERN


class FactAuthority(StrEnum):
    AUTHORITATIVE = "AUTHORITATIVE"
    DERIVED = "DERIVED"


class FactConfidence(StrEnum):
    AUTHORITATIVE = "AUTHORITATIVE"
    HIGH = "HIGH"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"


class ReviewState(StrEnum):
    NOT_REQUIRED = "NOT_REQUIRED"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    CONFIRMED = "CONFIRMED"
    REJECTED = "REJECTED"


class ValueState(StrEnum):
    TRUE = "TRUE"
    FALSE = "FALSE"
    UNKNOWN = "UNKNOWN"
    NOT_APPLICABLE = "NOT_APPLICABLE"


class SourceAvailability(StrEnum):
    """Whether a merchant says a variant can be bought, separately from how many there are.

    Public storefronts publish a state and almost never a count, so a source model that could
    only hold a count could not record an ordinary storefront honestly. These three are what a
    merchant can actually tell you.

    `UNKNOWN` is an answer rather than a missing field. A page that says nothing about stock has
    stated that the merchant did not publish it, and that is different from saying there is none.
    Nothing downstream is allowed to turn it into either of the other two.
    """

    IN_STOCK = "IN_STOCK"
    OUT_OF_STOCK = "OUT_OF_STOCK"
    UNKNOWN = "UNKNOWN"


class AttributeKind(StrEnum):
    TEXT = "TEXT"
    INTEGER = "INTEGER"
    BOOLEAN = "BOOLEAN"
    MEASUREMENT = "MEASUREMENT"


class RepresentationProducer(StrEnum):
    MANUAL_FIXTURE = "MANUAL_FIXTURE"
    COMPILER = "COMPILER"


@dataclass(frozen=True, slots=True)
class SourceReference:
    """The smallest practical pointer to merchant supplied evidence."""

    field: str
    excerpt: str | None = None

    def __post_init__(self) -> None:
        if not self.field.strip():
            raise ValueError("provenance field must not be blank")
        if self.excerpt is not None and not self.excerpt.strip():
            raise ValueError("provenance excerpt must not be blank")

    def payload(self) -> dict[str, str | None]:
        return {"field": self.field, "excerpt": self.excerpt}


@dataclass(frozen=True, slots=True)
class SemanticFact:
    value: Any
    authority: FactAuthority
    confidence: FactConfidence
    review_state: ReviewState
    provenance: tuple[SourceReference, ...]

    def __post_init__(self) -> None:
        _json_value(self.value, "fact value")
        if not self.provenance:
            raise ValueError("every Commerce IR fact must have merchant provenance")
        if self.authority is FactAuthority.AUTHORITATIVE:
            if self.confidence is not FactConfidence.AUTHORITATIVE:
                raise ValueError("authoritative facts require authoritative confidence")
            if self.review_state is not ReviewState.NOT_REQUIRED:
                raise ValueError("authoritative facts cannot carry review workflow state")
        elif self.confidence is FactConfidence.AUTHORITATIVE:
            raise ValueError("derived facts cannot claim authoritative confidence")
        if (
            self.confidence is FactConfidence.REVIEW_REQUIRED
            and self.review_state is not ReviewState.REVIEW_REQUIRED
        ):
            raise ValueError("review-required confidence must be marked review required")
        if (
            self.review_state is ReviewState.REVIEW_REQUIRED
            and self.confidence is not FactConfidence.REVIEW_REQUIRED
        ):
            raise ValueError("review-required state needs review-required confidence")

    def payload(self) -> dict[str, Any]:
        return {
            "value": self.value,
            "authority": self.authority.value,
            "confidence": self.confidence.value,
            "review_state": self.review_state.value,
            "provenance": [reference.payload() for reference in self.provenance],
        }


@dataclass(frozen=True, slots=True)
class CommerceAttribute:
    key: str
    kind: AttributeKind
    fact: SemanticFact
    unit: str | None = None

    def __post_init__(self) -> None:
        if not self.key.strip():
            raise ValueError("attribute key must not be blank")
        value = self.fact.value
        if self.kind is AttributeKind.TEXT and not isinstance(value, str):
            raise ValueError("text attribute requires a string")
        if self.kind is AttributeKind.INTEGER and (
            not isinstance(value, int) or isinstance(value, bool)
        ):
            raise ValueError("integer attribute requires an integer")
        if self.kind is AttributeKind.BOOLEAN and not isinstance(value, bool):
            raise ValueError("boolean attribute requires a boolean")
        if self.kind is AttributeKind.MEASUREMENT:
            if (
                not isinstance(value, int)
                or isinstance(value, bool)
                or not self.unit
                or not self.unit.strip()
            ):
                raise ValueError("measurement attribute requires an integer value and unit")
        elif self.unit is not None:
            raise ValueError("only measurement attributes may carry a unit")

    def payload(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "kind": self.kind.value,
            "unit": self.unit,
            "fact": self.fact.payload(),
        }


@dataclass(frozen=True, slots=True)
class SourceVariant:
    """One purchasable thing as the merchant states it, stock included and never invented.

    Availability and quantity are one fact recorded at two precisions. A merchant who published a
    count has said everything a state could say and more; a merchant who published only "in
    stock" has said something no integer can express without inventing the rest of it.

    ```text
    inventory_quantity  0        availability is OUT_OF_STOCK
    inventory_quantity  n > 0    availability is IN_STOCK, and there are exactly n
    inventory_quantity  None     availability is IN_STOCK with no count, or UNKNOWN
    ```

    Out of stock is deliberately not a countless state. "Out of stock" is an exact quantity, and
    it is zero, so recording it as one is reading the merchant rather than deciding for them.
    That leaves exactly two things a count cannot say, which is why those two are the only states
    a countless variant may hold.

    None of this is authoritative inventory. The commerce runtime owns what can be reserved and
    sold; this is what the merchant said about themselves, frozen.
    """

    sku: str
    label: str | None
    price_amount_minor: int
    currency: str
    availability: SourceAvailability
    inventory_quantity: int | None
    merchant_metadata: dict[str, Any]

    def __post_init__(self) -> None:
        if not self.sku.strip() or self.price_amount_minor < 0:
            raise ValueError("source variant has an invalid identity or price")
        if self.inventory_quantity is not None and self.inventory_quantity < 0:
            raise ValueError("source variant inventory must not be negative")
        if not re.fullmatch(CURRENCY_PATTERN, self.currency):
            raise ValueError("source variant currency must be ISO 4217")
        if self.inventory_quantity is not None and (
            self.availability is not availability_of(self.inventory_quantity)
        ):
            raise ValueError("source variant availability contradicts the exact quantity beside it")
        if self.inventory_quantity is None and self.availability is SourceAvailability.OUT_OF_STOCK:
            raise ValueError("an out of stock source variant states a quantity of zero")
        _json_value(self.merchant_metadata, "merchant metadata")

    @property
    def purchasable(self) -> bool:
        """Whether the merchant said this can be bought. Unknown is not a yes."""
        return self.availability is SourceAvailability.IN_STOCK

    def payload(self) -> dict[str, Any]:
        """The canonical stored shape, which records each fact exactly once.

        `availability` is written only where there is no quantity to read it off. That is what
        keeps every document written before availability existed serializing to the bytes it was
        stored as, so its content hash, its compiler provenance and every representation derived
        from it stay exactly what they were.
        """
        body: dict[str, Any] = {
            "sku": self.sku,
            "label": self.label,
            "price_amount_minor": self.price_amount_minor,
            "currency": self.currency,
            "inventory_quantity": self.inventory_quantity,
            "merchant_metadata": self.merchant_metadata,
        }
        if self.inventory_quantity is None:
            body["availability"] = self.availability.value
        return body


def availability_of(quantity: int) -> SourceAvailability:
    """The availability an exact quantity already states. Zero is out of stock and nothing else."""
    return SourceAvailability.OUT_OF_STOCK if quantity == 0 else SourceAvailability.IN_STOCK


def read_availability(
    quantity: int | None, stated: str | None, *, where: str
) -> SourceAvailability:
    """One stored or submitted variant's availability, from the two fields that can carry it.

    Tolerant in one direction only. A quantity with no state is read as the state it implies,
    which is how every document written before this field existed is read. A state beside a
    quantity is accepted when the two agree and refused when they do not, so no reader ever has
    to decide which of two contradicting facts a merchant meant.
    """
    if quantity is None:
        if stated is None:
            raise ValueError(f"{where} states neither a stock quantity nor an availability")
        return _availability_member(stated, where)
    implied = availability_of(quantity)
    if stated is not None and _availability_member(stated, where) is not implied:
        raise ValueError(f"{where} states an availability its own stock quantity contradicts")
    return implied


def _availability_member(stated: str, where: str) -> SourceAvailability:
    try:
        return SourceAvailability(stated)
    except ValueError as unknown:
        raise ValueError(f"{where} states an availability AgentRank does not define") from unknown


@dataclass(frozen=True, slots=True)
class SourceProduct:
    external_id: str
    title: str
    description: str | None
    category: str | None
    variants: tuple[SourceVariant, ...]
    merchant_metadata: dict[str, Any]

    def __post_init__(self) -> None:
        if not self.external_id.strip() or not self.title.strip() or not self.variants:
            raise ValueError("source product needs identity, title and a variant")
        _json_value(self.merchant_metadata, "merchant metadata")

    def payload(self) -> dict[str, Any]:
        return {
            "external_id": self.external_id,
            "title": self.title,
            "description": self.description,
            "category": self.category,
            "variants": [variant.payload() for variant in self.variants],
            "merchant_metadata": self.merchant_metadata,
        }


@dataclass(frozen=True, slots=True)
class MerchantSourceDefinition:
    key: str
    version: int
    merchant_slug: str
    products: tuple[SourceProduct, ...]
    policy_text: dict[str, str]

    def __post_init__(self) -> None:
        validate_key(self.key, "source key")
        validate_key(self.merchant_slug, "merchant slug")
        if self.version < 1 or not self.products:
            raise ValueError("source version must be positive and source must include products")
        ids = [product.external_id for product in self.products]
        skus = [variant.sku for product in self.products for variant in product.variants]
        if len(ids) != len(set(ids)) or len(skus) != len(set(skus)):
            raise ValueError("source product identifiers and SKUs must be unique")
        if any(not name.strip() or not body.strip() for name, body in self.policy_text.items()):
            raise ValueError("source policy text must have nonblank names and bodies")

    @property
    def label(self) -> str:
        return f"{self.key}@{self.version}"

    def payload(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "version": self.version,
            "merchant_slug": self.merchant_slug,
            "products": [product.payload() for product in self.products],
            "policy_text": self.policy_text,
        }

    @property
    def content_hash(self) -> str:
        return _hash(self.payload())


@dataclass(frozen=True, slots=True)
class CommerceVariant:
    sku: str
    label: str | None
    price: SemanticFact
    availability: SemanticFact
    attributes: tuple[CommerceAttribute, ...]
    compatibility: dict[str, SemanticFact]

    def __post_init__(self) -> None:
        if not self.sku.strip():
            raise ValueError("Commerce IR variant SKU must not be blank")
        if any(
            attribute.key in {other.key for other in self.attributes[:index]}
            for index, attribute in enumerate(self.attributes)
        ):
            raise ValueError("Commerce IR attribute keys must be unique per variant")
        # Availability is a state and never a count. A representation is a discovery surface, so
        # it says whether a buyer can expect to be able to buy the thing; how many there are is a
        # question only the commerce runtime can answer, and it answers it at reservation time.
        # UNKNOWN is admissible and load bearing: a merchant who published no availability has a
        # representation that says so rather than one that says no.
        if not isinstance(self.availability.value, str) or self.availability.value not in {
            member.value for member in ValueState
        }:
            raise ValueError("a Commerce IR availability fact must use a four-state value")
        for state in self.compatibility.values():
            if not isinstance(state.value, str) or state.value not in {
                member.value for member in ValueState
            }:
                raise ValueError("compatibility facts must use a four-state value")

    def payload(self) -> dict[str, Any]:
        return {
            "sku": self.sku,
            "label": self.label,
            "price": self.price.payload(),
            "availability": self.availability.payload(),
            "attributes": [attribute.payload() for attribute in self.attributes],
            "compatibility": {key: fact.payload() for key, fact in self.compatibility.items()},
        }


@dataclass(frozen=True, slots=True)
class CommerceProduct:
    external_id: str
    title: SemanticFact
    category: SemanticFact | None
    variants: tuple[CommerceVariant, ...]
    policy_facts: dict[str, SemanticFact]

    def __post_init__(self) -> None:
        if not self.external_id.strip() or not self.variants:
            raise ValueError("Commerce IR product needs identity and variants")

    def payload(self) -> dict[str, Any]:
        return {
            "external_id": self.external_id,
            "title": self.title.payload(),
            "category": None if self.category is None else self.category.payload(),
            "variants": [variant.payload() for variant in self.variants],
            "policy_facts": {key: fact.payload() for key, fact in self.policy_facts.items()},
        }


@dataclass(frozen=True, slots=True)
class CommerceIRDefinition:
    source_key: str
    source_version: int
    source_hash: str
    producer: RepresentationProducer
    producer_version: str
    products: tuple[CommerceProduct, ...]

    def __post_init__(self) -> None:
        validate_key(self.source_key, "source key")
        if self.source_version < 1 or not self.source_hash.startswith("sha256:"):
            raise ValueError("Commerce IR must name a valid source identity")
        if not self.producer_version.strip() or not self.products:
            raise ValueError("Commerce IR needs a producer version and products")
        if len(self.producer_version) > 128:
            raise ValueError("Commerce IR producer version is too long")
        ids = [product.external_id for product in self.products]
        skus = [variant.sku for product in self.products for variant in product.variants]
        if len(ids) != len(set(ids)) or len(skus) != len(set(skus)):
            raise ValueError("Commerce IR product identities and SKUs must be unique")

    def payload(self) -> dict[str, Any]:
        return {
            "source_key": self.source_key,
            "source_version": self.source_version,
            "source_hash": self.source_hash,
            "producer": self.producer.value,
            "producer_version": self.producer_version,
            "products": [product.payload() for product in self.products],
        }

    @property
    def content_hash(self) -> str:
        return _hash(self.payload())

    def buyer_projection(self) -> dict[str, Any]:
        """Useful commerce facts only, deliberately excluding provenance and review internals."""
        return {
            "products": [
                {
                    "external_id": product.external_id,
                    "title": product.title.value,
                    "category": None if product.category is None else product.category.value,
                    "variants": [
                        {
                            "sku": variant.sku,
                            "label": variant.label,
                            "price_amount_minor": variant.price.value,
                            "availability": variant.availability.value,
                            "attributes": {
                                attribute.key: {
                                    "value": attribute.fact.value,
                                    "unit": attribute.unit,
                                }
                                for attribute in variant.attributes
                            },
                            "compatibility": {
                                key: fact.value for key, fact in variant.compatibility.items()
                            },
                        }
                        for variant in product.variants
                    ],
                    "policy_facts": {key: fact.value for key, fact in product.policy_facts.items()},
                }
                for product in self.products
            ]
        }


# Merchant prose that impersonates instructions to whatever reads it next. A compiled
# representation is handed to a buyer agent as its discovery surface, and a product field is never
# a legitimate place to address one.
#
# Narrow on purpose and not a content filter. It catches one shape this repository has decided is
# never legitimate, and everything else a merchant writes is settled by the review workflow, which
# is where a disputed fact belongs. Widening it into a general prompt-injection detector would be
# a filter this repository would have to keep in step with language.
_INSTRUCTION_LIKE = re.compile(
    r"\b(ignore|disregard)\s+(all\s+)?(previous|compiler|system)\s+instructions?\b",
    re.IGNORECASE,
)


def instruction_like(text: str) -> bool:
    """Whether one merchant string impersonates instructions to whatever reads it next."""
    return _INSTRUCTION_LIKE.search(text) is not None


def _hash(payload: dict[str, Any]) -> str:
    digest = hashlib.sha256(canonical_json(payload).encode("utf-8"))
    return f"{HASH_ALGORITHM}:{digest.hexdigest()}"


def _json_value(value: Any, name: str) -> None:
    try:
        canonical_json({"value": value})
    except (TypeError, ValueError) as error:
        raise ValueError(f"{name} must be JSON serializable") from error
