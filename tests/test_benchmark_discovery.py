"""The buyer-facing discovery boundary, as data rather than experiment intention.

These tests prove the treatment split where it actually lives: in the tool answers a model
buyer reads. A storefront arm must never see a typed attribute dictionary from anywhere, an
agent-ready arm must see exactly the pinned representation's facts and nothing else, and the
quote channel must stay financial truth for both arms alike.
"""

import json
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest

from agentrank_api.benchmark.discovery import (
    DiscoveryKind,
    agent_ready_view,
    buyer_discovery_view,
    discover,
    quoted,
    storefront_view,
    to_payload,
    view_from_payload,
)
from agentrank_api.checkout.models import CheckoutStatus
from agentrank_api.checkout.schemas import CheckoutLineView, CheckoutView
from agentrank_api.commerce.schemas import (
    MerchantSummary,
    ProductDetail,
    ProductSearchResponse,
    ProductSearchResult,
    VariantView,
)

pytestmark = pytest.mark.anyio

IR_PATH = Path("benchmarks/voltedge/commerce_ir.json")


def _variant(sku: str) -> VariantView:
    return VariantView(
        id=uuid.uuid7(),
        sku=sku,
        label="Black",
        attributes={"color": "black", "wattage": 100, "ports": 3},
        price_amount_minor=499900,
        currency="INR",
        inventory_quantity=20,
        is_active=True,
    )


def _product(variant: VariantView) -> ProductSearchResult:
    return ProductSearchResult(
        id=uuid.uuid7(),
        external_id="VE-CHG-100",
        title="100W Multi-Port Charger",
        description="100W three-port USB-C charger for a shared workday.",
        category="chargers",
        is_active=True,
        merchant=MerchantSummary(id=uuid.uuid7(), slug="voltedge", name="VoltEdge"),
        eligible_variants=[variant],
    )


def _search_response(variant: VariantView) -> ProductSearchResponse:
    return ProductSearchResponse(results=[_product(variant)], count=1, limit=20)


async def test_a_storefront_discovery_answer_carries_no_typed_attributes() -> None:
    answer = discover(
        storefront_view(), _search_response(_variant("VE-CHG-100-BLK")).model_dump(mode="json")
    )
    serialized = json.dumps(answer)

    assert "attributes" not in serialized
    # Everything an ordinary storefront publishes survives untouched.
    assert "100W Multi-Port Charger" in serialized
    assert "three-port USB-C charger" in serialized
    assert answer["results"][0]["eligible_variants"][0]["price_amount_minor"] == 499900
    assert answer["results"][0]["eligible_variants"][0]["inventory_quantity"] == 20


async def test_an_agent_ready_answer_publishes_only_the_pinned_representation_facts() -> None:
    representation_id = uuid.uuid7()
    view = agent_ready_view(
        representation_id,
        {"VE-CHG-100-BLK": ({"key": "wattage", "kind": "MEASUREMENT", "unit": "W", "value": 100},)},
    )
    answer = discover(view, _search_response(_variant("VE-CHG-100-BLK")).model_dump(mode="json"))
    attributes = answer["results"][0]["eligible_variants"][0]["attributes"]

    assert attributes == [{"key": "wattage", "kind": "MEASUREMENT", "unit": "W", "value": 100}]
    # Compiler workflow metadata stays behind the boundary.
    serialized = json.dumps(attributes)
    for forbidden in ("provenance", "authority", "confidence", "review_state"):
        assert forbidden not in serialized


async def test_a_variant_the_representation_does_not_cover_is_unenriched_not_catalog_read() -> None:
    view = agent_ready_view(uuid.uuid7(), {"SOME-OTHER-SKU": ()})
    answer = discover(view, _search_response(_variant("VE-CHG-100-BLK")).model_dump(mode="json"))

    assert "attributes" not in json.dumps(answer)


async def test_product_detail_answers_are_projected_like_search_answers() -> None:
    variant = _variant("VE-CHG-100-BLK")
    detail = ProductDetail(
        id=uuid.uuid7(),
        external_id="VE-CHG-100",
        title="100W Multi-Port Charger",
        description=None,
        category="chargers",
        is_active=True,
        merchant=MerchantSummary(id=uuid.uuid7(), slug="voltedge", name="VoltEdge"),
        variants=[variant],
    )
    answer = discover(storefront_view(), detail.model_dump(mode="json"))

    assert "attributes" not in json.dumps(answer)


def _checkout() -> CheckoutView:
    instant = datetime.now(UTC)
    return CheckoutView(
        id=uuid.uuid7(),
        merchant_id=uuid.uuid7(),
        mandate_id=uuid.uuid7(),
        currency="INR",
        lines=[
            CheckoutLineView(
                id=uuid.uuid7(),
                variant_id=uuid.uuid7(),
                quantity=1,
                unit_price_amount_minor=499900,
                line_amount_minor=499900,
                currency="INR",
                product_category="chargers",
                variant_attributes={"color": "black", "wattage": 100},
            )
        ],
        total_quantity=1,
        subtotal_amount_minor=499900,
        shipping_amount_minor=0,
        discount_amount_minor=0,
        total_amount_minor=499900,
        status=CheckoutStatus.OPEN,
        created_at=instant,
        expires_at=instant,
        cancelled_at=None,
        paid_at=None,
    )


async def test_the_quote_channel_stays_financial_truth_for_both_arms_alike() -> None:
    dumped = _checkout().model_dump(mode="json")
    document = quoted(dumped)

    assert "variant_attributes" not in json.dumps(document)
    assert document["total_amount_minor"] == 499900
    assert document["lines"][0]["line_amount_minor"] == 499900
    assert document["lines"][0]["product_category"] == "chargers"
    # Identical stripping under the agent-ready view: this is not part of the treatment.
    through_agent_view = discover(agent_ready_view(uuid.uuid7(), {}), dumped)
    assert quoted(through_agent_view) == document


async def test_the_treatment_builder_pairs_sample_identity_with_its_own_surface() -> None:
    representation_id = uuid.uuid7()
    payload = {
        "products": [
            {
                "variants": [
                    {
                        "sku": "VE-CHG-100-BLK",
                        "attributes": [
                            {
                                "key": "wattage",
                                "kind": "MEASUREMENT",
                                "unit": "W",
                                "fact": {"value": 100},
                            }
                        ],
                    }
                ]
            }
        ]
    }

    raw = buyer_discovery_view(
        representation_kind="RAW", representation_id=None, representation_payload=None
    )
    compiled = buyer_discovery_view(
        representation_kind="COMPILED",
        representation_id=representation_id,
        representation_payload=payload,
    )

    assert raw.kind is DiscoveryKind.STOREFRONT
    assert compiled.kind is DiscoveryKind.AGENT_READY
    assert compiled.attributes_by_sku["VE-CHG-100-BLK"] == (
        {"key": "wattage", "kind": "MEASUREMENT", "unit": "W", "value": 100},
    )
    with pytest.raises(ValueError, match="raw treatment"):
        buyer_discovery_view(
            representation_kind="RAW",
            representation_id=representation_id,
            representation_payload=payload,
        )
    with pytest.raises(ValueError, match="compiled treatment"):
        buyer_discovery_view(
            representation_kind="COMPILED", representation_id=None, representation_payload=None
        )


def test_the_real_voltedge_ir_flattens_into_buyer_facts_by_sku() -> None:
    payload = json.loads(IR_PATH.read_text())
    view = buyer_discovery_view(
        representation_kind="COMPILED",
        representation_id=uuid.uuid7(),
        representation_payload=payload,
    )

    by_sku = view.attributes_by_sku
    charger = next(facts for sku, facts in by_sku.items() if sku == "VE-CHG-100-BLK")
    wattage = next(fact for fact in charger if fact["key"] == "wattage")
    assert wattage == {"key": "wattage", "kind": "MEASUREMENT", "unit": "W", "value": 100}
    # Every IR variant is covered, so the enrichment cannot silently miss the world.
    assert len(by_sku) == sum(len(product["variants"]) for product in payload["products"])


async def test_the_wire_round_trips_both_views_and_refuses_everything_else() -> None:
    storefront_payload = to_payload(storefront_view())
    assert view_from_payload(storefront_payload) == storefront_view()

    representation_id = uuid.uuid7()
    agent_payload = to_payload(
        agent_ready_view(
            representation_id,
            {"SKU": ({"key": "color", "kind": "TEXT", "unit": None, "value": "black"},)},
        )
    )
    rebuilt = view_from_payload(agent_payload)
    assert rebuilt.kind is DiscoveryKind.AGENT_READY
    assert rebuilt.representation_id == representation_id
    assert to_payload(rebuilt) == agent_payload

    for malformed in (
        {"kind": "ORACLE"},
        {"kind": "STOREFRONT", "representation_id": str(uuid.uuid7())},
        {"kind": "AGENT_READY"},
        {
            "kind": "AGENT_READY",
            "representation_id": str(representation_id),
            "attributes": [
                {
                    "sku": "SKU",
                    "key": "expected_outcome",
                    "kind": "TEXT",
                    "unit": None,
                    "value": "PURCHASE_AVAILABLE",
                }
            ],
        },
    ):
        with pytest.raises(ValueError):
            view_from_payload(malformed)
