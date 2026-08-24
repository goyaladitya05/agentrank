"""What a merchant may say about their own source evidence, and what they are told back.

The source document is the untrusted half of this system. Everything else a merchant submits is
a decision about something AgentRank already holds: a review names a candidate, a publication
names a run, a launch names a representation. This names nothing. It is content, it becomes an
immutable artifact, a deterministic compiler reads it, and what the compiler reads becomes an
agent-facing representation. So it is bounded here, hard, in the layer the browser reaches.

Four rules the request models enforce and the domain does not:

```text
identity is the server's   no key, no version, no merchant. A body carrying one is refused
every field is bounded     length, count and range, so no document is unbounded work
nothing is coerced         strict mode, so "12" is not 12 and 1 is not true
nothing extra survives     extra="forbid", so a field this schema lacks is a refusal
```

`extra="forbid"` is doing real work rather than being tidy. A body carrying `merchant_slug`,
`version` or `content_hash` is a caller who believed it would take effect, and quietly dropping
one is how a request comes to mean something the caller did not intend. It is also the
compiler firewall at this boundary: a benchmark answer smuggled into an unexpected field would
be stored as source evidence and read by the extractor, and there is no unexpected field.

Identifiers are restricted to a shape a source field address and a compiler target can both
carry unambiguously. A SKU containing a dot or a bracket would make
`products[X].variants[Y].label` and `variant.Y.attribute.wattage` parse two ways, and provenance
that parses two ways is provenance that proves nothing.
"""

import re
import uuid
from datetime import datetime
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from agentrank_api.money import CURRENCY_PATTERN
from agentrank_api.representation.definitions import (
    MerchantSourceDefinition,
    SourceProduct,
    SourceVariant,
)
from agentrank_api.representation.models import SUBMISSION_KEY_PATTERN

# The whole request body a merchant may POST, in bytes. Checked before the body is parsed, so an
# oversized document is refused rather than buffered, parsed and then found to be too large.
MAX_SOURCE_REQUEST_BYTES = 128 * 1024

# How deeply a source document may nest. Seven is what the shape below actually needs: the
# document, its product array, one product, its variant array, one variant, that variant's
# metadata, and a value inside it. Twelve leaves room and is still far below the depth at which
# the JSON parser recurses into the interpreter's stack limit, which is a `RecursionError` rather
# than a parse error and would otherwise reach a caller as a 500.
MAX_SOURCE_REQUEST_DEPTH = 12

MAX_PRODUCTS = 50
MAX_VARIANTS_PER_PRODUCT = 25
MAX_TOTAL_VARIANTS = 250
MAX_POLICY_ENTRIES = 20
MAX_METADATA_ENTRIES = 20

MAX_TITLE_LENGTH = 200
MAX_DESCRIPTION_LENGTH = 2000
MAX_CATEGORY_LENGTH = 64
MAX_LABEL_LENGTH = 64
MAX_METADATA_VALUE_LENGTH = 500
MAX_POLICY_BODY_LENGTH = 4000

# Money and stock are bounded so that no document can carry a number the catalog, the money
# helpers or a JSON reader would have to treat as special. Neither is authoritative: these are
# what the merchant says, and the commerce runtime remains the only place a price or a stock
# level means anything.
MAX_PRICE_AMOUNT_MINOR = 10**12
MAX_INVENTORY_QUANTITY = 10**7

# A metadata integer is bounded for the same reason a price is. It is transitively bounded by the
# request size cap already, and "every field is bounded" should be true of every field rather than
# true of most of them and left to a cap somewhere else for the rest.
MAX_METADATA_INTEGER = 10**12

# A product external identifier and a variant SKU. No dot and no bracket, because both appear
# inside a source field address and a compiler target and both grammars use those characters.
IDENTIFIER_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$"

# A metadata key and a policy name. Both become the last segment of a source field address.
NAME_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$"

Identifier = Annotated[str, Field(pattern=IDENTIFIER_PATTERN)]
MetadataValue = str | int | bool


class SourceVariantInput(BaseModel):
    """One purchasable variant as the merchant describes it, not as the runtime holds it."""

    model_config = ConfigDict(extra="forbid", strict=True)

    sku: Identifier
    label: str | None = Field(default=None, max_length=MAX_LABEL_LENGTH)
    price_amount_minor: int = Field(ge=0, le=MAX_PRICE_AMOUNT_MINOR)
    currency: str = Field(pattern=CURRENCY_PATTERN)
    inventory_quantity: int = Field(ge=0, le=MAX_INVENTORY_QUANTITY)
    merchant_metadata: dict[str, MetadataValue] = Field(default_factory=dict)

    @field_validator("merchant_metadata")
    @classmethod
    def bounded_metadata(cls, value: dict[str, MetadataValue]) -> dict[str, MetadataValue]:
        return _bounded_map(value, MAX_METADATA_ENTRIES, MAX_METADATA_VALUE_LENGTH, "metadata")

    def domain(self) -> SourceVariant:
        return SourceVariant(
            sku=self.sku,
            label=self.label,
            price_amount_minor=self.price_amount_minor,
            currency=self.currency,
            inventory_quantity=self.inventory_quantity,
            merchant_metadata=dict(self.merchant_metadata),
        )


class SourceProductInput(BaseModel):
    """One product as the merchant describes it."""

    model_config = ConfigDict(extra="forbid", strict=True)

    external_id: Identifier
    title: str = Field(min_length=1, max_length=MAX_TITLE_LENGTH)
    description: str | None = Field(default=None, max_length=MAX_DESCRIPTION_LENGTH)
    category: str | None = Field(default=None, max_length=MAX_CATEGORY_LENGTH)
    variants: list[SourceVariantInput] = Field(min_length=1, max_length=MAX_VARIANTS_PER_PRODUCT)
    merchant_metadata: dict[str, MetadataValue] = Field(default_factory=dict)

    @field_validator("merchant_metadata")
    @classmethod
    def bounded_metadata(cls, value: dict[str, MetadataValue]) -> dict[str, MetadataValue]:
        return _bounded_map(value, MAX_METADATA_ENTRIES, MAX_METADATA_VALUE_LENGTH, "metadata")

    def domain(self) -> SourceProduct:
        return SourceProduct(
            external_id=self.external_id,
            title=self.title,
            description=self.description,
            category=self.category,
            variants=tuple(variant.domain() for variant in self.variants),
            merchant_metadata=dict(self.merchant_metadata),
        )


class SourceDocumentInput(BaseModel):
    """The whole of what a merchant may say about their source evidence.

    There is no key, no version and no merchant here on purpose. Which merchant this is comes
    from the credential that authenticated the request; which source line it continues and which
    version it becomes are resolved from what that merchant already holds. A browser that could
    name any of the three could write into another merchant's source history or overwrite a
    version an existing compiler run and representation were derived from.
    """

    model_config = ConfigDict(extra="forbid", strict=True)

    products: list[SourceProductInput] = Field(min_length=1, max_length=MAX_PRODUCTS)
    policy_text: dict[str, str] = Field(default_factory=dict)

    @field_validator("policy_text")
    @classmethod
    def bounded_policy(cls, value: dict[str, str]) -> dict[str, str]:
        bounded = _bounded_map(value, MAX_POLICY_ENTRIES, MAX_POLICY_BODY_LENGTH, "policy text")
        for name, body in bounded.items():
            if not body.strip():
                raise ValueError(f"policy text {name} must not be blank")
        return bounded

    @field_validator("products")
    @classmethod
    def bounded_variants(cls, value: list[SourceProductInput]) -> list[SourceProductInput]:
        total = sum(len(product.variants) for product in value)
        if total > MAX_TOTAL_VARIANTS:
            raise ValueError(
                f"a source document may describe at most {MAX_TOTAL_VARIANTS} variants"
            )
        return value

    def evidence(self) -> dict[str, Any]:
        """What this document says, without the identity the server has not supplied yet.

        The same shape a stored payload carries under `products` and `policy_text`, so a
        submitted document and a persisted snapshot can be compared before either has a version.
        """
        return {
            "products": [product.domain().payload() for product in self.products],
            "policy_text": dict(self.policy_text),
        }

    def definition(self, *, key: str, version: int, merchant_slug: str) -> MerchantSourceDefinition:
        """The canonical source document, with its identity supplied by the server."""
        return MerchantSourceDefinition(
            key=key,
            version=version,
            merchant_slug=merchant_slug,
            products=tuple(product.domain() for product in self.products),
            policy_text=dict(self.policy_text),
        )


class SourceSubmissionRequest(SourceDocumentInput):
    """One submission command: the evidence, and the key that makes a retry the same command."""

    request_key: str = Field(pattern=SUBMISSION_KEY_PATTERN)


class SourceFieldView(BaseModel):
    """One addressable source field, exactly as a compiler candidate would cite it."""

    model_config = ConfigDict(extra="forbid")

    field: str
    excerpt: str
    truncated: bool


class SourceCompilerRunView(BaseModel):
    """One compiler run over this snapshot, as much of it as a source page needs."""

    model_config = ConfigDict(extra="forbid")

    run_id: uuid.UUID
    status: str
    configuration_digest: str
    created_at: datetime
    completed_at: datetime | None
    error_code: str | None
    review_required_count: int
    reviewed_count: int
    published_representation_id: uuid.UUID | None


class SourceSnapshotSummaryView(BaseModel):
    """One snapshot as a history row: identity and size, never its content.

    A list page renders many of these, and rendering every historical source document to draw a
    table would make the page grow with the merchant's whole history.
    """

    model_config = ConfigDict(extra="forbid")

    source_snapshot_id: uuid.UUID
    source_label: str
    source_key: str
    source_version: int
    content_hash: str
    created_at: datetime
    origin: str
    product_count: int
    variant_count: int
    policy_count: int
    compiler_run_count: int
    published_representation_count: int
    is_current: bool


class SourceOverviewView(BaseModel):
    """This merchant's source history, newest first, and which snapshot is current."""

    model_config = ConfigDict(extra="forbid")

    current_source_snapshot_id: uuid.UUID | None
    snapshots: list[SourceSnapshotSummaryView]


class SourceSnapshotView(BaseModel):
    """One snapshot: what it is, what it says, and what has been compiled from it.

    `document` is the submittable half of the stored payload, so the console can show a merchant
    what they last supplied and let them edit it into newer evidence. The key, the version and
    the merchant are not in it, because they are not the merchant's to set.

    `compilable` is false once every compiler configuration this build has has already been run
    against this snapshot. Deterministic compilation of the same source under the same
    configuration produces the same candidates, so a second run would be the same run, and
    `existing_run_id` names the one that already exists.
    """

    model_config = ConfigDict(extra="forbid")

    summary: SourceSnapshotSummaryView
    document: dict[str, Any]
    fields: list[SourceFieldView]
    compiler_runs: list[SourceCompilerRunView]
    compilable: bool
    existing_run_id: uuid.UUID | None


class SourceSubmissionView(BaseModel):
    """What one submission command did.

    `created_snapshot` is false when the evidence submitted was identical to the merchant's
    current snapshot. Nothing was written and the answer names the snapshot that already carries
    it, which is a different fact from a new snapshot and reads as one.
    """

    model_config = ConfigDict(extra="forbid")

    submission_id: uuid.UUID
    request_key: str
    created_snapshot: bool
    snapshot: SourceSnapshotSummaryView


def _bounded_map(value: dict[str, Any], entries: int, body: int, name: str) -> dict[str, Any]:
    """Refuse a map that is too large, badly named, or carries a string that is too long."""
    if len(value) > entries:
        raise ValueError(f"a source document may carry at most {entries} {name} entries")
    for key, item in value.items():
        if re.fullmatch(NAME_PATTERN, key) is None:
            raise ValueError(f"{name} name {key!r} is not a valid identifier")
        if isinstance(item, str) and len(item) > body:
            raise ValueError(f"{name} {key} is longer than {body} characters")
        if (
            isinstance(item, int)
            and not isinstance(item, bool)
            and abs(item) > MAX_METADATA_INTEGER
        ):
            raise ValueError(f"{name} {key} is outside the supported range")
    return value
