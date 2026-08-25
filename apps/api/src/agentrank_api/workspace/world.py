"""Reading a generated benchmark world back out of the row that stored it.

An authored world lives in `benchmarks/<world>/catalog.json` and an operator command is given a
path to it. A generated world has no file, so the fixture a run puts the merchant's catalog back
to is the payload on the workspace row, and this is the one place that turns it back into the
validated domain object every other part of the benchmark already takes.

Two guards, and both are the same guard the authored reader applies to a file.

The payload goes through `BenchmarkFixture` rather than being trusted, so a row edited around
this application cannot become a world with a negative stock level, a duplicate SKU or a
fractional attribute. And the digest is compared with the `catalog_hash` stored beside it, so a
payload that was changed after the world was registered is refused by name rather than prepared:
the registered environment's own `fixture_hash` would no longer describe it, and preparation
would then refuse anyway with a message about a fixture nobody can find.

`WorkspaceWorld` is deliberately the fixture and the merchant slug and nothing else. It carries
no suite, because the dispatcher reads the workload from the launch and the runner reads missions
from rows; giving this a suite would be putting an oracle in a place that has no use for one.
"""

from dataclasses import dataclass
from typing import Any

from agentrank_api.benchmark.fixtures import BenchmarkFixture
from agentrank_api.commerce.catalog_fixture import SeedProduct, SeedVariant
from agentrank_api.errors import ConflictError
from agentrank_api.workspace.models import MerchantEvaluationWorkspace

RESOURCE = "merchant_evaluation_workspace"


@dataclass(frozen=True, slots=True)
class WorkspaceWorld:
    """One generated merchant world, in the shape the benchmark dispatcher takes.

    Structurally what an `AuthoredWorld` offers a dispatcher: which merchant, and the catalog
    their world is put back to. The dispatcher accepts either, because what it needs from a
    world is exactly these two things and where they came from is not its business.
    """

    merchant_slug: str
    fixture: BenchmarkFixture


def workspace_world(workspace: MerchantEvaluationWorkspace) -> WorkspaceWorld:
    """The world this workspace generated, ready for the existing dispatcher."""
    fixture = workspace_fixture(workspace)
    return WorkspaceWorld(merchant_slug=fixture.merchant_slug, fixture=fixture)


def workspace_fixture(workspace: MerchantEvaluationWorkspace) -> BenchmarkFixture:
    """The stored catalog payload as the validated world it describes.

    Refuses rather than adapts. A payload this build cannot read as a fixture, or one whose
    digest is not the one the workspace recorded, is a world nobody can vouch for, and preparing
    a catalog from one would be overwriting a merchant's shelf with something unverified.
    """
    payload = workspace.catalog_fixture
    try:
        fixture = BenchmarkFixture(
            key=_text(payload, "key"),
            version=_integer(payload, "version"),
            merchant_slug=_text(payload, "merchant_slug"),
            merchant_name=_text(payload, "merchant_name"),
            products=tuple(_product(entry) for entry in _array(payload, "products")),
        )
    except (KeyError, TypeError, ValueError) as unreadable:
        raise ConflictError(
            "workspace_catalog_unreadable",
            f"the evaluation world stored for workspace {workspace.id} is not a benchmark"
            " fixture this build can read",
            resource=RESOURCE,
            identifier=str(workspace.id),
        ) from unreadable
    if fixture.content_hash != workspace.catalog_hash:
        raise ConflictError(
            "workspace_catalog_changed",
            f"workspace {workspace.id} recorded world {workspace.catalog_hash} and its stored"
            f" catalog now hashes to {fixture.content_hash}",
            resource=RESOURCE,
            identifier=str(workspace.id),
        )
    return fixture


def _product(payload: Any) -> SeedProduct:
    entry = _object(payload)
    return SeedProduct(
        external_id=_text(entry, "external_id"),
        title=_text(entry, "title"),
        description=_optional_text(entry, "description"),
        category=_optional_text(entry, "category"),
        is_active=_boolean(entry, "is_active"),
        variants=tuple(_variant(item) for item in _array(entry, "variants")),
    )


def _variant(payload: Any) -> SeedVariant:
    entry = _object(payload)
    attributes = entry.get("attributes", {})
    if not isinstance(attributes, dict):
        raise TypeError("variant attributes must be an object")
    return SeedVariant(
        sku=_text(entry, "sku"),
        label=_optional_text(entry, "label"),
        price_amount_minor=_integer(entry, "price_amount_minor"),
        currency=_text(entry, "currency"),
        inventory_quantity=_integer(entry, "inventory_quantity"),
        attributes=dict(attributes),
        is_active=_boolean(entry, "is_active"),
    )


def _object(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise TypeError("a stored world entry must be an object")
    return payload


def _array(payload: dict[str, Any], name: str) -> list[Any]:
    value = payload.get(name)
    if not isinstance(value, list):
        raise TypeError(f"{name} must be an array")
    return value


def _text(payload: dict[str, Any], name: str) -> str:
    value = payload.get(name)
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    return value


def _optional_text(payload: dict[str, Any], name: str) -> str | None:
    return None if payload.get(name) is None else _text(payload, name)


def _integer(payload: dict[str, Any], name: str) -> int:
    value = payload.get(name)
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError(f"{name} must be an integer")
    return value


def _boolean(payload: dict[str, Any], name: str) -> bool:
    value = payload.get(name)
    if not isinstance(value, bool):
        raise TypeError(f"{name} must be true or false")
    return value
