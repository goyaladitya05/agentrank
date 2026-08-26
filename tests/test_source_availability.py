"""What a merchant may say about stock, and what each of those statements becomes downstream.

The representation problem Phase 5E found and this phase closes. A source variant used to require
an exact integer quantity, and no public storefront publishes one, so an ordinary merchant could
only be imported by having somebody state a number nobody had published. A source variant now
holds the availability state a storefront actually publishes and an optional exact count.

Three separate things are kept apart here, and most of this file exists to hold them apart.

```text
SOURCE EVIDENCE     what the merchant published: a state, and a count where there was one
EVALUATION WORLD    the simulated shelf a benchmark runs against: always an exact number
COMMERCE RUNTIME    the authoritative inventory a checkout reserves: unchanged by any of this
```

The load-bearing property in the middle is that no source claim ever becomes an evaluation
quantity by accident. A count is carried across, a countless "in stock" gets a depth the
bootstrap configuration states and the workspace records as simulated, and `UNKNOWN` is refused
by name because a shelf cannot hold an unknown number of anything.
"""

import json
from pathlib import Path

import pytest
from workspace_support import product, source, variant

from agentrank_api.compiler.extraction import extract
from agentrank_api.compiler.targets import variant_availability_target
from agentrank_api.representation.definitions import (
    MerchantSourceDefinition,
    SourceAvailability,
    SourceProduct,
    SourceVariant,
    availability_of,
    read_availability,
)
from agentrank_api.representation.fields import source_fields
from agentrank_api.representation.fixtures import RepresentationFixtureError, parse_source
from agentrank_api.representation.schemas import SourceDocumentInput
from agentrank_api.workspace.definitions import (
    DEFAULT_ASSUMED_STOCK_UNITS,
    BootstrapConfiguration,
    BootstrapRefusedError,
)
from agentrank_api.workspace.projection import project_catalog


def test_a_stated_quantity_states_the_availability_with_it() -> None:
    assert availability_of(0) is SourceAvailability.OUT_OF_STOCK
    assert availability_of(1) is SourceAvailability.IN_STOCK
    assert variant("A", stock=7).availability is SourceAvailability.IN_STOCK
    assert variant("A", stock=0).availability is SourceAvailability.OUT_OF_STOCK


def test_a_variant_whose_two_stock_fields_disagree_is_refused_rather_than_resolved() -> None:
    with pytest.raises(ValueError, match="contradicts"):
        SourceVariant(
            sku="A",
            label=None,
            price_amount_minor=100,
            currency="INR",
            availability=SourceAvailability.OUT_OF_STOCK,
            inventory_quantity=5,
            merchant_metadata={},
        )


def test_out_of_stock_is_an_exact_quantity_and_may_not_be_stated_without_one() -> None:
    """Zero is what out of stock means, so there is one way to write it rather than two."""
    with pytest.raises(ValueError, match="quantity of zero"):
        SourceVariant(
            sku="A",
            label=None,
            price_amount_minor=100,
            currency="INR",
            availability=SourceAvailability.OUT_OF_STOCK,
            inventory_quantity=None,
            merchant_metadata={},
        )


def test_the_canonical_payload_records_each_stock_fact_exactly_once() -> None:
    counted = variant("A", stock=7).payload()
    assert counted["inventory_quantity"] == 7
    assert "availability" not in counted
    stated = variant("A", stock=None, availability=SourceAvailability.IN_STOCK).payload()
    assert stated["inventory_quantity"] is None
    assert stated["availability"] == "IN_STOCK"


def test_a_document_written_before_availability_existed_round_trips_unchanged() -> None:
    """The whole of historical compatibility, as bytes rather than as an argument.

    Every stored source snapshot carries a content hash, the compiler recomputes it from the
    payload before it will read the document, and a Commerce IR names the source by that hash. If
    re-serializing an old document produced one extra key, every snapshot in the database would
    become uncompilable and every representation's lineage would stop resolving.
    """
    stored = {
        "key": "merchant-source",
        "version": 1,
        "merchant_slug": "old-merchant",
        "products": [
            {
                "external_id": "CHG",
                "title": "Charger",
                "description": None,
                "category": "chargers",
                "variants": [
                    {
                        "sku": "CHG-1",
                        "label": "Black",
                        "price_amount_minor": 499900,
                        "currency": "INR",
                        "inventory_quantity": 4,
                        "merchant_metadata": {"finish": "black"},
                    }
                ],
                "merchant_metadata": {},
            }
        ],
        "policy_text": {},
    }
    before = MerchantSourceDefinition(
        key="merchant-source",
        version=1,
        merchant_slug="old-merchant",
        products=(
            SourceProduct(
                external_id="CHG",
                title="Charger",
                description=None,
                category="chargers",
                variants=(
                    variant(
                        "CHG-1", label="Black", price=499900, stock=4, metadata={"finish": "black"}
                    ),
                ),
                merchant_metadata={},
            ),
        ),
        policy_text={},
    )
    parsed = parse_source(stored)
    assert parsed.payload() == stored
    assert parsed.content_hash == before.content_hash
    assert parsed.products[0].variants[0].availability is SourceAvailability.IN_STOCK


def test_a_stored_variant_with_neither_stock_field_is_refused() -> None:
    with pytest.raises(RepresentationFixtureError):
        parse_source(
            {
                "key": "k",
                "version": 1,
                "merchant_slug": "m",
                "products": [
                    {
                        "external_id": "P",
                        "title": "T",
                        "description": None,
                        "category": None,
                        "variants": [
                            {
                                "sku": "S",
                                "label": None,
                                "price_amount_minor": 1,
                                "currency": "INR",
                                "merchant_metadata": {},
                            }
                        ],
                        "merchant_metadata": {},
                    }
                ],
                "policy_text": {},
            }
        )


def test_a_submitted_document_may_state_either_precision_and_not_neither() -> None:
    body = {
        "products": [
            {
                "external_id": "P",
                "title": "T",
                "variants": [
                    {
                        "sku": "S1",
                        "price_amount_minor": 100,
                        "currency": "INR",
                        "availability": "IN_STOCK",
                    },
                    {
                        "sku": "S2",
                        "price_amount_minor": 100,
                        "currency": "INR",
                        "inventory_quantity": 3,
                    },
                ],
            }
        ]
    }
    document = SourceDocumentInput.model_validate(body)
    stated, counted = document.products[0].variants
    assert stated.domain().inventory_quantity is None
    assert stated.domain().availability is SourceAvailability.IN_STOCK
    assert counted.domain().inventory_quantity == 3
    with pytest.raises(ValueError, match="neither"):
        SourceDocumentInput.model_validate(
            {
                "products": [
                    {
                        "external_id": "P",
                        "title": "T",
                        "variants": [{"sku": "S", "price_amount_minor": 100, "currency": "INR"}],
                    }
                ]
            }
        )


def test_a_countless_variant_is_addressed_at_the_field_the_document_holds() -> None:
    """A compiler candidate cites a source field, and it has to be one the document has."""
    counted = source(product("P", variant("S", stock=4)))
    stated = source(
        product("P", variant("S", stock=None, availability=SourceAvailability.IN_STOCK))
    )
    assert "products[P].variants[S].inventory_quantity" in source_fields(counted)
    assert "products[P].variants[S].availability" not in source_fields(counted)
    assert source_fields(stated)["products[P].variants[S].availability"] == "IN_STOCK"


@pytest.mark.parametrize(
    ("availability", "stock", "expected", "field"),
    [
        (None, 4, "TRUE", "inventory_quantity"),
        (None, 0, "FALSE", "inventory_quantity"),
        (SourceAvailability.IN_STOCK, None, "TRUE", "availability"),
        (SourceAvailability.UNKNOWN, None, "UNKNOWN", "availability"),
    ],
)
def test_the_compiler_copies_availability_and_never_decides_it(
    availability: SourceAvailability | None, stock: int | None, expected: str, field: str
) -> None:
    """An unknown availability compiles to UNKNOWN, which is the point of having the state."""
    compiled = source(product("P", variant("S", stock=stock, availability=availability)))
    target = variant_availability_target("S")
    proposals = [proposal for proposal, _ in extract(compiled) if proposal.target == target]
    assert len(proposals) == 1
    assert proposals[0].fact.value == expected
    assert proposals[0].fact.provenance[0].field == f"products[P].variants[S].{field}"


def test_a_counted_line_keeps_its_own_depth_in_the_evaluation_world() -> None:
    catalog = project_catalog(
        source(product("P", variant("S", stock=7))),
        merchant_slug="test-merchant",
        merchant_name="Test",
        version=1,
    )
    assert catalog.entries[0].inventory_quantity == 7
    assert catalog.simulated_stock == ()
    assert catalog.summary.simulated_stock_variants == 0


def test_a_countless_in_stock_line_takes_the_configured_depth_and_says_it_did() -> None:
    """The simulation assumption, made explicit rather than hidden inside a projection."""
    catalog = project_catalog(
        source(product("P", variant("S", stock=None, availability=SourceAvailability.IN_STOCK))),
        merchant_slug="test-merchant",
        merchant_name="Test",
        version=1,
        assumed_stock_units=5,
    )
    assert catalog.entries[0].inventory_quantity == 5
    assert [entry.sku for entry in catalog.simulated_stock] == ["S"]
    assert catalog.summary.simulated_stock_variants == 1
    assert catalog.summary.assumed_stock_units == 5


def test_an_unknown_availability_is_refused_by_name_rather_than_read_as_either_answer() -> None:
    with pytest.raises(BootstrapRefusedError) as refused:
        project_catalog(
            source(
                product(
                    "P",
                    variant("S", stock=None, availability=SourceAvailability.UNKNOWN),
                )
            ),
            merchant_slug="test-merchant",
            merchant_name="Test",
            version=1,
        )
    assert refused.value.blocker.code == "source_availability_unknown"
    assert "products[P].variants[S]" in refused.value.blocker.message


def test_the_assumed_depth_is_part_of_the_workspace_identity() -> None:
    """A different simulation policy is a different workspace and never a rewritten one."""
    default = BootstrapConfiguration()
    other = BootstrapConfiguration(assumed_stock_units=DEFAULT_ASSUMED_STOCK_UNITS + 1)
    assert default.digest != other.digest
    assert default.to_payload()["assumed_stock_units"] == DEFAULT_ASSUMED_STOCK_UNITS


def test_read_availability_refuses_a_state_this_repository_does_not_define() -> None:
    with pytest.raises(ValueError, match="does not define"):
        read_availability(None, "MAYBE", where="variant 'S'")


def test_agentranks_own_import_provenance_never_becomes_a_benchmark_constraint() -> None:
    """A generated mission requires a merchant's product fact, not AgentRank's reading of a page.

    An import writes `import_availability` and `import_availability_text` into a variant's
    merchant metadata, which is where provenance belongs: it explains the evidence beside it. The
    projection copies scalar metadata into the evaluation catalog as typed attributes, and a typed
    attribute there is something the specification families can build a mission around. A mission
    requiring `import_availability_text` equals "In stock" would be measuring whether a buyer can
    find AgentRank's own note about how it read a page, and it would take a slot in a bounded
    mission budget from a fact the merchant actually publishes.
    """
    catalog = project_catalog(
        source(
            product(
                "P",
                variant(
                    "S",
                    stock=4,
                    metadata={
                        "finish": "black",
                        "import_availability": "IN_STOCK",
                        "import_availability_text": "In stock",
                    },
                ),
            )
        ),
        merchant_slug="test-merchant",
        merchant_name="Test",
        version=1,
    )

    assert catalog.entries[0].attributes == {"finish": "black"}
    # Dropped visibly rather than silently, by the same mechanism that reports a nested value.
    assert "products[P].variants[S].merchant_metadata.import_availability" in catalog.omitted_fields


def test_the_authored_voltedge_source_round_trips_byte_for_byte() -> None:
    """The one source document this repository actually ships, asserted as bytes.

    The synthetic case above proves the rule; this proves it holds for the document every
    benchmark, every compiler test and the deployment smoke are built on. If it did not, the
    stored snapshot's content hash would stop matching its payload and the compiler would refuse
    to read it.
    """
    raw = json.loads(Path("benchmarks/voltedge/source.json").read_text(encoding="utf-8"))

    assert parse_source(raw).payload() == raw
