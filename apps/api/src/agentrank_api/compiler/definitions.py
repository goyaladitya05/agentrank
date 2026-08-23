"""Stable compiler identity and candidate shapes.

The first compiler is intentionally deterministic.  Merchant prose is parsed only for a small
set of explicit, bounded forms.  A model is not a useful dependency for this source format, so
there is no semantic-provider request surface to accidentally contaminate with benchmark data.
"""

import hashlib
from dataclasses import asdict, dataclass
from typing import Any

from agentrank_api.benchmark.identity import canonical_json
from agentrank_api.representation.definitions import AttributeKind, SemanticFact

COMPILER_KIND = "deterministic-merchant-compiler"
COMPILER_SEMANTIC_VERSION = "1"
DETERMINISTIC_EXTRACTOR_VERSION = "1"
NORMALIZATION_POLICY_VERSION = "1"
VALIDATION_POLICY_VERSION = "1"


def digest(value: dict[str, Any]) -> str:
    return "sha256:" + hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class CompilerConfiguration:
    """Everything that changes a compiler interpretation, excluding source identity."""

    compiler_kind: str = COMPILER_KIND
    compiler_semantic_version: str = COMPILER_SEMANTIC_VERSION
    deterministic_extractor_version: str = DETERMINISTIC_EXTRACTOR_VERSION
    semantic_extractor: str | None = None
    prompt_digest: str | None = None
    normalization_policy_version: str = NORMALIZATION_POLICY_VERSION
    validation_policy_version: str = VALIDATION_POLICY_VERSION

    def __post_init__(self) -> None:
        if self.compiler_kind != COMPILER_KIND or self.semantic_extractor is not None:
            raise ValueError("only the deterministic compiler configuration is supported")
        if self.prompt_digest is not None:
            raise ValueError("the deterministic compiler has no semantic prompt")
        if any(
            not value.strip()
            for value in (
                self.compiler_semantic_version,
                self.deterministic_extractor_version,
                self.normalization_policy_version,
                self.validation_policy_version,
            )
        ):
            raise ValueError("compiler configuration versions must not be blank")

    def payload(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def configuration_digest(self) -> str:
        return digest(self.payload())


@dataclass(frozen=True, slots=True)
class CandidateProposal:
    """A typed proposed Commerce IR fact, keyed by the field it would populate."""

    target: str
    fact: SemanticFact
    attribute_kind: AttributeKind | None = None
    unit: str | None = None
    requires_correction: bool = False

    def __post_init__(self) -> None:
        if not self.target.strip():
            raise ValueError("candidate target must not be blank")
        if self.attribute_kind is None and self.unit is not None:
            raise ValueError("only attribute candidates may carry units")
        if self.attribute_kind is AttributeKind.MEASUREMENT and not self.unit:
            raise ValueError("measurement candidate needs a unit")
        if self.attribute_kind is not AttributeKind.MEASUREMENT and self.unit is not None:
            raise ValueError("only measurement candidate may carry a unit")

    def payload(self) -> dict[str, Any]:
        return {
            "target": self.target,
            "fact": self.fact.payload(),
            "attribute_kind": None if self.attribute_kind is None else self.attribute_kind.value,
            "unit": self.unit,
            "requires_correction": self.requires_correction,
        }
