"""What an import produced, before any of it is source history.

A draft is evidence that has been read and normalized and has not been believed yet. It is not a
source snapshot, it is not a second source truth, and nothing downstream of the merchant's
confirmation ever reads one: the confirmation produces an ordinary immutable
`MerchantSourceSnapshot` through the same intake every other source document goes through, and
from that point the draft is only a record of where the snapshot came from.

Three separate lists, because a merchant asking "what did AgentRank get from my store" is asking
three different questions:

```text
products    what was extracted, with the page and the method behind every one of them
omissions   what was found and deliberately not imported, and the reason it was not
findings    what is worth knowing about something that was imported anyway
```

An omission is the shape this whole phase turns on. A page that publishes a price with no
currency, two prices that disagree, or variants that cannot be told apart is not an import
failure and is not something to resolve by choosing. It is a product AgentRank cannot describe
honestly from public evidence, so it is left out and said out loud, and the merchant can supply
it themselves through the source document they were always able to write.

Stock is evidence here like everything else. A page that publishes a count has it read as a
count, a page that publishes only a state has that state recorded, and a page that publishes
neither says `UNKNOWN`, which is a fact about the merchant rather than a gap to fill in. Nothing
in this module ever turns one of those into another. See `canonical_document`.
"""

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Self

from agentrank_api.representation.schemas import (
    MAX_CATEGORY_LENGTH,
    MAX_DESCRIPTION_LENGTH,
    MAX_LABEL_LENGTH,
    MAX_POLICY_BODY_LENGTH,
    MAX_TITLE_LENGTH,
)


class PageKind(StrEnum):
    """What the merchant said one URL is.

    Stated by the merchant rather than guessed from the page, and that is a deliberate refusal to
    classify. Deciding "this looks like a product page" from markup is the first step of a
    heuristic crawler, it is wrong on storefronts that do not look like the ones it was written
    against, and it is unnecessary: the person who owns the store already knows which page is
    their returns policy.
    """

    PRODUCT = "PRODUCT"
    POLICY = "POLICY"


class AvailabilityEvidence(StrEnum):
    """What one page actually said about whether something can be bought.

    Three states because public storefronts publish three things, and none of them is a number.
    `UNKNOWN` is a real answer rather than a missing one: a page that says nothing about stock has
    told AgentRank something, which is that the merchant did not publish it.
    """

    IN_STOCK = "IN_STOCK"
    OUT_OF_STOCK = "OUT_OF_STOCK"
    UNKNOWN = "UNKNOWN"


class ExtractionMethod(StrEnum):
    """Which of the two things AgentRank reads produced a fact.

    Both are merchant authored machine readable data published by the page itself. There is no
    third member and adding one that meant "read out of the prose" would be adding the guess this
    importer exists without.
    """

    STRUCTURED_DATA = "STRUCTURED_DATA"
    PAGE_METADATA = "PAGE_METADATA"
    PAGE_TEXT = "PAGE_TEXT"


@dataclass(frozen=True, slots=True)
class DraftVariant:
    """One purchasable thing, as far as public evidence establishes it."""

    sku: str
    label: str | None
    price_amount_minor: int
    currency: str
    availability: AvailabilityEvidence
    availability_text: str | None
    # The exact count, on the rare page that publishes one. Almost always None, and None is not a
    # gap: it says this merchant published a state and no number, which is what storefronts do.
    inventory_quantity: int | None = None

    def payload(self) -> dict[str, Any]:
        return {
            "sku": self.sku,
            "label": self.label,
            "price_amount_minor": self.price_amount_minor,
            "currency": self.currency,
            "availability": self.availability.value,
            "availability_text": self.availability_text,
            "inventory_quantity": self.inventory_quantity,
        }

    @classmethod
    def of(cls, entry: dict[str, Any]) -> Self:
        return cls(
            sku=str(entry["sku"]),
            label=_optional_str(entry.get("label")),
            price_amount_minor=int(entry["price_amount_minor"]),
            currency=str(entry["currency"]),
            availability=AvailabilityEvidence(str(entry["availability"])),
            availability_text=_optional_str(entry.get("availability_text")),
            inventory_quantity=_optional_int(entry.get("inventory_quantity")),
        )


@dataclass(frozen=True, slots=True)
class DraftProduct:
    """One product, every field of it traceable to the page and the method that produced it."""

    external_id: str
    title: str
    description: str | None
    category: str | None
    source_url: str
    extraction: ExtractionMethod
    variants: tuple[DraftVariant, ...]

    def payload(self) -> dict[str, Any]:
        return {
            "external_id": self.external_id,
            "title": self.title,
            "description": self.description,
            "category": self.category,
            "source_url": self.source_url,
            "extraction": self.extraction.value,
            "variants": [variant.payload() for variant in self.variants],
        }

    @classmethod
    def of(cls, entry: dict[str, Any]) -> Self:
        return cls(
            external_id=str(entry["external_id"]),
            title=str(entry["title"]),
            description=_optional_str(entry.get("description")),
            category=_optional_str(entry.get("category")),
            source_url=str(entry["source_url"]),
            extraction=ExtractionMethod(str(entry["extraction"])),
            variants=tuple(DraftVariant.of(item) for item in entry["variants"]),
        )


@dataclass(frozen=True, slots=True)
class DraftPolicy:
    """One merchant policy page, as bounded prose and never as an interpreted guarantee.

    The text is the evidence. What a returns window is, whether a warranty covers a fault and
    which of them a benchmark may rely on are all decided downstream by the compiler and its
    review, which is where a disputed reading of a merchant's own words belongs.
    """

    name: str
    body: str
    source_url: str
    truncated: bool

    def payload(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "body": self.body,
            "source_url": self.source_url,
            "truncated": self.truncated,
        }

    @classmethod
    def of(cls, entry: dict[str, Any]) -> Self:
        return cls(
            name=str(entry["name"]),
            body=str(entry["body"]),
            source_url=str(entry["source_url"]),
            truncated=bool(entry["truncated"]),
        )


@dataclass(frozen=True, slots=True)
class Omission:
    """Something the page had and the draft does not, and the reason why."""

    source_url: str
    reason: str
    detail: str
    subject: str | None = None

    def payload(self) -> dict[str, Any]:
        return {
            "source_url": self.source_url,
            "reason": self.reason,
            "detail": self.detail,
            "subject": self.subject,
        }

    @classmethod
    def of(cls, entry: dict[str, Any]) -> Self:
        return cls(
            source_url=str(entry["source_url"]),
            reason=str(entry["reason"]),
            detail=str(entry["detail"]),
            subject=_optional_str(entry.get("subject")),
        )


@dataclass(frozen=True, slots=True)
class Finding:
    """Something worth saying about evidence that was imported anyway."""

    source_url: str
    code: str
    detail: str
    subject: str | None = None

    def payload(self) -> dict[str, Any]:
        return {
            "source_url": self.source_url,
            "code": self.code,
            "detail": self.detail,
            "subject": self.subject,
        }

    @classmethod
    def of(cls, entry: dict[str, Any]) -> Self:
        return cls(
            source_url=str(entry["source_url"]),
            code=str(entry["code"]),
            detail=str(entry["detail"]),
            subject=_optional_str(entry.get("subject")),
        )


@dataclass(frozen=True, slots=True)
class SourceDraft:
    """Everything one import extracted, in the order the merchant's URLs were given."""

    products: tuple[DraftProduct, ...] = ()
    policies: tuple[DraftPolicy, ...] = ()
    omissions: tuple[Omission, ...] = ()
    findings: tuple[Finding, ...] = ()

    @property
    def variant_count(self) -> int:
        return sum(len(product.variants) for product in self.products)

    @property
    def unstated_availability(self) -> tuple[tuple[str, str], ...]:
        """Every imported variant whose page said nothing about whether it can be bought.

        A page URL and a SKU each, because this is what a merchant has to act on before the
        variant can be part of an evaluation world: an isolated world holds an exact number of
        units and `UNKNOWN` is not one. Reported at review time rather than discovered at setup
        time, and never resolved here in either direction.
        """
        return tuple(
            (product.source_url, variant.sku)
            for product in self.products
            for variant in product.variants
            if variant.availability is AvailabilityEvidence.UNKNOWN
        )

    def payload(self) -> dict[str, Any]:
        return {
            "products": [product.payload() for product in self.products],
            "policies": [policy.payload() for policy in self.policies],
            "omissions": [omission.payload() for omission in self.omissions],
            "findings": [finding.payload() for finding in self.findings],
        }

    @classmethod
    def of(cls, entry: dict[str, Any]) -> Self:
        return cls(
            products=tuple(DraftProduct.of(item) for item in entry.get("products", [])),
            policies=tuple(DraftPolicy.of(item) for item in entry.get("policies", [])),
            omissions=tuple(Omission.of(item) for item in entry.get("omissions", [])),
            findings=tuple(Finding.of(item) for item in entry.get("findings", [])),
        )


def canonical_document(draft: SourceDraft) -> dict[str, Any]:
    """The draft as the ordinary source document body a merchant could have written by hand.

    This is the only place the importer produces AgentRank's canonical source shape, and it
    produces exactly that shape rather than an importer flavoured one. What comes out is what
    `SourceDocumentInput` accepts, so the existing validation, the existing bounds, the existing
    instruction-like guard and the existing content identity all apply unchanged.

    **Stock.** A source variant records an availability state and, where the merchant published
    one, an exact quantity. That is exactly the shape of what a storefront publishes, so nothing
    here has to be invented and nothing here is: a page saying "In stock" becomes `IN_STOCK` with
    no count, a page saying "Out of stock" becomes a count of zero, a page publishing an
    inventory level becomes that number, and a page saying nothing becomes `UNKNOWN`.

    `UNKNOWN` survives into the source document. It is refused later, by name, when a merchant
    asks for an evaluation world to be built from it, because a simulated shelf holds an exact
    number of units and unknown is not one. Refusing it here instead would be refusing to record
    a true thing about the merchant.
    """
    names = [policy.name for policy in draft.policies]
    if len(names) != len(set(names)):
        # A dictionary comprehension would keep the last of them and lose the rest, silently. The
        # import command already refuses two policy pages sharing a name, so this is a guard on a
        # public function rather than a reachable state, and it raises rather than choosing.
        raise ValueError("this draft has two policies under one name")
    return {
        "products": [_product(product) for product in draft.products],
        "policy_text": {policy.name: policy.body for policy in draft.policies},
    }


def _product(product: DraftProduct) -> dict[str, Any]:
    return {
        "external_id": product.external_id,
        "title": product.title[:MAX_TITLE_LENGTH],
        "description": (
            None if product.description is None else product.description[:MAX_DESCRIPTION_LENGTH]
        ),
        "category": None if product.category is None else product.category[:MAX_CATEGORY_LENGTH],
        "variants": [_variant(variant) for variant in product.variants],
        # Provenance that survives into source history, and deliberately only the part of it that
        # is stable. The URL and the method are properties of the merchant's page; a retrieval
        # timestamp is a property of the fetch, and putting one here would make every re-import of
        # an unchanged store a new source snapshot saying the same thing.
        "merchant_metadata": {
            "import_source_url": product.source_url,
            "import_extraction": product.extraction.value,
        },
    }


def _variant(variant: DraftVariant) -> dict[str, Any]:
    """One extracted variant as a canonical source variant, stock included.

    Out of stock is written as a count of zero rather than as a state with no count, because that
    is the canonical form a source document holds it in and because zero is what the page means.
    Everything else keeps whatever precision the page published.
    """
    quantity = variant.inventory_quantity
    if quantity is None and variant.availability is AvailabilityEvidence.OUT_OF_STOCK:
        quantity = 0
    metadata: dict[str, str | int | bool] = {"import_availability": variant.availability.value}
    if variant.availability_text is not None:
        metadata["import_availability_text"] = variant.availability_text
    body: dict[str, Any] = {
        "sku": variant.sku,
        "label": None if variant.label is None else variant.label[:MAX_LABEL_LENGTH],
        "price_amount_minor": variant.price_amount_minor,
        "currency": variant.currency,
        "inventory_quantity": quantity,
        "merchant_metadata": metadata,
    }
    if quantity is None:
        body["availability"] = variant.availability.value
    return body


def bounded_policy_body(text: str) -> tuple[str, bool]:
    """One policy body cut to what a source document holds, and whether cutting was needed."""
    if len(text) <= MAX_POLICY_BODY_LENGTH:
        return text, False
    return text[:MAX_POLICY_BODY_LENGTH].rstrip(), True


def _optional_str(value: object) -> str | None:
    return None if value is None else str(value)


def _optional_int(value: object) -> int | None:
    return None if value is None else int(value)  # type: ignore[call-overload]
