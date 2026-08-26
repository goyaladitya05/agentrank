"""Deterministic extraction, and every place it refuses rather than guesses.

The tests that matter most here are the negative ones. An importer that extracts a plausible
number from an ambiguous page is worse than one that extracts nothing, because the number goes
into a merchant's source history looking exactly like a fact they stated, and an evaluation is
then run against it. So most of this file is about what does not come out.

No network. `reading` and `extraction` see a string, which is what makes them testable at this
granularity; the retrieval that produces the string is `test_import_network`.
"""

from decimal import Decimal

import pytest
from importer_support import (
    CONFLICTING_PRICE_PRODUCT,
    CROSS_ORIGIN_PRODUCT,
    INSTRUCTION_POLICY,
    INSTRUCTION_PRODUCT,
    JSON_LD_PRODUCT,
    MALFORMED_PRODUCT,
    METADATA_PRODUCT,
    NO_CURRENCY_PRODUCT,
    OUT_OF_STOCK_PRODUCT,
    RETURNS_POLICY,
    VARIANT_PRODUCT,
)

from agentrank_api.importer.amounts import (
    RefusedAmountError,
    exponent,
    minor_units,
    normalize_currency,
)
from agentrank_api.importer.draft import (
    AvailabilityEvidence,
    DraftProduct,
    DraftVariant,
    ExtractionMethod,
    SourceDraft,
    canonical_document,
)
from agentrank_api.importer.extraction import (
    Identifiers,
    extract_policy,
    extract_product,
    structured_nodes,
)
from agentrank_api.importer.reading import read_page
from agentrank_api.representation.schemas import SourceDocumentInput

URL = "https://shop.example/p/item"


def product(markup: str, url: str = URL) -> tuple[DraftProduct | None, str | None]:
    """One page extracted, as the product it produced and the reason it did not."""
    outcome = extract_product(read_page(markup), source_url=url, identifiers=Identifiers())
    return outcome.product, None if outcome.omission is None else outcome.omission.reason


def structured(body: str) -> str:
    return (
        '<html><head><title>T</title><script type="application/ld+json">'
        f"{body}</script></head><body><h1>H</h1></body></html>"
    )


def test_a_page_publishing_schema_org_data_produces_a_grounded_product() -> None:
    found, refused = product(JSON_LD_PRODUCT)
    assert refused is None
    assert found is not None
    assert found.title == "VoltEdge 65W GaN Charger"
    assert found.category == "Chargers"
    assert found.extraction is ExtractionMethod.STRUCTURED_DATA
    assert found.source_url == URL
    assert [variant.price_amount_minor for variant in found.variants] == [349900]
    assert [variant.currency for variant in found.variants] == ["INR"]
    assert found.variants[0].availability is AvailabilityEvidence.IN_STOCK


def test_a_page_publishing_only_metadata_tags_produces_a_product_named_as_such() -> None:
    """The second path, and the record says which one produced the fact."""
    found, refused = product(METADATA_PRODUCT)
    assert refused is None
    assert found is not None
    assert found.extraction is ExtractionMethod.PAGE_METADATA
    assert found.title == "VoltEdge 2m USB-C Cable"
    assert found.variants[0].price_amount_minor == 89900


def test_variants_come_from_what_the_merchant_published_and_not_from_ordering() -> None:
    found, refused = product(VARIANT_PRODUCT)
    assert refused is None
    assert found is not None
    assert [variant.sku for variant in found.variants] == ["VE-SLV-BLK", "VE-SLV-SND"]
    assert [variant.label for variant in found.variants] == ["Black", "Sand"]


def test_an_out_of_stock_page_is_the_one_availability_that_needs_no_merchant_number() -> None:
    found, _ = product(OUT_OF_STOCK_PRODUCT)
    assert found is not None
    assert found.variants[0].availability is AvailabilityEvidence.OUT_OF_STOCK
    draft = SourceDraft(products=(found,))
    assert draft.stock_level_required is False
    document = canonical_document(draft, stock_level=None)
    variant = document["products"][0]["variants"][0]
    assert variant["inventory_quantity"] == 0
    assert variant["merchant_metadata"]["import_stock_level_source"] == "PAGE_OUT_OF_STOCK"


def test_an_in_stock_page_never_becomes_a_quantity_on_its_own() -> None:
    """The representation gap, asserted rather than described.

    A public page saying "In stock" publishes no number, and `canonical_document` refuses to
    invent one. The merchant states it, and the source document records that they did.
    """
    found, _ = product(JSON_LD_PRODUCT)
    assert found is not None
    draft = SourceDraft(products=(found,))
    assert draft.stock_level_required is True
    with pytest.raises(ValueError, match="stock level"):
        canonical_document(draft, stock_level=None)
    document = canonical_document(draft, stock_level=12)
    variant = document["products"][0]["variants"][0]
    assert variant["inventory_quantity"] == 12
    assert variant["merchant_metadata"]["import_stock_level_source"] == "MERCHANT_SUPPLIED"
    assert variant["merchant_metadata"]["import_availability"] == "IN_STOCK"


def test_an_unknown_availability_is_an_answer_and_still_needs_a_stated_number() -> None:
    found, _ = product(
        structured(
            '{"@type":"Product","name":"X","sku":"S",'
            '"offers":{"@type":"Offer","price":"10","priceCurrency":"INR"}}'
        )
    )
    assert found is not None
    assert found.variants[0].availability is AvailabilityEvidence.UNKNOWN
    assert SourceDraft(products=(found,)).stock_level_required is True


@pytest.mark.parametrize(
    ("markup", "reason"),
    [
        (NO_CURRENCY_PRODUCT, "currency_missing"),
        (CONFLICTING_PRICE_PRODUCT, "price_conflict"),
        (INSTRUCTION_PRODUCT, "instruction_like"),
        (
            structured(
                '{"@type":"Product","name":"X","offers":{"@type":"AggregateOffer",'
                '"lowPrice":"10","highPrice":"20","priceCurrency":"INR"}}'
            ),
            "price_conflict",
        ),
        (
            structured(
                '{"@type":"Product","name":"X","offers":{"@type":"Offer",'
                '"price":"1,299.00","priceCurrency":"INR"}}'
            ),
            "price_malformed",
        ),
        (
            structured(
                '{"@type":"Product","name":"X","offers":{"@type":"Offer",'
                '"price":"10.005","priceCurrency":"INR"}}'
            ),
            "price_precision",
        ),
        (
            structured(
                '{"@type":"Product","name":"X","offers":{"@type":"Offer",'
                '"price":"10","priceCurrency":"ZZZ"}}'
            ),
            "currency_unsupported",
        ),
        (
            structured(
                '[{"@type":"Product","name":"A","offers":{"price":"1","priceCurrency":"INR"}},'
                '{"@type":"Product","name":"B","offers":{"price":"2","priceCurrency":"INR"}}]'
            ),
            "several_products",
        ),
        (
            structured(
                '{"@type":"Product","name":"X","offers":['
                '{"@type":"Offer","price":"1","priceCurrency":"INR"},'
                '{"@type":"Offer","price":"2","priceCurrency":"INR"}]}'
            ),
            "variant_ambiguous",
        ),
        (
            structured(
                '{"@type":"Product","name":"X","offers":['
                '{"@type":"Offer","sku":"S","price":"1","priceCurrency":"INR"},'
                '{"@type":"Offer","sku":"S","price":"2","priceCurrency":"INR"}]}'
            ),
            "price_conflict",
        ),
        (
            "<html><head><title>Plain</title></head><body><h1>Plain</h1>"
            "<p>Only Rs 2,499 today</p></body></html>",
            "price_missing",
        ),
    ],
)
def test_a_page_that_cannot_be_read_without_guessing_is_omitted_by_name(
    markup: str, reason: str
) -> None:
    """Each of these is a page a plausible importer would have produced a number from."""
    found, refused = product(markup)
    assert found is None
    assert refused == reason


def test_a_refused_structured_price_does_not_fall_back_to_a_different_number() -> None:
    """Refusing an ambiguous price and then using another one is guessing with an extra step."""
    markup = (
        "<html><head><title>T</title>"
        '<meta property="product:price:amount" content="500.00">'
        '<meta property="product:price:currency" content="INR">'
        '<script type="application/ld+json">'
        '{"@type":"Product","name":"X","offers":{"@type":"Offer","price":"1,000.00",'
        '"priceCurrency":"INR"}}</script></head><body></body></html>'
    )
    found, refused = product(markup)
    assert found is None
    assert refused == "price_malformed"


def test_a_product_with_no_offer_at_all_falls_through_to_the_page_metadata() -> None:
    """Absence is not refusal, so a product node with nothing to say lets the second path run."""
    markup = (
        "<html><head><title>T</title>"
        '<meta property="product:price:amount" content="500.00">'
        '<meta property="product:price:currency" content="INR">'
        '<meta property="product:retailer_item_id" content="FB-1">'
        '<script type="application/ld+json">'
        '{"@type":"Product","name":"X"}</script></head><body></body></html>'
    )
    found, refused = product(markup)
    assert refused is None
    assert found is not None
    assert found.extraction is ExtractionMethod.PAGE_METADATA
    assert found.variants[0].price_amount_minor == 50000


def test_malformed_markup_and_malformed_structured_data_are_read_rather_than_refused() -> None:
    """A storefront with unclosed tags is an ordinary storefront."""
    outcome = extract_product(
        read_page(MALFORMED_PRODUCT), source_url=URL, identifiers=Identifiers()
    )
    assert outcome.product is not None
    assert outcome.product.variants[0].price_amount_minor == 45000
    assert any(finding.code == "structured_data_malformed" for finding in outcome.findings)


def test_a_url_named_inside_a_document_is_never_treated_as_something_to_fetch() -> None:
    """Extraction reads. The list of URLs an import fetches is the one the merchant supplied.

    The fixture names an internal address in its structured data, its image and its links. None of
    them appears anywhere in what comes out.
    """
    found, refused = product(CROSS_ORIGIN_PRODUCT)
    assert refused is None
    assert found is not None
    rendered = str(found.payload())
    assert "169.254.169.254" not in rendered
    assert "127.0.0.1" not in rendered
    assert "10.0.0.1" not in rendered
    assert "elsewhere.example" not in rendered


def test_a_script_body_never_becomes_page_text() -> None:
    reading = read_page(RETURNS_POLICY)
    assert "should never be text" not in reading.text
    assert "color: red" not in reading.text
    assert "Return any unopened item within 30 days" in reading.text


def test_a_policy_page_is_bounded_merchant_prose() -> None:
    outcome = extract_policy(read_page(RETURNS_POLICY), source_url=URL, name="returns")
    assert outcome.policy is not None
    assert outcome.policy.name == "returns"
    assert "30 days" in outcome.policy.body
    assert outcome.policy.truncated is False


def test_a_policy_page_longer_than_a_source_document_is_cut_and_says_so() -> None:
    long_page = f"<html><body><p>{'word ' * 3000}</p></body></html>"
    outcome = extract_policy(read_page(long_page), source_url=URL, name="returns")
    assert outcome.policy is not None
    assert outcome.policy.truncated is True
    assert len(outcome.policy.body) <= 4000
    assert any(finding.code == "policy_truncated" for finding in outcome.findings)


def test_a_policy_page_that_addresses_its_reader_is_not_imported() -> None:
    outcome = extract_policy(read_page(INSTRUCTION_POLICY), source_url=URL, name="returns")
    assert outcome.policy is None
    assert outcome.omission is not None
    assert outcome.omission.reason == "instruction_like"


def test_the_same_page_read_twice_produces_the_same_draft() -> None:
    """Determinism, which is what makes a re-import of an unchanged store write nothing."""
    first, _ = product(JSON_LD_PRODUCT)
    second, _ = product(JSON_LD_PRODUCT)
    assert first is not None and second is not None
    assert first.payload() == second.payload()


def test_two_different_merchant_identifiers_never_collide_onto_one() -> None:
    """A collision would lose a product to the source document's uniqueness check."""
    identifiers = Identifiers()
    first = identifiers.of("Blue / Large")
    second = identifiers.of("Blue - Large")
    assert first != second
    assert identifiers.of("Blue / Large") == first


def test_an_identifier_with_nothing_usable_in_it_still_produces_a_valid_one() -> None:
    identifiers = Identifiers()
    produced = identifiers.of("///")
    assert produced.startswith("item-")
    assert identifiers.of("///") == produced


def test_a_page_only_publishing_a_breadcrumb_still_states_its_category() -> None:
    found, _ = product(
        structured(
            '[{"@type":"BreadcrumbList","itemListElement":['
            '{"@type":"ListItem","name":"Home"},{"@type":"ListItem","name":"Cables"},'
            '{"@type":"ListItem","name":"USB-C 2m"}]},'
            '{"@type":"Product","name":"USB-C 2m","sku":"U2",'
            '"offers":{"@type":"Offer","price":"499","priceCurrency":"INR"}}]'
        )
    )
    assert found is not None
    assert found.category == "Cables"


def test_a_node_named_twice_in_one_graph_is_one_product() -> None:
    found, refused = product(
        structured(
            '{"@graph":[{"@id":"#p","@type":"Product","name":"X","sku":"S",'
            '"offers":{"@type":"Offer","price":"10","priceCurrency":"INR"}},'
            '{"@id":"#p","@type":"Product","name":"X","sku":"S",'
            '"offers":{"@type":"Offer","price":"10","priceCurrency":"INR"}}]}'
        )
    )
    assert refused is None
    assert found is not None


def test_a_related_product_nested_under_another_key_is_not_read_as_the_page_subject() -> None:
    """Walking every key would make a page about its cross sells as much as about its product."""
    nodes, _ = structured_nodes(
        read_page(
            structured(
                '{"@type":"Product","name":"X","sku":"S",'
                '"offers":{"@type":"Offer","price":"10","priceCurrency":"INR"},'
                '"isRelatedTo":[{"@type":"Product","name":"Other","sku":"O"}]}'
            )
        )
    )
    assert [node.get("name") for node in nodes] == ["X"]


def test_a_product_reached_through_main_entity_is_the_page_subject() -> None:
    found, refused = product(
        structured(
            '{"@type":"WebPage","mainEntity":{"@type":"Product","name":"Main","sku":"M",'
            '"offers":{"@type":"Offer","price":"10","priceCurrency":"INR"}}}'
        )
    )
    assert refused is None
    assert found is not None
    assert found.title == "Main"


@pytest.mark.parametrize(
    ("published", "currency", "expected"),
    [
        ("4999.00", "INR", 499900),
        (4999, "INR", 499900),
        (Decimal("4999.5"), "INR", 499950),
        ("1200", "JPY", 1200),
        ("12.345", "KWD", 12345),
        ("0", "USD", 0),
    ],
)
def test_a_published_price_becomes_exact_minor_units(
    published: object, currency: str, expected: int
) -> None:
    assert minor_units(published, currency) == expected


@pytest.mark.parametrize(
    ("published", "reason"),
    [
        ("", "price_missing"),
        ("1,299", "price_malformed"),
        ("₹4999", "price_malformed"),
        ("4999.00 INR", "price_malformed"),
        ("1e5", "price_malformed"),
        (True, "price_malformed"),
        ({"value": 1}, "price_malformed"),
        ("-5", "price_malformed"),
    ],
)
def test_a_figure_that_is_not_a_plain_number_is_refused(published: object, reason: str) -> None:
    with pytest.raises(RefusedAmountError) as refused:
        minor_units(published, "INR")
    assert refused.value.reason == reason


@pytest.mark.parametrize(
    ("published", "expected"),
    [("INR", "INR"), ("inr", "INR"), ("₹", "INR"), ("€", "EUR"), (" USD ", "USD")],
)
def test_a_currency_the_page_states_unambiguously_is_read(published: str, expected: str) -> None:
    assert normalize_currency(published) == expected


@pytest.mark.parametrize("published", ["$", "£", "¥", "Rs", "rupees", "", None])
def test_a_currency_that_names_more_than_one_currency_is_refused(published: object) -> None:
    """The dollar and the pound sign each denote several currencies, so neither is a currency."""
    with pytest.raises(RefusedAmountError):
        normalize_currency(published)


def test_a_currency_with_no_known_minor_unit_is_refused_rather_than_assumed_to_have_two() -> None:
    assert exponent("XYZ") is None
    with pytest.raises(RefusedAmountError) as refused:
        normalize_currency("XYZ")
    assert refused.value.reason == "currency_unsupported"


def test_an_imported_draft_is_a_document_the_ordinary_source_schema_accepts() -> None:
    """The whole point of producing the canonical shape rather than an importer flavoured one."""
    found, _ = product(JSON_LD_PRODUCT)
    policy = extract_policy(read_page(RETURNS_POLICY), source_url=URL, name="returns").policy
    assert found is not None and policy is not None
    draft = SourceDraft(products=(found,), policies=(policy,))
    document = SourceDocumentInput.model_validate(canonical_document(draft, stock_level=5))
    assert document.products[0].variants[0].inventory_quantity == 5
    assert "returns" in document.policy_text


def test_a_draft_round_trips_through_the_shape_it_is_persisted_as() -> None:
    found, _ = product(VARIANT_PRODUCT)
    assert found is not None
    draft = SourceDraft(products=(found,))
    assert SourceDraft.of(draft.payload()).payload() == draft.payload()


def test_a_variant_carries_the_availability_word_the_page_actually_used() -> None:
    """A merchant reading a draft should see their own page's word, not only a summary of it."""
    found, _ = product(JSON_LD_PRODUCT)
    assert found is not None
    variant: DraftVariant = found.variants[0]
    assert variant.availability_text == "https://schema.org/InStock"


def test_a_page_whose_title_is_never_closed_yields_nothing_rather_than_a_guess() -> None:
    """An unclosed RCDATA element swallows the rest of the document, exactly as a browser does.

    Worth pinning because the failure is silent by nature: the parser buffers everything waiting
    for a close tag that never comes, so the page reads as empty. Empty is the honest answer, and
    the import says so rather than falling back to anything.
    """
    reading = read_page(
        '<html><head><title>Broken\n<meta property="product:price:amount" content="9">'
        "<body><h1>Item</h1>"
    )
    assert reading.title is None
    assert reading.metadata == {}
    found, refused = product(
        '<html><head><title>Broken\n<meta property="product:price:amount" content="9">'
        "<body><h1>Item</h1>"
    )
    assert found is None
    assert refused == "price_missing"
