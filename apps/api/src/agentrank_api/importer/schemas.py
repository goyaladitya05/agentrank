"""What a merchant may say when importing their own pages, and what they are told back.

The request half is deliberately tiny. An import command is a list of URLs and what each one is,
and there is no field for a depth, a page budget, a timeout, a user agent, a header, a cookie, a
selector or a rule. Every one of those is server authoritative, stated in
`agentrank_api.importer.network` and `agentrank_api.importer.service`, and a browser cannot raise
one. That is what keeps this a merchant import rather than a crawler with a settings page.

Two things a browser may not say, and their absence here is the mechanism rather than a
convention. It may not say which merchant: that comes from the credential that authenticated the
request. And it may not say what the pages contain: the draft is produced by fetching them.

The response half is evidence first. Every product names the page and the method behind it, every
omission names the reason it is not a product, and the numbers a merchant reads are counts of
things rather than a score. Nothing in here is called a suggestion, an optimization or a
discovery.
"""

import uuid
from datetime import datetime
from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from agentrank_api.importer.draft import (
    AvailabilityEvidence,
    DraftProduct,
    ExtractionMethod,
    PageKind,
    SourceDraft,
)
from agentrank_api.importer.models import ImportState, MerchantSourceImport
from agentrank_api.importer.service import (
    MAX_IMPORT_PAGES,
    MAX_IMPORT_URL_LENGTH,
    MAX_STOCK_LEVEL,
    ImportBlocker,
    ImportSummary,
    RequestedPage,
    blockers_for,
)
from agentrank_api.representation.models import SUBMISSION_KEY_PATTERN
from agentrank_api.representation.schemas import MAX_POLICY_ENTRIES, NAME_PATTERN

# The whole import command body. Twelve URLs of four hundred characters plus their names is under
# six kilobytes; sixteen is generous and is still nowhere near a document worth parsing to refuse.
MAX_IMPORT_REQUEST_BYTES = 16 * 1024

# A URL list, a kind and a name. Six is what the shape needs and is checked before the body is
# parsed, so a deeply nested body is refused rather than walked.
MAX_IMPORT_REQUEST_DEPTH = 6

PolicyName = Annotated[str, Field(pattern=NAME_PATTERN)]

# The one field these strict models relax, because strict mode reads an enum as "an instance of
# this enum class" and a JSON body cannot carry one. Everything strict mode is here for still
# holds: `"12"` does not become `12`, `1` does not become `True`, and a value that is not one of
# the two members is refused by name rather than coerced into one.
PageKindField = Annotated[PageKind, Field(strict=False)]


class ImportPageRequest(BaseModel):
    """One public URL and what the merchant says it is.

    The kind is stated rather than detected. Deciding from markup whether a page is a product page
    is the first heuristic of a crawler, it is wrong on any storefront it was not written against,
    and it is unnecessary: the merchant knows which page is their returns policy.
    """

    model_config = ConfigDict(extra="forbid", strict=True)

    url: str = Field(min_length=1, max_length=MAX_IMPORT_URL_LENGTH)
    kind: PageKindField
    name: PolicyName | None = None

    @model_validator(mode="after")
    def name_matches_kind(self) -> Self:
        """A policy is named and a product is not.

        The name becomes a key in the source document's `policy_text`, so it is the merchant's to
        choose and has to be one. A product page carrying a name would be a field that looked like
        it did something and did not.
        """
        if self.kind is PageKind.POLICY and self.name is None:
            raise ValueError("a policy page must be given a name, such as returns or warranty")
        if self.kind is PageKind.PRODUCT and self.name is not None:
            raise ValueError("a product page is not named; the name is taken from the page")
        return self


class SourceImportRequest(BaseModel):
    """One import command: the pages, and the key that makes a retry the same command."""

    model_config = ConfigDict(extra="forbid", strict=True)

    request_key: str = Field(pattern=SUBMISSION_KEY_PATTERN)
    pages: list[ImportPageRequest] = Field(min_length=1, max_length=MAX_IMPORT_PAGES)

    @field_validator("pages")
    @classmethod
    def distinct_policies(cls, value: list[ImportPageRequest]) -> list[ImportPageRequest]:
        names = [page.name for page in value if page.name is not None]
        if len(names) != len(set(names)):
            raise ValueError("two policy pages in one import may not share a name")
        if len(names) > MAX_POLICY_ENTRIES:
            raise ValueError(f"an import may name at most {MAX_POLICY_ENTRIES} policy pages")
        return value

    def requested(self) -> list[RequestedPage]:
        return [RequestedPage(url=page.url, kind=page.kind, name=page.name) for page in self.pages]


class ConfirmImportRequest(BaseModel):
    """The merchant's decision to turn one inspected import into source history.

    `stock_level` is the one number in this workflow that is not evidence, and it is here rather
    than in the import command because the merchant states it after seeing what was found. It is
    the stock the isolated evaluation world will hold for every variant whose page said what is
    available and not how much of it. It is not a claim about the merchant's warehouse and it
    changes nothing in the commerce runtime.
    """

    model_config = ConfigDict(extra="forbid", strict=True)

    stock_level: int | None = Field(default=None, ge=0, le=MAX_STOCK_LEVEL)


class ImportPageView(BaseModel):
    """One requested URL and what it answered.

    Validated from the stored payload rather than rebuilt field by field, because
    `PageRecord.payload` writes exactly these names and a second mapping between the two would be
    a second thing to keep in step.
    """

    model_config = ConfigDict(extra="forbid")

    url: str
    kind: PageKind
    name: str | None
    retrieved: bool
    reason: str | None
    detail: str | None
    status_code: int | None
    byte_count: int
    content_hash: str | None
    final_url: str | None
    redirect_count: int
    retrieved_at: str | None


class ImportVariantView(BaseModel):
    """One extracted variant, with the availability the page published and no quantity."""

    model_config = ConfigDict(extra="forbid")

    sku: str
    label: str | None
    price_amount_minor: int
    currency: str
    availability: AvailabilityEvidence
    availability_text: str | None


class ImportProductView(BaseModel):
    """One extracted product, naming the page and the method behind it."""

    model_config = ConfigDict(extra="forbid")

    external_id: str
    title: str
    description: str | None
    category: str | None
    source_url: str
    extraction: ExtractionMethod
    variants: list[ImportVariantView]

    @classmethod
    def of(cls, product: DraftProduct) -> Self:
        return cls(
            external_id=product.external_id,
            title=product.title,
            description=product.description,
            category=product.category,
            source_url=product.source_url,
            extraction=product.extraction,
            variants=[
                ImportVariantView(
                    sku=variant.sku,
                    label=variant.label,
                    price_amount_minor=variant.price_amount_minor,
                    currency=variant.currency,
                    availability=variant.availability,
                    availability_text=variant.availability_text,
                )
                for variant in product.variants
            ],
        )


class ImportPolicyView(BaseModel):
    """One extracted policy, as bounded merchant prose."""

    model_config = ConfigDict(extra="forbid")

    name: str
    body: str
    source_url: str
    truncated: bool


class ImportNoteView(BaseModel):
    """One omission or one finding, in the one shape a console renders both with."""

    model_config = ConfigDict(extra="forbid")

    source_url: str
    code: str
    detail: str
    subject: str | None


class ImportBlockerView(BaseModel):
    """One reason this import cannot become a source snapshot as it stands."""

    model_config = ConfigDict(extra="forbid")

    code: str
    detail: str

    @classmethod
    def of(cls, blocker: ImportBlocker) -> Self:
        return cls(code=blocker.code, detail=blocker.detail)


class SourceImportSummaryView(BaseModel):
    """One import as a history row: what it was aimed at and how much it found.

    Counts rather than content, so a history page does not render every merchant's every imported
    product to draw a table.
    """

    model_config = ConfigDict(extra="forbid")

    import_id: uuid.UUID
    origin: str
    state: ImportState
    failure_reason: str | None
    created_at: datetime
    page_count: int
    retrieved_count: int
    product_count: int
    variant_count: int
    policy_count: int
    omission_count: int
    source_snapshot_id: uuid.UUID | None
    confirmed_at: datetime | None

    @classmethod
    def of(cls, summary: ImportSummary) -> Self:
        return cls(
            import_id=summary.import_id,
            origin=summary.origin,
            state=summary.state,
            failure_reason=summary.failure_reason,
            created_at=summary.created_at,
            page_count=summary.page_count,
            retrieved_count=summary.retrieved_count,
            product_count=summary.product_count,
            variant_count=summary.variant_count,
            policy_count=summary.policy_count,
            omission_count=summary.omission_count,
            source_snapshot_id=summary.source_snapshot_id,
            confirmed_at=summary.confirmed_at,
        )


class SourceImportView(BaseModel):
    """One import in full: every page, everything extracted, and everything left out.

    `confirmable` is the one derived answer, and it is derived from the same function the confirm
    command checks, so a console that renders a disabled button and a caller that posts anyway are
    refused for the same reason.
    """

    model_config = ConfigDict(extra="forbid")

    summary: SourceImportSummaryView
    pages: list[ImportPageView]
    products: list[ImportProductView]
    policies: list[ImportPolicyView]
    omissions: list[ImportNoteView]
    findings: list[ImportNoteView]
    blockers: list[ImportBlockerView]
    stock_level_required: bool
    stock_level: int | None
    confirmable: bool
    max_stock_level: int = MAX_STOCK_LEVEL

    @classmethod
    def of(cls, record: MerchantSourceImport) -> Self:
        draft = SourceDraft.of(record.draft)
        # Blockers are computed as though the merchant had already stated a stock level, because
        # this read is what tells them whether stating one is all that is left. The number itself
        # is checked again by the confirm command, which is where it actually arrives.
        assumed = 0 if draft.stock_level_required else None
        blockers = blockers_for(record, draft, stock_level=assumed)
        return cls(
            summary=SourceImportSummaryView.of(ImportSummary.of(record)),
            pages=[ImportPageView.model_validate(page) for page in record.pages],
            products=[ImportProductView.of(product) for product in draft.products],
            policies=[
                ImportPolicyView(
                    name=policy.name,
                    body=policy.body,
                    source_url=policy.source_url,
                    truncated=policy.truncated,
                )
                for policy in draft.policies
            ],
            omissions=[
                ImportNoteView(
                    source_url=item.source_url,
                    code=item.reason,
                    detail=item.detail,
                    subject=item.subject,
                )
                for item in draft.omissions
            ],
            findings=[
                ImportNoteView(
                    source_url=item.source_url,
                    code=item.code,
                    detail=item.detail,
                    subject=item.subject,
                )
                for item in draft.findings
            ],
            blockers=[ImportBlockerView.of(blocker) for blocker in blockers],
            stock_level_required=draft.stock_level_required,
            stock_level=record.stock_level,
            confirmable=not blockers and record.confirmed_at is None,
        )


class ImportConfirmationView(BaseModel):
    """What confirming one import did.

    `created_snapshot` is false when the imported document was identical to the merchant's current
    snapshot, which is what a re-import of an unchanged storefront produces. Nothing was written
    and the answer names the snapshot that already carries it, because that is a different fact
    from a new snapshot and reads as one.
    """

    model_config = ConfigDict(extra="forbid")

    import_id: uuid.UUID
    already_confirmed: bool
    created_snapshot: bool
    source_snapshot_id: uuid.UUID
    source_label: str
