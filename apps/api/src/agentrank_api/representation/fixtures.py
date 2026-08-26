"""Strict readers for merchant source and manually-authored Commerce IR fixtures.

These documents are operator artifacts.  Their fixed schemas reject arbitrary fields, so a
benchmark oracle cannot be smuggled through an opaque JSON blob.
"""

import json
from pathlib import Path
from typing import Any

from agentrank_api.representation.definitions import (
    AttributeKind,
    CommerceAttribute,
    CommerceIRDefinition,
    CommerceProduct,
    CommerceVariant,
    FactAuthority,
    FactConfidence,
    MerchantSourceDefinition,
    RepresentationProducer,
    ReviewState,
    SemanticFact,
    SourceProduct,
    SourceReference,
    SourceVariant,
    read_availability,
)


class RepresentationFixtureError(ValueError):
    pass


def read_source(path: Path) -> MerchantSourceDefinition:
    return parse_source(_document(path))


def parse_source(document: dict[str, Any]) -> MerchantSourceDefinition:
    """Validate a persisted source payload before a compiler interprets it."""
    _only(document, {"key", "version", "merchant_slug", "products", "policy_text"}, "source")
    return MerchantSourceDefinition(
        key=_string(document, "key"),
        version=_integer(document, "version"),
        merchant_slug=_string(document, "merchant_slug"),
        products=tuple(_source_product(entry) for entry in _array(document, "products")),
        policy_text=_string_map(document, "policy_text"),
    )


def read_ir(path: Path) -> CommerceIRDefinition:
    document = _document(path)
    _only(
        document,
        {"source_key", "source_version", "source_hash", "producer", "producer_version", "products"},
        "Commerce IR",
    )
    return CommerceIRDefinition(
        source_key=_string(document, "source_key"),
        source_version=_integer(document, "source_version"),
        source_hash=_string(document, "source_hash"),
        producer=_enum(RepresentationProducer, _string(document, "producer")),
        producer_version=_string(document, "producer_version"),
        products=tuple(_commerce_product(entry) for entry in _array(document, "products")),
    )


def _source_product(value: Any) -> SourceProduct:
    entry = _object(value, "source product")
    _only(
        entry,
        {"external_id", "title", "description", "category", "variants", "merchant_metadata"},
        "source product",
    )
    return SourceProduct(
        external_id=_string(entry, "external_id"),
        title=_string(entry, "title"),
        description=_optional_string(entry, "description"),
        category=_optional_string(entry, "category"),
        variants=tuple(_source_variant(item) for item in _array(entry, "variants")),
        merchant_metadata=_object(entry.get("merchant_metadata", {}), "merchant metadata"),
    )


def _source_variant(value: Any) -> SourceVariant:
    """One stored or authored variant, with its availability read rather than assumed.

    A document written before availability existed carries a quantity and no state, and the
    quantity already says which state it is. A document written since carries a state and a
    quantity that may be null. Both are read here, and a document carrying two facts that
    disagree is refused rather than resolved.
    """
    entry = _object(value, "source variant")
    _only(
        entry,
        {
            "sku",
            "label",
            "price_amount_minor",
            "currency",
            "inventory_quantity",
            "merchant_metadata",
        },
        "source variant",
        {"availability"},
    )
    quantity = _nullable_integer(entry, "inventory_quantity")
    sku = _string(entry, "sku")
    return SourceVariant(
        sku=sku,
        label=_optional_string(entry, "label"),
        price_amount_minor=_integer(entry, "price_amount_minor"),
        currency=_string(entry, "currency"),
        availability=read_availability(
            quantity, _optional_string(entry, "availability"), where=f"source variant {sku!r}"
        ),
        inventory_quantity=quantity,
        merchant_metadata=_object(entry.get("merchant_metadata", {}), "merchant metadata"),
    )


def _commerce_product(value: Any) -> CommerceProduct:
    entry = _object(value, "Commerce IR product")
    _only(
        entry,
        {"external_id", "title", "category", "variants", "policy_facts"},
        "Commerce IR product",
    )
    category = entry.get("category")
    return CommerceProduct(
        external_id=_string(entry, "external_id"),
        title=_fact(entry.get("title")),
        category=None if category is None else _fact(category),
        variants=tuple(_commerce_variant(item) for item in _array(entry, "variants")),
        policy_facts={
            key: _fact(fact)
            for key, fact in _object(entry.get("policy_facts", {}), "policy facts").items()
        },
    )


def _commerce_variant(value: Any) -> CommerceVariant:
    entry = _object(value, "Commerce IR variant")
    _only(
        entry,
        {"sku", "label", "price", "availability", "attributes", "compatibility"},
        "Commerce IR variant",
    )
    return CommerceVariant(
        sku=_string(entry, "sku"),
        label=_optional_string(entry, "label"),
        price=_fact(entry.get("price")),
        availability=_fact(entry.get("availability")),
        attributes=tuple(_attribute(item) for item in _array(entry, "attributes")),
        compatibility={
            key: _fact(fact)
            for key, fact in _object(entry.get("compatibility", {}), "compatibility").items()
        },
    )


def _attribute(value: Any) -> CommerceAttribute:
    entry = _object(value, "Commerce IR attribute")
    _only(entry, {"key", "kind", "unit", "fact"}, "Commerce IR attribute")
    return CommerceAttribute(
        key=_string(entry, "key"),
        kind=_enum(AttributeKind, _string(entry, "kind")),
        unit=_optional_string(entry, "unit"),
        fact=_fact(entry.get("fact")),
    )


def _fact(value: Any) -> SemanticFact:
    entry = _object(value, "Commerce IR fact")
    _only(
        entry,
        {"value", "authority", "confidence", "review_state", "provenance"},
        "Commerce IR fact",
    )
    if "value" not in entry:
        raise RepresentationFixtureError("Commerce IR fact requires value")
    return SemanticFact(
        value=entry["value"],
        authority=_enum(FactAuthority, _string(entry, "authority")),
        confidence=_enum(FactConfidence, _string(entry, "confidence")),
        review_state=_enum(ReviewState, _string(entry, "review_state")),
        provenance=tuple(_reference(item) for item in _array(entry, "provenance")),
    )


def _reference(value: Any) -> SourceReference:
    entry = _object(value, "provenance")
    _only(entry, {"field", "excerpt"}, "provenance")
    return SourceReference(
        field=_string(entry, "field"), excerpt=_optional_string(entry, "excerpt")
    )


def _document(path: Path) -> dict[str, Any]:
    try:
        return _object(json.loads(path.read_text(encoding="utf-8")), str(path))
    except OSError as error:
        raise RepresentationFixtureError(f"cannot read {path}: {error.strerror}") from error
    except json.JSONDecodeError as error:
        raise RepresentationFixtureError(f"{path}: invalid JSON") from error


def _only(
    entry: dict[str, Any], allowed: set[str], name: str, optional: set[str] | None = None
) -> None:
    """Refuse a document that carries a key this format lacks, or lacks one it defines.

    `optional` is for a key the canonical form writes only where it carries information. A
    variant that states an exact quantity has already stated its availability, so the canonical
    payload omits the availability key; requiring it would refuse every document written before
    the field existed, and allowing it as an extra everywhere would let a typo through.
    """
    extra = set(entry) - allowed - (optional or set())
    missing = allowed - set(entry)
    if extra or missing:
        raise RepresentationFixtureError(f"{name} has unsupported or missing fields")


def _object(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise RepresentationFixtureError(f"{name} must be an object")
    return dict(value)


def _array(entry: dict[str, Any], name: str) -> list[Any]:
    value = entry.get(name)
    if not isinstance(value, list):
        raise RepresentationFixtureError(f"{name} must be an array")
    return value


def _string(entry: dict[str, Any], name: str) -> str:
    value = entry.get(name)
    if not isinstance(value, str):
        raise RepresentationFixtureError(f"{name} must be a string")
    return value


def _optional_string(entry: dict[str, Any], name: str) -> str | None:
    value = entry.get(name)
    if value is not None and not isinstance(value, str):
        raise RepresentationFixtureError(f"{name} must be a string or null")
    return value


def _integer(entry: dict[str, Any], name: str) -> int:
    value = entry.get(name)
    if not isinstance(value, int) or isinstance(value, bool):
        raise RepresentationFixtureError(f"{name} must be an integer")
    return value


def _nullable_integer(entry: dict[str, Any], name: str) -> int | None:
    """An integer or an explicit null, with an absent key refused rather than read as null.

    A canonical source document always writes this key, so a document missing it is a document
    this reader was not given. Reading the absence as null would make a misspelled key a variant
    with no stock quantity, which is a document that reads correctly and means something else.
    """
    if name not in entry:
        raise RepresentationFixtureError(f"{name} must be present, as an integer or null")
    value = entry[name]
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool):
        raise RepresentationFixtureError(f"{name} must be an integer or null")
    return value


def _string_map(entry: dict[str, Any], name: str) -> dict[str, str]:
    value = _object(entry.get(name), name)
    if any(not isinstance(key, str) or not isinstance(body, str) for key, body in value.items()):
        raise RepresentationFixtureError(f"{name} must map strings to strings")
    return value


def _enum(enum_type: type[Any], value: str) -> Any:
    try:
        return enum_type(value)
    except ValueError as error:
        raise RepresentationFixtureError(f"unknown enum value {value!r}") from error
