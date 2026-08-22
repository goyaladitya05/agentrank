"""The intended initial state of a merchant a benchmark is run against.

A published suite pins the workload. It says nothing about the shelf. Every mission oracle is a
claim about a catalog, and a catalog is mutable in a way a published suite deliberately is not,
so a benchmark that ran against whatever happened to be in a developer's database would produce
a number nobody could reproduce and nobody could compare.

A fixture is the other half of that identity. It is an authored, versioned description of one
merchant's world: its products, its variants, their prices, their attributes and their stock. It
is written in the same `SeedProduct` and `SeedVariant` vocabulary the development catalog uses,
because a benchmark world is an ordinary catalog and inventing a second way to describe one
would be inventing a second catalog model.

Three properties, and each is load bearing.

It is versioned, and the version is part of the identity rather than a field somebody bumps.
Two worlds sharing a key and differing in version are two different targets, and a result
produced against one says nothing about the other.

It is content addressed. `fixture_content_hash` is a digest over everything a mission can read,
so editing the catalog without bumping the version is a refusal rather than a silent
reinterpretation of every historical run. This is exactly the rule `suite_content_hash` applies
to a workload, applied to the target instead.

And it names its merchant, for the same reason a suite does. A mission oracle is a statement
about one catalog, so a fixture describes one merchant and cannot be applied anywhere else.

What is deliberately not here is a merchant representation model. This is a catalog fixture and
nothing more: no policy, no delivery, no provenance and no compiled form. The Merchant Compiler
is a later phase and building its representation now would be guessing at it.
"""

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from agentrank_api.benchmark.definitions import (
    MAX_NAME_LENGTH,
    validate_key,
)
from agentrank_api.benchmark.identity import HASH_ALGORITHM, canonical_json
from agentrank_api.commerce.catalog_fixture import SeedProduct, SeedVariant


@dataclass(frozen=True, slots=True)
class BenchmarkFixture:
    """One versioned merchant world, as authored.

    `key` and `version` together are the identity a historical run records. `merchant_slug` is
    the merchant this world describes, and it is the same slug the suite authored against that
    world names, so the two are comparable without either of them referring to the other.

    Immutable once it has been registered anywhere. Nothing here can change without changing
    `content_hash`, and a registration under an existing key and version with a different hash
    is refused.
    """

    key: str
    version: int
    merchant_slug: str
    merchant_name: str
    products: tuple[SeedProduct, ...]

    def __post_init__(self) -> None:
        validate_key(self.key, "fixture key")
        validate_key(self.merchant_slug, "merchant slug")
        if self.version < 1:
            raise ValueError(f"fixture version must be at least 1, got {self.version}")
        if not self.merchant_name.strip():
            raise ValueError("a fixture merchant name must not be blank")
        if len(self.merchant_name) > MAX_NAME_LENGTH:
            raise ValueError(f"a merchant name must be at most {MAX_NAME_LENGTH} characters")
        if not self.products:
            raise ValueError("a benchmark fixture must describe at least one product")

        external_ids = [product.external_id for product in self.products]
        if len(set(external_ids)) != len(external_ids):
            raise ValueError("product external identifiers must be unique within a fixture")

        skus = [variant.sku for product in self.products for variant in product.variants]
        if not skus:
            raise ValueError("a benchmark fixture must describe at least one variant")
        if len(set(skus)) != len(skus):
            # A SKU is unique per merchant at the database, and it is also the key the catalog
            # pin is built from. Two variants sharing one would make the pin describe a world
            # with fewer variants than the fixture has.
            raise ValueError("variant SKUs must be unique within a fixture")

        for product in self.products:
            for variant in product.variants:
                if variant.inventory_quantity < 0:
                    raise ValueError(f"variant {variant.sku} cannot stock a negative quantity")
                _require_exact_attributes(variant)

    @property
    def label(self) -> str:
        """How this world is named in a report or on a command line."""
        return f"{self.key}@{self.version}"

    @property
    def content_hash(self) -> str:
        """The labelled digest of everything about this world a mission can read."""
        return fixture_content_hash(self)

    def to_payload(self) -> dict[str, Any]:
        """The semantically relevant content of this fixture, as a plain JSON object.

        Separate from the digest so a test can assert what is inside it rather than only that
        two digests differ. A field that belongs in the identity and is missing here is a field
        an author could change without the version noticing.

        Titles and descriptions are in it, unlike a suite's display name, and the difference is
        not an inconsistency. A suite name is a label nothing reads; product prose is what the
        merchant's own search matches on, so editing it changes what a buyer can find.
        """
        return {
            "key": self.key,
            "version": self.version,
            "merchant_slug": self.merchant_slug,
            "merchant_name": self.merchant_name,
            "products": [_product_payload(product) for product in self.products],
        }


def fixture_content_hash(fixture: BenchmarkFixture) -> str:
    """A labelled digest of one fixture's authored content.

    Same canonicalisation and same shape as `suite_content_hash`, so one check constraint
    pattern describes every digest this schema stores. Product and variant order is preserved
    rather than sorted, because the order a catalog is seeded in is part of the authored
    definition and reordering it is an edit like any other.
    """
    digest = hashlib.sha256(canonical_json(fixture.to_payload()).encode("utf-8"))
    return f"{HASH_ALGORITHM}:{digest.hexdigest()}"


def _product_payload(product: SeedProduct) -> dict[str, Any]:
    return {
        "external_id": product.external_id,
        "title": product.title,
        "description": product.description,
        "category": product.category,
        "is_active": product.is_active,
        "variants": [_variant_payload(variant) for variant in product.variants],
    }


def _variant_payload(variant: SeedVariant) -> dict[str, Any]:
    return {
        "sku": variant.sku,
        "label": variant.label,
        "price_amount_minor": variant.price_amount_minor,
        "currency": variant.currency,
        "inventory_quantity": variant.inventory_quantity,
        "attributes": dict(variant.attributes),
        "is_active": variant.is_active,
    }


def _require_exact_attributes(variant: SeedVariant) -> None:
    """Refuse an attribute value that would not survive being stored and read back.

    The same rule a mission constraint value follows, for the same reason. Variant attributes
    are written to JSONB, PostgreSQL stores a JSON number as `numeric`, and a floating point
    value can come back as a different float than it went in as. That would move the catalog
    pin of a run against a world nobody edited, which is the one guarantee a pin exists for.

    Whole numbers, text and booleans round trip exactly, and no commerce attribute this
    benchmark can decide against needs anything else: a wattage, a length and a port count are
    all integers.
    """
    for name, value in variant.attributes.items():
        members: Sequence[Any] = value if isinstance(value, list | tuple) else (value,)
        for member in members:
            if isinstance(member, float):
                raise ValueError(
                    f"variant {variant.sku} attribute {name!r} must be a whole number, text or"
                    f" a boolean, got the fractional value {member!r}"
                )
