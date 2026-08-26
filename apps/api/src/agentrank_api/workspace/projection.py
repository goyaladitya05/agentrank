"""One merchant source snapshot, projected into the isolated catalog a benchmark is run against.

This is the narrowest useful reading of merchant evidence there is: every field is copied, none
is interpreted, and nothing is added. A product keeps its identifier, its title, its prose and
its category. A variant keeps its SKU, its label, its price, its currency and its stock. A
variant's structured merchant metadata becomes the variant's typed attributes, verbatim, because
that is already structured merchant-supplied data rather than a reading of anything.

What this deliberately does not do is the whole reason it can be trusted.

It runs no model, so nothing here can decide that a charger is compatible with a laptop or that
"charcoal" means black. It reads no Commerce IR and no compiler candidate, so a benchmark built
on top of it cannot be measuring the compiler's own output. It reads no benchmark result and no
diagnostic, so a generated workload cannot be shaped around what a buyer failed at last time.
And it invents no attribute, so a typed fact in the evaluation catalog is a typed fact the
merchant supplied.

The trust boundary is the other half, and it is not a technicality.

```text
SOURCE-DERIVED EVALUATION STATE      what this produces. A frozen simulation of the merchant,
                                     used to run a benchmark against
AUTHORITATIVE MERCHANT STATE         the merchant's real prices, stock, checkout and payments.
                                     Nothing here reads or writes any of it
```

A price and a stock level appear in the evaluation catalog because a benchmark that could not
quote or hold anything would measure nothing. They are the merchant's own frozen words about
themselves, not AgentRank's account of what the merchant currently sells, and no snapshot and no
workspace has ever been able to change what the commerce runtime decides. This module writes no
row at all: it produces a `BenchmarkFixture`, which is a description of a world, and the existing
benchmark preparation is the only thing that ever puts one into a catalog.

Stock is the one field where the source and the world cannot be the same kind of fact, and this
is where the two are separated rather than confused.

```text
SOURCE            IN_STOCK with a count, IN_STOCK with none, OUT_OF_STOCK, or UNKNOWN
EVALUATION WORLD  an exact number of units, always, because a shelf has to hold something
```

A count is carried across unchanged. A merchant who published only that something is in stock
gets `assumed_stock_units` from the bootstrap configuration, which is a simulation parameter
frozen into the workspace identity and reported as one. And `UNKNOWN` is refused by name, because
a shelf cannot hold an unknown quantity of anything: a merchant who published no availability is
asked to state one rather than having one chosen for them in either direction.

The assumed depth is authoritative inside the world and nowhere outside it. That is worth being
exact about, because "it is never treated as real stock" would be too strong: a benchmark world is
a commerce runtime, and a runtime whose shelf depth were advisory could not reserve, decrement or
refuse anything. Within the world it is the stock, the storefront the buyer queries publishes it,
and the oracle is computed against it. What it never becomes is a fact about the merchant: the
source document keeps saying `IN_STOCK` with no count, `raw_projection` hands the buyer exactly
that, and the workspace records which SKUs took a supplied depth so the console can render it as
an assumption rather than as their inventory.

Money stays an integer count of minor units with its currency beside it, exactly as it arrived.
Nothing here converts, rounds, sums across currencies or infers an exponent.
"""

import uuid
from dataclasses import dataclass
from typing import Any

from agentrank_api.benchmark.catalog import CatalogEntry
from agentrank_api.benchmark.fixtures import BenchmarkFixture
from agentrank_api.commerce.catalog_fixture import SeedProduct, SeedVariant
from agentrank_api.representation.definitions import (
    MerchantSourceDefinition,
    SourceAvailability,
    SourceProduct,
    SourceVariant,
    instruction_like,
)
from agentrank_api.representation.schemas import MAX_PRODUCTS, MAX_TOTAL_VARIANTS
from agentrank_api.workspace.definitions import (
    DEFAULT_ASSUMED_STOCK_UNITS,
    BootstrapBlocker,
    BootstrapRefusedError,
    workspace_key,
)

# The suffix a generated evaluation catalog's fixture key carries, so an operator reading a
# registered world can tell one this package built from one somebody authored.
CATALOG_KEY_SUFFIX = "-workspace-catalog"

# A stable namespace for the identifiers the oracle predicate needs. These never reach a
# database: `CatalogEntry` carries a variant identifier because a run compares one against what
# an executor selected, and generation only ever asks whether an entry satisfies a mission.
_ENTRY_NAMESPACE = uuid.UUID("6f4f0b0e-7d1e-4a4b-9a1f-2b0f3f6a8c11")

# What a variant attribute value may be. Exactly the scalars that survive a round trip through
# JSONB unchanged, which is the same rule a benchmark fixture already enforces: PostgreSQL
# stores a JSON number as `numeric`, so a fractional value can come back as a different value
# and move the identity of a world nobody edited.
_SCALARS = (str, int, bool)

# What a catalog row can actually hold. A source document submitted through the console is
# already bounded well inside these, and one published by the operator command line is bounded by
# nothing at all, so they are checked here rather than discovered as a database error in the
# middle of the first benchmark run that tries to prepare the world.
# The namespace an import writes its own provenance under, which is never a merchant product
# fact. Stated here as well as in the importer because this is the boundary that has to refuse
# it, and a projection that trusted the importer to have named things carefully would be a
# projection with no rule at all.
IMPORT_PROVENANCE_PREFIX = "import_"

_LIMITS = {
    "external_id": 128,
    "title": 300,
    "category": 120,
    "sku": 128,
    "label": 200,
}


@dataclass(frozen=True, slots=True)
class SimulatedStock:
    """One evaluation world line whose depth is a simulation rather than merchant evidence.

    Recorded per variant rather than as a count, so a merchant reading their setup can see
    exactly which of their own lines AgentRank had to assume a depth for and which ones it read.
    """

    sku: str
    units: int

    def to_payload(self) -> dict[str, Any]:
        return {"sku": self.sku, "units": self.units}


@dataclass(frozen=True, slots=True)
class CatalogSummary:
    """What the projected evaluation catalog holds, as counts rather than as content.

    A merchant reads this before deciding to build a workspace, and an operator reads it back
    afterwards. It is deliberately not the catalog: a console overview that rendered every
    product would be loading a whole source document to draw a table of numbers.
    """

    products: int
    variants: int
    purchasable_variants: int
    # Null for a workspace built before a source document could omit a stock quantity. Zero would
    # say "nothing was simulated", which is probably true of those worlds and is not something
    # this reader can prove from the row it was given.
    simulated_stock_variants: int | None
    assumed_stock_units: int | None
    currencies: tuple[str, ...]
    categories: tuple[str, ...]

    def to_payload(self) -> dict[str, Any]:
        return {
            "products": self.products,
            "variants": self.variants,
            "purchasable_variants": self.purchasable_variants,
            "simulated_stock_variants": self.simulated_stock_variants,
            "assumed_stock_units": self.assumed_stock_units,
            "currencies": list(self.currencies),
            "categories": list(self.categories),
        }


@dataclass(frozen=True, slots=True)
class EvaluationCatalog:
    """The world one workspace evaluates, and the facts a mission is decided against.

    `fixture` is the authored form: it is what registers the merchant as a benchmark world and
    what a run puts the catalog back to before every mission. `entries` is the same content in
    the vocabulary the benchmark's own ground-truth predicate reads, so a generated mission's
    expected outcome is computed by the code that recomputes it during a run rather than by a
    second implementation that could disagree.

    `omitted_fields` names the source fields this projection did not carry, as source field
    addresses. It exists so that dropping something is visible rather than silent: a merchant
    whose metadata holds a nested object should be able to see that the evaluation catalog does
    not carry it, and no benchmark should be built around a fact nobody was told was missing.
    """

    fixture: BenchmarkFixture
    entries: tuple[CatalogEntry, ...]
    omitted_fields: tuple[str, ...]
    simulated_stock: tuple[SimulatedStock, ...] = ()
    assumed_stock_units: int = DEFAULT_ASSUMED_STOCK_UNITS

    @property
    def summary(self) -> CatalogSummary:
        categories = {
            product.category for product in self.fixture.products if product.category is not None
        }
        return CatalogSummary(
            products=len(self.fixture.products),
            variants=len(self.entries),
            purchasable_variants=sum(1 for entry in self.entries if entry.can_supply(1)),
            simulated_stock_variants=len(self.simulated_stock),
            assumed_stock_units=self.assumed_stock_units,
            currencies=tuple(sorted({entry.currency for entry in self.entries})),
            categories=tuple(sorted(categories)),
        )


def project_catalog(
    source: MerchantSourceDefinition,
    *,
    merchant_slug: str,
    merchant_name: str,
    version: int,
    assumed_stock_units: int = DEFAULT_ASSUMED_STOCK_UNITS,
) -> EvaluationCatalog:
    """Read one frozen source document as the isolated world a benchmark will be run against.

    The merchant slug and display name come from the merchant row rather than from the document.
    A source snapshot carries a slug of its own, and a browser has never been able to set it,
    but the world this registers is a named merchant's catalog and which merchant that is must
    come from the identity the caller authenticated rather than from a field inside the evidence.

    Refuses rather than guesses. A source that carries no purchasable variant, or that addresses
    whatever reads it next, produces a named blocker; there is no path here that fills a gap in
    with a default.
    """
    if source.merchant_slug != merchant_slug:
        raise BootstrapRefusedError(
            BootstrapBlocker(
                "source_names_another_merchant",
                "This source snapshot was recorded against a different merchant, so it cannot"
                " become this merchant's evaluation catalog.",
            )
        )

    _require_bounded(source)
    omitted: list[str] = []
    simulated: list[SimulatedStock] = []
    products = tuple(
        _product(product, omitted, simulated, assumed_stock_units) for product in source.products
    )
    fixture = BenchmarkFixture(
        key=workspace_key(merchant_slug, CATALOG_KEY_SUFFIX),
        version=version,
        merchant_slug=merchant_slug,
        merchant_name=merchant_name,
        products=products,
    )
    entries = catalog_entries(fixture)
    if not any(entry.can_supply(1) for entry in entries):
        raise BootstrapRefusedError(
            BootstrapBlocker(
                "no_purchasable_variant",
                "Every variant in your merchant information is out of stock, so there is nothing"
                " a buyer could be asked to buy. Add stock for at least one variant.",
            )
        )
    return EvaluationCatalog(
        fixture=fixture,
        entries=entries,
        omitted_fields=tuple(omitted),
        simulated_stock=tuple(simulated),
        assumed_stock_units=assumed_stock_units,
    )


def catalog_entries(fixture: BenchmarkFixture) -> tuple[CatalogEntry, ...]:
    """The fixture as the facts a mission's ground truth is computed from.

    The identifiers are derived from the SKU and never stored. A `CatalogEntry` carries one
    because a run uses the same type to ask whether an executor's selection is something the
    merchant sells, and generation only ever asks the other question this type answers, which is
    whether an entry satisfies a mission.
    """
    return tuple(
        CatalogEntry(
            variant_id=uuid.uuid5(_ENTRY_NAMESPACE, variant.sku),
            sku=variant.sku,
            product_category=product.category,
            attributes=dict(variant.attributes),
            price_amount_minor=variant.price_amount_minor,
            currency=variant.currency,
            inventory_quantity=variant.inventory_quantity,
            is_active=variant.is_active and product.is_active,
        )
        for product in fixture.products
        for variant in product.variants
    )


def _require_bounded(source: MerchantSourceDefinition) -> None:
    """Refuse a source too large to become an evaluation world, by the intake's own bounds.

    The same numbers a console submission is already held to, imported rather than restated so
    the two cannot disagree. They are checked again here because the operator command line
    publishes a snapshot without going through the submission schema, and an unbounded document
    would become an unbounded stored world payload, an unbounded catalog to prepare before every
    mission, and a benchmark nobody chose the size of.
    """
    variants = sum(len(product.variants) for product in source.products)
    if len(source.products) <= MAX_PRODUCTS and variants <= MAX_TOTAL_VARIANTS:
        return
    raise BootstrapRefusedError(
        BootstrapBlocker(
            "source_too_large",
            f"Your merchant information describes {len(source.products)} products and"
            f" {variants} variants. An evaluation catalog holds at most {MAX_PRODUCTS} products"
            f" and {MAX_TOTAL_VARIANTS} variants.",
        )
    )


def _product(
    product: SourceProduct,
    omitted: list[str],
    simulated: list[SimulatedStock],
    assumed_stock_units: int,
) -> SeedProduct:
    """One source product as an evaluation catalog product, field for field."""
    address = f"products[{product.external_id}]"
    _bounded(product.external_id, "external_id", f"{address}.external_id")
    _bounded(product.title, "title", f"{address}.title")
    _bounded(product.category, "category", f"{address}.category")
    _plain(product.title, f"{address}.title")
    _plain(product.description, f"{address}.description")
    _plain(product.category, f"{address}.category")
    # A source product carries structured metadata of its own and the catalog has nowhere to put
    # it: attributes belong to a purchasable variant, which is what a mission constraint is
    # checked against. Attaching a product's metadata to each of its variants would be moving a
    # fact to an address the merchant did not state it at, so it is reported instead.
    omitted.extend(
        f"{address}.merchant_metadata.{key}" for key in sorted(product.merchant_metadata)
    )
    return SeedProduct(
        external_id=product.external_id,
        title=product.title,
        description=product.description,
        category=product.category,
        variants=tuple(
            _variant(product, variant, omitted, simulated, assumed_stock_units)
            for variant in product.variants
        ),
        is_active=True,
    )


def _variant(
    product: SourceProduct,
    variant: SourceVariant,
    omitted: list[str],
    simulated: list[SimulatedStock],
    assumed_stock_units: int,
) -> SeedVariant:
    """One source variant as an evaluation catalog variant, with its metadata as attributes.

    `is_active` is true for every projected variant, and that is a statement rather than a
    default. A source document says what a merchant sells; it has no field for withdrawn, so
    inventing one would be reading a fact the merchant never wrote. A variant nobody can buy is
    expressed the way the merchant expressed it, which is a stock level of zero.
    """
    address = f"products[{product.external_id}].variants[{variant.sku}]"
    quantity = _stock(variant, address, simulated, assumed_stock_units)
    _bounded(variant.sku, "sku", f"{address}.sku")
    _bounded(variant.label, "label", f"{address}.label")
    _plain(variant.label, f"{address}.label")
    attributes: dict[str, Any] = {}
    for key in sorted(variant.merchant_metadata):
        value = variant.merchant_metadata[key]
        if key.startswith(IMPORT_PROVENANCE_PREFIX):
            # AgentRank's own record of how it read the merchant's page, not a fact about the
            # product. It reaches the source document because provenance belongs with the
            # evidence it explains, and it must not reach the evaluation catalog: a typed
            # attribute there is something a generated mission can require, and a mission built
            # on `import_availability_text` would be measuring whether a buyer can find
            # AgentRank's own reading of a page rather than anything the merchant sells.
            omitted.append(f"{address}.merchant_metadata.{key}")
            continue
        if not isinstance(value, _SCALARS):
            omitted.append(f"{address}.merchant_metadata.{key}")
            continue
        _plain(key, f"{address}.merchant_metadata key {key!r}")
        if isinstance(value, str):
            _plain(value, f"{address}.merchant_metadata.{key}")
        attributes[key] = value
    return SeedVariant(
        sku=variant.sku,
        label=variant.label,
        price_amount_minor=variant.price_amount_minor,
        currency=variant.currency,
        inventory_quantity=quantity,
        attributes=attributes,
        is_active=True,
    )


def _stock(
    variant: SourceVariant,
    address: str,
    simulated: list[SimulatedStock],
    assumed_stock_units: int,
) -> int:
    """How deep this line's shelf is in the evaluation world, and where that number came from.

    Three cases and no fourth. A merchant who published a count has stated the depth and it is
    carried across. A merchant who published only that something is in stock has stated that the
    shelf is not empty and nothing about how full it is, so the configured simulation depth is
    used and the line is recorded as simulated. A merchant who published nothing at all has
    stated neither, and that is refused rather than resolved: turning it into zero would assert
    they cannot supply something they never said they could not, and turning it into a positive
    number would assert the opposite.
    """
    if variant.inventory_quantity is not None:
        return variant.inventory_quantity
    if variant.availability is SourceAvailability.UNKNOWN:
        raise BootstrapRefusedError(
            BootstrapBlocker(
                "source_availability_unknown",
                f"Your merchant information does not say whether {address} can be bought. An"
                " evaluation world holds an exact number of units, and AgentRank will not decide"
                " that one for you. State whether it is in stock, or give it a stock quantity,"
                " and submit your source again.",
            )
        )
    simulated.append(SimulatedStock(sku=variant.sku, units=assumed_stock_units))
    return assumed_stock_units


def _bounded(value: str | None, field: str, address: str) -> None:
    """Refuse a source value the evaluation catalog has no column wide enough for.

    A named blocker rather than a truncation. Shortening a merchant's own product title to make
    it fit would be changing a source fact for benchmark convenience, and the merchant would be
    measured against a catalog that says something they did not.
    """
    limit = _LIMITS[field]
    if value is not None and len(value) > limit:
        raise BootstrapRefusedError(
            BootstrapBlocker(
                "source_field_too_long",
                f"The value at {address} is longer than the {limit} characters an evaluation"
                " catalog can hold. Shorten it and submit your source again.",
            )
        )


def _plain(value: str | None, address: str) -> None:
    """Refuse merchant text that addresses whatever reads it next, at one field address.

    Defense in depth and stated as exactly that. The published guard catches one shape this
    repository has decided is never legitimate in merchant evidence, and it is not a general
    prompt-injection detector; the boundary that actually holds is structural, and it is that a
    generated mission's own prose is written in this repository and carries no merchant string
    at all. This refusal exists because a generated benchmark is trusted truth rather than a
    discovery projection, and truth is the wrong place to be lenient.
    """
    if value is not None and instruction_like(value):
        raise BootstrapRefusedError(
            BootstrapBlocker(
                "source_addresses_the_reader",
                f"The text at {address} addresses whatever reads your merchant information"
                " rather than describing a product, so AgentRank will not build a benchmark"
                " from it. Edit that field and submit your source again.",
            )
        )
