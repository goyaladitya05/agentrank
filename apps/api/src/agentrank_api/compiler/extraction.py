"""Small, evidence-preserving extractors for the Phase 3B source format."""

import re
from collections import defaultdict

from agentrank_api.compiler.definitions import CandidateProposal
from agentrank_api.compiler.models import CandidateState
from agentrank_api.representation.definitions import (
    AttributeKind,
    FactAuthority,
    FactConfidence,
    MerchantSourceDefinition,
    ReviewState,
    SemanticFact,
    SourceReference,
    ValueState,
)


def _authoritative(value: object, field: str) -> SemanticFact:
    return SemanticFact(
        value=value,
        authority=FactAuthority.AUTHORITATIVE,
        confidence=FactConfidence.AUTHORITATIVE,
        review_state=ReviewState.NOT_REQUIRED,
        provenance=(SourceReference(field),),
    )


def _derived(value: object, field: str, excerpt: str) -> SemanticFact:
    return SemanticFact(
        value=value,
        authority=FactAuthority.DERIVED,
        confidence=FactConfidence.HIGH,
        review_state=ReviewState.CONFIRMED,
        provenance=(SourceReference(field, excerpt),),
    )


def _review(value: object, field: str, excerpt: str) -> SemanticFact:
    return SemanticFact(
        value=value,
        authority=FactAuthority.DERIVED,
        confidence=FactConfidence.REVIEW_REQUIRED,
        review_state=ReviewState.REVIEW_REQUIRED,
        provenance=(SourceReference(field, excerpt),),
    )


def extract(source: MerchantSourceDefinition) -> list[tuple[CandidateProposal, CandidateState]]:
    """Extract only explicit source facts and preserve their exact supporting field."""
    output: list[tuple[CandidateProposal, CandidateState]] = []
    for product in source.products:
        prefix = f"products[{product.external_id}]"
        output.append(
            (
                CandidateProposal(
                    f"product.{product.external_id}.title",
                    _authoritative(product.title, f"{prefix}.title"),
                ),
                CandidateState.ACCEPTED,
            )
        )
        if product.category is not None:
            output.append(
                (
                    CandidateProposal(
                        f"product.{product.external_id}.category",
                        _authoritative(product.category, f"{prefix}.category"),
                    ),
                    CandidateState.ACCEPTED,
                )
            )
        for variant in product.variants:
            variant_prefix = f"{prefix}.variants[{variant.sku}]"
            output.extend(
                [
                    (
                        CandidateProposal(
                            f"variant.{variant.sku}.price",
                            _authoritative(
                                {
                                    "amount_minor": variant.price_amount_minor,
                                    "currency": variant.currency,
                                },
                                f"{variant_prefix}.price_amount_minor",
                            ),
                        ),
                        CandidateState.ACCEPTED,
                    ),
                    (
                        CandidateProposal(
                            f"variant.{variant.sku}.availability",
                            _authoritative(
                                ValueState.TRUE.value
                                if variant.inventory_quantity > 0
                                else ValueState.FALSE.value,
                                f"{variant_prefix}.inventory_quantity",
                            ),
                        ),
                        CandidateState.ACCEPTED,
                    ),
                ]
            )
            finish = variant.merchant_metadata.get("finish")
            if isinstance(finish, str) and finish.strip():
                output.append(
                    (
                        CandidateProposal(
                            f"variant.{variant.sku}.attribute.color",
                            _authoritative(
                                finish.lower(), f"{variant_prefix}.merchant_metadata.finish"
                            ),
                            AttributeKind.TEXT,
                        ),
                        CandidateState.ACCEPTED,
                    )
                )
            if variant.label:
                match = re.fullmatch(r"\s*(\d+)\s*m\s*", variant.label, re.IGNORECASE)
                if match:
                    output.append(
                        (
                            CandidateProposal(
                                f"variant.{variant.sku}.attribute.length",
                                _derived(
                                    int(match.group(1)),
                                    f"{variant_prefix}.label",
                                    match.group(0).strip(),
                                ),
                                AttributeKind.MEASUREMENT,
                                "m",
                            ),
                            CandidateState.ACCEPTED,
                        )
                    )
        _semantic_product(
            output,
            [variant.sku for variant in product.variants],
            product.title,
            product.description,
            prefix,
        )
    warranty = source.policy_text.get("warranty")
    if warranty is not None:
        match = re.search(r"\b(one|1)[ -]year\b", warranty, re.IGNORECASE)
        if match and not _instruction_like(warranty):
            output.append(
                (
                    CandidateProposal(
                        "policy.warranty_months",
                        _derived(12, "policy_text.warranty", match.group(0)),
                        AttributeKind.INTEGER,
                    ),
                    CandidateState.ACCEPTED,
                )
            )
    return output


def _semantic_product(
    output: list[tuple[CandidateProposal, CandidateState]],
    variant_skus: list[str],
    title: str,
    description: str | None,
    source_prefix: str,
) -> None:
    text_fields = [(f"{source_prefix}.title", title)]
    if description is not None:
        text_fields.append((f"{source_prefix}.description", description))
    if any(_instruction_like(text) for _, text in text_fields):
        return
    if any(
        re.search(r"\b(not|no|without|never)\b", text, re.IGNORECASE) for _, text in text_fields
    ):
        return
    watts: dict[int, list[tuple[str, str]]] = defaultdict(list)
    for field, text in text_fields:
        for match in re.finditer(r"\b(\d{1,4})\s*W\b", text, re.IGNORECASE):
            watts[int(match.group(1))].append((field, match.group(0)))
    if len(watts) == 1:
        value, evidence = next(iter(watts.items()))
        field, excerpt = evidence[0]
        for variant_sku in variant_skus:
            output.append(
                (
                    CandidateProposal(
                        f"variant.{variant_sku}.attribute.wattage",
                        _derived(value, field, excerpt),
                        AttributeKind.MEASUREMENT,
                        "W",
                    ),
                    CandidateState.ACCEPTED,
                )
            )
    elif len(watts) > 1:
        # A conflict is deliberately represented as a review-required candidate, never picked.
        field, excerpt = next(iter(watts.values()))[0]
        for variant_sku in variant_skus:
            output.append(
                (
                    CandidateProposal(
                        f"variant.{variant_sku}.attribute.wattage",
                        _review(0, field, excerpt),
                        AttributeKind.MEASUREMENT,
                        "W",
                        True,
                    ),
                    CandidateState.REVIEW_REQUIRED,
                )
            )
    if description is not None:
        ports = re.search(r"\b(\d+|two|three|four)[ -]port\b", description, re.IGNORECASE)
        if ports:
            number = {"two": 2, "three": 3, "four": 4}.get(ports.group(1).lower())
            value = int(ports.group(1)) if number is None else number
            for variant_sku in variant_skus:
                output.append(
                    (
                        CandidateProposal(
                            f"variant.{variant_sku}.attribute.ports",
                            _derived(value, f"{source_prefix}.description", ports.group(0)),
                            AttributeKind.INTEGER,
                        ),
                        CandidateState.ACCEPTED,
                    )
                )
    joined = " ".join(text for _, text in text_fields)
    if re.search(r"\bUSB[ -]?PD\b", joined, re.IGNORECASE):
        field, text = text_fields[-1]
        pd_match = re.search(r"\bUSB[ -]?PD\b", text, re.IGNORECASE)
        if pd_match:
            for variant_sku in variant_skus:
                output.append(
                    (
                        CandidateProposal(
                            f"variant.{variant_sku}.compatibility.usb-c-pd",
                            _review(ValueState.TRUE.value, field, pd_match.group(0)),
                        ),
                        CandidateState.REVIEW_REQUIRED,
                    )
                )
    elif re.search(r"USB-C\s+to\s+USB-C", joined, re.IGNORECASE):
        field, text = text_fields[0]
        cable_match = re.search(r"USB-C\s+to\s+USB-C", text, re.IGNORECASE)
        if cable_match:
            for variant_sku in variant_skus:
                output.append(
                    (
                        CandidateProposal(
                            f"variant.{variant_sku}.compatibility.usb-c",
                            _derived(ValueState.TRUE.value, field, cable_match.group(0)),
                        ),
                        CandidateState.ACCEPTED,
                    )
                )


def _instruction_like(text: str) -> bool:
    """Do not interpret a source string that impersonates compiler instructions."""
    return bool(
        re.search(
            r"\b(ignore|disregard)\s+(all\s+)?(previous|compiler|system)\s+instructions?\b",
            text,
            re.IGNORECASE,
        )
    )
