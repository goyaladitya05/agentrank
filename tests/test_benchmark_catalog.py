"""The merchant's authoritative data as the benchmark reads it: the pin, and the predicates.

Pure, and tested directly rather than only through the run service. A predicate reached only
through a fixture is a predicate whose fail closed branches nobody has stated.
"""

import uuid

import pytest
from benchmark_support import BLACK, BUDGET, CHARGERS, CURRENCY, brief

from agentrank_api.benchmark.catalog import CatalogEntry, catalog_content_hash, facts_for, satisfies
from agentrank_api.benchmark.observation import (
    ObservedSelection,
)
from agentrank_api.constraints.rules import ConstraintOperator
from agentrank_api.mandates.intent import MaxQuantity, RequiredAttribute

PRICE = 400000


def entry(
    *,
    sku: str = "VE-1",
    category: str | None = "chargers",
    attributes: dict[str, object] | None = None,
    price: int = PRICE,
    currency: str = CURRENCY,
    stock: int = 5,
    active: bool = True,
    variant_id: uuid.UUID | None = None,
) -> CatalogEntry:
    return CatalogEntry(
        variant_id=variant_id or uuid.uuid7(),
        sku=sku,
        product_category=category,
        attributes={"color": "black"} if attributes is None else attributes,
        price_amount_minor=price,
        currency=currency,
        inventory_quantity=stock,
        is_active=active,
    )


# What satisfies a mission, and every way it fails closed.


def test_a_qualifying_variant_satisfies_the_mission() -> None:
    assert satisfies(brief(constraints=(CHARGERS, BLACK)), entry()) is True


@pytest.mark.parametrize(
    ("label", "candidate"),
    [
        ("out of stock", entry(stock=0)),
        ("no longer sold", entry(active=False)),
        ("priced in another currency", entry(currency="EUR", price=8999)),
        ("over the budget", entry(price=BUDGET + 1)),
        ("no category published", entry(category=None)),
        ("the wrong category", entry(category="headphones")),
        ("the attribute is missing", entry(attributes={"colour_family": "dark"})),
        ("the attribute is wrong", entry(attributes={"color": "blue"})),
    ],
)
def test_nothing_satisfies_a_mission_it_does_not_meet(label: str, candidate: CatalogEntry) -> None:
    """Every branch stated once. A predicate that only ever gets asked about the happy case is
    a predicate whose refusals nobody has established."""
    assert satisfies(brief(constraints=(CHARGERS, BLACK)), candidate) is False, label


def test_an_attribute_that_cannot_be_compared_is_not_a_pass() -> None:
    """`"100W"` is not a number, and an unanswerable question is never a yes."""
    stated = brief(constraints=(RequiredAttribute("wattage", 100, ConstraintOperator.GTE),))

    assert satisfies(stated, entry(attributes={"wattage": "100W"})) is False
    assert satisfies(stated, entry(attributes={"wattage": 140})) is True


def test_a_multi_unit_mission_is_priced_by_the_line_not_the_unit() -> None:
    """Two at 400000 is 800000, and a 500000 budget does not cover it."""
    stated = brief(quantity=2, constraints=())

    assert satisfies(stated, entry(price=PRICE)) is False
    assert satisfies(stated, entry(price=200000)) is True


def test_a_multi_unit_mission_is_stocked_by_the_line_not_the_unit() -> None:
    """One unit left is not enough for a mission that wants two.

    Found by an independent review, which noticed that the budget comparison beside this one
    already multiplied by the quantity and this one did not. The consequence was not a smaller
    number: a mission whose only qualifying variant had one unit left was reported as
    satisfiable here and declined by the executor, so the executor was marked down for a
    discovery failure it never had a chance at while the oracle check reported no disagreement
    to explain it.
    """
    stated = brief(quantity=2, constraints=(), budget_minor=BUDGET)

    assert satisfies(stated, entry(price=200000, stock=1)) is False
    assert satisfies(stated, entry(price=200000, stock=2)) is True


def test_a_mission_cannot_want_more_units_than_it_authorizes() -> None:
    """Unsatisfiable by construction, and for a reason no merchant has anything to do with."""
    with pytest.raises(ValueError, match="while authorizing"):
        brief(quantity=3, constraints=(MaxQuantity(2),))


# Sellable, which is about the catalog and not about the shelf.


def test_a_variant_the_merchant_has_run_out_of_is_still_one_it_sells() -> None:
    """The distinction `INVALID_VARIANT` and `INVENTORY_UNAVAILABLE` exist to keep apart.

    Reporting an empty shelf as a catalog listing things nobody sells hands a merchant the
    wrong repair entirely.
    """
    variant_id = uuid.uuid7()
    empty = entry(variant_id=variant_id, stock=0)

    facts = facts_for(brief(constraints=()), [empty], _selection(variant_id))

    assert facts.selection_is_sellable is True
    # And it still satisfies nothing, because nobody can take one away.
    assert facts.qualifying_variant_exists is False


def test_a_withdrawn_variant_is_not_one_the_merchant_sells() -> None:
    variant_id = uuid.uuid7()
    withdrawn = entry(variant_id=variant_id, active=False)

    facts = facts_for(brief(constraints=()), [withdrawn], _selection(variant_id))

    assert facts.selection_is_sellable is False


def test_a_variant_the_merchant_has_never_had_is_not_one_it_sells() -> None:
    """The shape a hallucinated identifier takes."""
    facts = facts_for(brief(constraints=()), [entry()], _selection(uuid.uuid7()))

    assert facts.selection_is_sellable is False


def test_nothing_is_claimed_about_a_selection_that_was_never_made() -> None:
    facts = facts_for(brief(constraints=()), [entry()], None)

    assert facts.selection_is_sellable is None
    assert facts.qualifying_variant_exists is True


# The pin.


@pytest.mark.parametrize(
    ("label", "changed"),
    [
        ("price", entry(price=450000)),
        ("stock", entry(stock=4)),
        ("withdrawal", entry(active=False)),
        ("category", entry(category="cables")),
        ("attribute", entry(attributes={"color": "white"})),
        ("currency", entry(currency="EUR", price=8999)),
    ],
)
def test_every_catalog_fact_a_mission_reads_moves_the_pin(
    label: str, changed: CatalogEntry
) -> None:
    """The pin is what makes a before and after comparison attributable, or visibly not.

    A field that drops out of it is a merchant that can move without any run noticing.
    """
    assert catalog_content_hash([changed]) != catalog_content_hash([entry()]), label


def test_the_pin_does_not_depend_on_the_order_rows_came_back_in() -> None:
    first = entry(sku="VE-1")
    second = entry(sku="VE-2", price=200000)

    assert catalog_content_hash([first, second]) == catalog_content_hash([second, first])


def test_the_pin_does_not_depend_on_database_identifiers() -> None:
    """Two databases holding the same catalog pin the same hash."""
    here = entry(sku="VE-1", variant_id=uuid.uuid7())
    there = entry(sku="VE-1", variant_id=uuid.uuid7())

    assert catalog_content_hash([here]) == catalog_content_hash([there])


def test_a_catalog_entry_refuses_impossible_values() -> None:
    with pytest.raises(ValueError, match="must not be negative"):
        entry(stock=-1)
    with pytest.raises(ValueError, match="currency must match"):
        entry(currency="rupees")


def _selection(variant_id: uuid.UUID) -> ObservedSelection:
    return ObservedSelection(
        variant_id=variant_id,
        quantity=1,
        unit_price_amount_minor=PRICE,
        currency=CURRENCY,
        product_category="chargers",
        variant_attributes={"color": "black"},
    )
