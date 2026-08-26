"""Running one merchant import, and turning a confirmed one into ordinary source history.

The whole workflow is four steps and the boundaries between them are the point:

```text
retrieve    bounded public GET of the URLs the merchant named, and nothing else
extract     deterministic reading of what came back, with omissions stated
inspect     the merchant reads the draft, which is not source history yet
confirm     the ordinary source intake writes an ordinary immutable snapshot
```

Nothing skips a step. An import never creates a snapshot, never compiles, never publishes a
representation, never builds a workspace and never calls a model. Confirming produces exactly
what a merchant typing the same document into the source editor would produce, through the same
service, validated by the same schema, deduplicated by the same content identity.

**Why the import is synchronous.** It fetches at most a dozen pages, sequentially, each bounded to
fifteen seconds, under an overall deadline well inside any sensible request lifetime. A queue
would add a durable job table, a worker, a polling surface and a set of partial states, all to
avoid a wait the merchant is already choosing to make by pressing a button. The benchmark
dispatcher exists for work that spends money and takes minutes; this is neither.

**Why the pages are fetched one at a time.** Determinism, and courtesy. Sequential fetching means
the byte budget is spent in the order the merchant listed their URLs rather than in whatever order
sockets happened to complete, so two imports of the same list stop at the same page. It also means
AgentRank opens one connection to somebody else's storefront rather than a dozen.

**Where the merchant's identity comes from.** The credential, as everywhere else. Nothing in a
request body names a merchant, an import belongs to the merchant that ran it, and another
merchant's import identifier is indistinguishable from one that does not exist.
"""

import asyncio
import logging
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import ColumnElement, func, literal_column, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from agentrank_api.benchmark.identity import canonical_json
from agentrank_api.commerce.models import Merchant
from agentrank_api.commerce.repository import MerchantRepository
from agentrank_api.errors import ConflictError, NotFoundError
from agentrank_api.importer.draft import (
    DraftPolicy,
    DraftProduct,
    Finding,
    Omission,
    PageKind,
    SourceDraft,
    canonical_document,
)
from agentrank_api.importer.extraction import Identifiers, extract_policy, extract_product
from agentrank_api.importer.models import ImportState, MerchantSourceImport
from agentrank_api.importer.network import (
    AddressPolicy,
    FetchLimits,
    ImportTarget,
    MerchantPageFetcher,
    RefusedTargetError,
    RetrievalFailure,
    RetrievalLogLine,
    RetrievedDocument,
    validate_target,
)
from agentrank_api.importer.reading import read_page
from agentrank_api.representation.intake import MerchantSourceIntakeService, SubmissionOutcome
from agentrank_api.representation.models import SourceOrigin
from agentrank_api.representation.schemas import (
    MAX_PRODUCTS,
    MAX_TOTAL_VARIANTS,
    RESERVED_KEY_PREFIX,
    SourceDocumentInput,
)

logger = logging.getLogger(__name__)

IMPORT_RESOURCE = "merchant_source_import"

# Counts computed by PostgreSQL rather than by loading every historical draft into Python, for the
# reason argued in `MerchantSourceImportService.history`.
_PAGE_COUNT: ColumnElement[int] = literal_column("jsonb_array_length(merchant_source_import.pages)")
_RETRIEVED_COUNT: ColumnElement[int] = literal_column(
    "(SELECT count(*) FROM jsonb_array_elements(merchant_source_import.pages) AS entry"
    " WHERE (entry ->> 'retrieved')::boolean)"
)
_PRODUCT_COUNT: ColumnElement[int] = literal_column(
    "jsonb_array_length(coalesce(merchant_source_import.draft -> 'products', '[]'::jsonb))"
)
_VARIANT_COUNT: ColumnElement[int] = literal_column(
    "(SELECT coalesce(sum(jsonb_array_length(entry -> 'variants')), 0)"
    " FROM jsonb_array_elements(coalesce(merchant_source_import.draft -> 'products',"
    " '[]'::jsonb)) AS entry)"
)
_POLICY_COUNT: ColumnElement[int] = literal_column(
    "jsonb_array_length(coalesce(merchant_source_import.draft -> 'policies', '[]'::jsonb))"
)
_OMISSION_COUNT: ColumnElement[int] = literal_column(
    "jsonb_array_length(coalesce(merchant_source_import.draft -> 'omissions', '[]'::jsonb))"
)

# How many URLs one import may name. A dozen covers a storefront's product pages plus its returns,
# warranty and shipping pages, which is what this is for. It is not a number to raise until
# somebody has a merchant it is actually too small for: every page is somebody else's server being
# asked for something, and the bound is what keeps that a request rather than a crawl.
MAX_IMPORT_PAGES = 12

# The whole import, across every page and every redirect. Separate from the per page bound because
# twelve pages each just inside the per page bound is twenty four megabytes of markup to parse
# inside one request.
MAX_IMPORT_TOTAL_BYTES = 8 * 1024 * 1024

# The overall deadline. Sequential pages at fifteen seconds each could otherwise reach three
# minutes, which is not a request. An import that reaches this is recorded as failed with the
# reason rather than answered with whatever it happened to have.
IMPORT_DEADLINE_SECONDS = 60.0

# A merchant supplied URL. Shorter than what the network boundary will fetch, because this one is
# stored inside a source document's metadata, where a value is bounded at five hundred characters.
MAX_IMPORT_URL_LENGTH = 400

# The stock level a merchant may state for the evaluation world. Bounded by what a source document
# accepts. It is not a claim about their warehouse. See `draft.canonical_document`.
MAX_STOCK_LEVEL = 10_000


@dataclass(frozen=True, slots=True)
class RequestedPage:
    """One URL a merchant named, and what they said it is."""

    url: str
    kind: PageKind
    name: str | None = None


@dataclass(frozen=True, slots=True)
class PageRecord:
    """What one requested URL actually produced, as the import record keeps it."""

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

    def payload(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "kind": self.kind.value,
            "name": self.name,
            "retrieved": self.retrieved,
            "reason": self.reason,
            "detail": self.detail,
            "status_code": self.status_code,
            "byte_count": self.byte_count,
            "content_hash": self.content_hash,
            "final_url": self.final_url,
            "redirect_count": self.redirect_count,
            "retrieved_at": self.retrieved_at,
        }


@dataclass(frozen=True, slots=True)
class ImportSummary:
    """One import as identity and size, with no part of its draft in it."""

    import_id: uuid.UUID
    origin: str
    state: ImportState
    failure_reason: str | None
    created_at: datetime
    source_snapshot_id: uuid.UUID | None
    confirmed_at: datetime | None
    page_count: int
    retrieved_count: int
    product_count: int
    variant_count: int
    policy_count: int
    omission_count: int

    @classmethod
    def of(cls, record: MerchantSourceImport) -> ImportSummary:
        """The same summary for one already loaded record, so both paths agree on the numbers."""
        draft = SourceDraft.of(record.draft)
        return cls(
            import_id=record.id,
            origin=record.origin,
            state=record.state,
            failure_reason=record.failure_reason,
            created_at=record.created_at,
            source_snapshot_id=record.source_snapshot_id,
            confirmed_at=record.confirmed_at,
            page_count=len(record.pages),
            retrieved_count=sum(1 for page in record.pages if page.get("retrieved")),
            product_count=len(draft.products),
            variant_count=draft.variant_count,
            policy_count=len(draft.policies),
            omission_count=len(draft.omissions),
        )


@dataclass(frozen=True, slots=True)
class ImportBlocker:
    """One reason an import cannot become a source snapshot as it stands."""

    code: str
    detail: str


@dataclass(frozen=True, slots=True)
class ConfirmationOutcome:
    """What confirming one import did."""

    record: MerchantSourceImport
    submission: SubmissionOutcome
    already_confirmed: bool


class MerchantSourceImportService:
    """Retrieving a merchant's own public pages, and confirming what came out into source."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        policy: AddressPolicy | None = None,
        limits: FetchLimits | None = None,
        fetcher: MerchantPageFetcher | None = None,
    ) -> None:
        self._session = session
        self._policy = policy or AddressPolicy()
        self._limits = limits or FetchLimits()
        self._fetcher = fetcher

    async def run(
        self,
        merchant_id: uuid.UUID,
        *,
        request_key: str,
        pages: Sequence[RequestedPage],
    ) -> MerchantSourceImport:
        """Fetch and read one merchant's pages, and record what came of it.

        The request key is checked before anything is fetched, so pressing the import button
        twice fetches the merchant's storefront once. Two genuinely concurrent requests with one
        key can both get past that check, and the unique constraint settles them: the loser reads
        the winner's record rather than writing a second one. Fetching twice in that window is
        the cost of not holding a database lock across somebody else's network.
        """
        await self._merchant(merchant_id)
        targets = self._targets(pages)
        settled = await self._by_request_key(merchant_id, request_key)
        if settled is not None:
            return _same_request(settled, targets, request_key)
        origin = targets[0][0].origin
        records, draft, failure = await self._retrieve(targets)
        record = MerchantSourceImport(
            merchant_id=merchant_id,
            request_key=request_key,
            origin=origin,
            state=ImportState.FAILED if failure else ImportState.COMPLETED,
            failure_reason=failure,
            pages=[page.payload() for page in records],
            draft=draft.payload(),
        )
        self._session.add(record)
        try:
            await self._session.commit()
        except IntegrityError:
            await self._session.rollback()
            duplicate = await self._by_request_key(merchant_id, request_key)
            if duplicate is not None:
                return _same_request(duplicate, targets, request_key)
            raise
        return record

    def _targets(self, pages: Sequence[RequestedPage]) -> list[tuple[ImportTarget, RequestedPage]]:
        """Every requested URL validated, and refused unless they are all one storefront.

        One origin because an import is a merchant importing their own store. A list that mixes
        hosts is either a mistake or somebody using an authenticated AgentRank endpoint to fetch
        a set of unrelated addresses, and neither is a thing to support. The first URL decides
        which origin the rest are checked against, so the refusal names the one that disagreed.
        """
        if not pages:
            raise RefusedTargetError("no_pages", "an import must name at least one page")
        if len(pages) > MAX_IMPORT_PAGES:
            raise RefusedTargetError(
                "too_many_pages", f"an import may name at most {MAX_IMPORT_PAGES} pages"
            )
        resolved: list[tuple[ImportTarget, RequestedPage]] = []
        origin: str | None = None
        seen: set[str] = set()
        for page in pages:
            if len(page.url) > MAX_IMPORT_URL_LENGTH:
                raise RefusedTargetError(
                    "url_too_long",
                    f"a merchant page URL may be at most {MAX_IMPORT_URL_LENGTH} characters",
                )
            target = validate_target(page.url, policy=self._policy)
            if len(target.text) > MAX_IMPORT_URL_LENGTH:
                # Checked again after normalization, not only on what was submitted. Percent
                # encoding a path expands it, so a URL inside the submitted bound can leave it,
                # and this is the string that is stored in a source document's merchant metadata
                # where five hundred characters is the limit.
                raise RefusedTargetError(
                    "url_too_long",
                    f"that URL is longer than {MAX_IMPORT_URL_LENGTH} characters once AgentRank"
                    " has normalized it",
                )
            if origin is None:
                origin = target.origin
            elif target.origin != origin:
                raise RefusedTargetError(
                    "several_origins",
                    "every page in one import must be on the same storefront origin",
                )
            if target.text in seen:
                raise RefusedTargetError(
                    "duplicate_page", "the same URL is named more than once in this import"
                )
            seen.add(target.text)
            resolved.append((target, page))
        return resolved

    async def _retrieve(
        self, targets: Sequence[tuple[ImportTarget, RequestedPage]]
    ) -> tuple[list[PageRecord], SourceDraft, str | None]:
        """Fetch every page in order and read each one, inside one overall deadline."""
        records: list[PageRecord] = []
        products: list[DraftProduct] = []
        policies: list[DraftPolicy] = []
        omissions: list[Omission] = []
        findings: list[Finding] = []
        identifiers = Identifiers()
        spent = 0
        failure: str | None = None

        fetcher = self._fetcher or MerchantPageFetcher(policy=self._policy, limits=self._limits)
        owns = self._fetcher is None
        try:
            async with asyncio.timeout(IMPORT_DEADLINE_SECONDS):
                for target, page in targets:
                    if spent >= MAX_IMPORT_TOTAL_BYTES:
                        records.append(_refused(target, page, "import_byte_budget"))
                        continue
                    outcome = await fetcher.fetch(target)
                    if isinstance(outcome, RetrievalFailure):
                        logger.info(
                            "merchant page import refused: %s",
                            RetrievalLogLine.of(target, status_code=outcome.status_code),
                        )
                        records.append(_failed(target, page, outcome))
                        omissions.append(
                            Omission(target.text, outcome.reason, outcome.detail, page.name)
                        )
                        continue
                    spent += outcome.byte_count
                    logger.info(
                        "merchant page imported: %s",
                        RetrievalLogLine.of(
                            target,
                            status_code=outcome.status_code,
                            byte_count=outcome.byte_count,
                        ),
                    )
                    records.append(_retrieved(target, page, outcome))
                    self._read(
                        target, page, outcome, identifiers, products, policies, omissions, findings
                    )
        except TimeoutError:
            failure = "deadline"
            for target, page in targets[len(records) :]:
                records.append(_refused(target, page, "import_deadline"))
        finally:
            if owns:
                await fetcher.aclose()

        products, policies, findings = _bounded(products, policies, findings)
        draft = SourceDraft(
            products=tuple(products),
            policies=tuple(policies),
            omissions=tuple(omissions),
            findings=tuple(findings),
        )
        return records, (SourceDraft() if failure else draft), failure

    def _read(
        self,
        target: ImportTarget,
        page: RequestedPage,
        document: RetrievedDocument,
        identifiers: Identifiers,
        products: list[DraftProduct],
        policies: list[DraftPolicy],
        omissions: list[Omission],
        findings: list[Finding],
    ) -> None:
        """Read one retrieved page as whatever the merchant said it is."""
        reading = read_page(document.text())
        if reading.truncated:
            findings.append(
                Finding(
                    target.text,
                    "page_truncated",
                    "this page is larger than AgentRank reads and was cut while being read",
                )
            )
        if page.kind is PageKind.PRODUCT:
            extracted = extract_product(reading, source_url=target.text, identifiers=identifiers)
            findings.extend(extracted.findings)
            if extracted.omission is not None:
                omissions.append(extracted.omission)
            if extracted.product is not None:
                duplicate = _duplicate_identity(extracted.product, products)
                if duplicate is None:
                    products.append(extracted.product)
                else:
                    omissions.append(duplicate)
            return
        name = page.name
        if name is None:  # pragma: no cover - the request schema requires one
            omissions.append(Omission(target.text, "policy_unnamed", "this policy has no name"))
            return
        extracted_policy = extract_policy(reading, source_url=target.text, name=name)
        findings.extend(extracted_policy.findings)
        if extracted_policy.omission is not None:
            omissions.append(extracted_policy.omission)
        if extracted_policy.policy is not None:
            policies.append(extracted_policy.policy)

    async def read(self, merchant_id: uuid.UUID, import_id: uuid.UUID) -> MerchantSourceImport:
        """One import this merchant ran. Somebody else's identifier is an unknown one."""
        found = (
            await self._session.execute(
                select(MerchantSourceImport).where(
                    MerchantSourceImport.id == import_id,
                    MerchantSourceImport.merchant_id == merchant_id,
                )
            )
        ).scalar_one_or_none()
        if found is None:
            raise NotFoundError(IMPORT_RESOURCE, str(import_id))
        return found

    async def history(self, merchant_id: uuid.UUID, *, limit: int = 10) -> list[ImportSummary]:
        """This merchant's imports as identity and size, newest first.

        The counts are computed by PostgreSQL rather than by loading every historical draft into
        Python. A history page renders a table of numbers, and a merchant with a long history and
        full drafts would otherwise pay to deserialize all of them to draw one. The same reason
        `MerchantSourceIntakeService` computes its snapshot summaries in SQL.
        """
        rows = await self._session.execute(
            select(
                MerchantSourceImport.id,
                MerchantSourceImport.origin,
                MerchantSourceImport.state,
                MerchantSourceImport.failure_reason,
                MerchantSourceImport.created_at,
                MerchantSourceImport.source_snapshot_id,
                MerchantSourceImport.confirmed_at,
                _PAGE_COUNT,
                _RETRIEVED_COUNT,
                _PRODUCT_COUNT,
                _VARIANT_COUNT,
                _POLICY_COUNT,
                _OMISSION_COUNT,
            )
            .where(MerchantSourceImport.merchant_id == merchant_id)
            .order_by(MerchantSourceImport.created_at.desc(), MerchantSourceImport.id.desc())
            .limit(limit)
        )
        return [
            ImportSummary(
                import_id=row[0],
                origin=row[1],
                state=row[2],
                failure_reason=row[3],
                created_at=row[4],
                source_snapshot_id=row[5],
                confirmed_at=row[6],
                page_count=int(row[7]),
                retrieved_count=int(row[8]),
                product_count=int(row[9]),
                variant_count=int(row[10]),
                policy_count=int(row[11]),
                omission_count=int(row[12]),
            )
            for row in rows.all()
        ]

    async def confirm(
        self, merchant_id: uuid.UUID, import_id: uuid.UUID, *, stock_level: int | None
    ) -> ConfirmationOutcome:
        """Turn one inspected import into an ordinary immutable source snapshot.

        Explicit by construction. There is no path from running an import to a snapshot that does
        not come through here, and nothing calls this except a merchant command naming an import
        they have been shown.

        The submission key is derived from the import rather than supplied, and this reads it back
        before doing anything else. That one read is what makes the whole command recoverable,
        because the two writes it performs cannot be one transaction: the intake commits the
        snapshot, and only then can this row be linked to it.

        ```text
        the submission exists   this import already produced a snapshot. Link it if the link is
                                missing, and answer with what that submission did
        it does not             check the blockers, submit, and link
        ```

        Three situations that all used to be wrong resolve to the first branch. A retry after a
        response nobody saw. A process that died between the two writes, including one whose retry
        states a different stock level, which used to be a permanent 409 with the snapshot orphaned
        from the import that produced it. And two simultaneous confirmations, where the loser used
        to write over a row the immutability trigger then refused, surfacing to a merchant as "the
        database is unavailable" on a command that had in fact succeeded.
        """
        record = await self.read(merchant_id, import_id)
        intake = MerchantSourceIntakeService(self._session)
        key = submission_key(record.id)
        draft = SourceDraft.of(record.draft)
        settled = await intake.by_request_key(merchant_id, key)
        if settled is not None:
            # The stock level is recorded only when it is the one the settled snapshot actually
            # carries, which is decided by rebuilding the document rather than assumed. Two
            # simultaneous confirmations name the same number and both are right; a retry after a
            # lost link may name a different one, and writing that against a snapshot built from
            # another figure would have the row state something the snapshot does not.
            record = await self._link(record, settled, _stated(draft, stock_level, settled))
            return ConfirmationOutcome(record, settled, already_confirmed=True)
        blockers = blockers_for(record, draft, stock_level=stock_level)
        if blockers:
            raise ConflictError(
                blockers[0].code,
                blockers[0].detail,
                resource=IMPORT_RESOURCE,
                identifier=str(import_id),
            )
        document = SourceDocumentInput.model_validate(
            canonical_document(draft, stock_level=stock_level)
        )
        submission = await intake.submit(
            merchant_id, request_key=key, document=document, origin=SourceOrigin.MERCHANT_IMPORT
        )
        record = await self._link(record, submission, stock_level)
        return ConfirmationOutcome(record, submission, already_confirmed=False)

    async def _link(
        self,
        record: MerchantSourceImport,
        submission: SubmissionOutcome,
        stock_level: int | None,
    ) -> MerchantSourceImport:
        """Record which snapshot this import became, exactly once, whoever gets there first.

        A conditional statement rather than an ORM write, and that is the point. The immutability
        trigger refuses any update to a row that is already confirmed, so a second writer using
        the ORM would raise a database error on a command that had succeeded. `WHERE confirmed_at
        IS NULL` means the second writer matches no row, the trigger never fires, and both callers
        end up reading the same settled state.
        """
        if record.confirmed_at is None:
            await self._session.execute(
                update(MerchantSourceImport)
                .where(
                    MerchantSourceImport.id == record.id,
                    MerchantSourceImport.merchant_id == record.merchant_id,
                    MerchantSourceImport.confirmed_at.is_(None),
                )
                .values(
                    source_snapshot_id=submission.snapshot.id,
                    stock_level=stock_level,
                    confirmed_at=func.now(),
                )
            )
            await self._session.commit()
        return await self.read(record.merchant_id, record.id)

    async def _by_request_key(
        self, merchant_id: uuid.UUID, request_key: str
    ) -> MerchantSourceImport | None:
        return (
            await self._session.execute(
                select(MerchantSourceImport).where(
                    MerchantSourceImport.merchant_id == merchant_id,
                    MerchantSourceImport.request_key == request_key,
                )
            )
        ).scalar_one_or_none()

    async def _merchant(self, merchant_id: uuid.UUID) -> Merchant:
        merchant = await MerchantRepository(self._session).get_by_id(merchant_id)
        if merchant is None:
            raise NotFoundError("merchant", str(merchant_id))
        return merchant


def _stated(draft: SourceDraft, stock_level: int | None, settled: SubmissionOutcome) -> int | None:
    """The stock level to record, which is this caller's only if it produced this snapshot."""
    try:
        rebuilt = canonical_document(draft, stock_level=stock_level)
    except ValueError:
        return None
    evidence = {
        "products": settled.snapshot.payload.get("products", []),
        "policy_text": settled.snapshot.payload.get("policy_text", {}),
    }
    return stock_level if canonical_json(rebuilt) == canonical_json(evidence) else None


def _same_request(
    settled: MerchantSourceImport,
    targets: Sequence[tuple[ImportTarget, RequestedPage]],
    request_key: str,
) -> MerchantSourceImport:
    """The import this key already produced, if this really is the same command.

    A key names a command, so a repeat naming the same pages is that command and answers with what
    it did. A repeat naming different pages is a different command wearing another one's name, and
    answering it with somebody else's result would tell a merchant their import of B succeeded
    while showing them the results for A. The same rule the source intake applies to a reused
    submission key, for the same reason.
    """
    requested = [
        {"url": target.text, "kind": page.kind.value, "name": page.name} for target, page in targets
    ]
    stored = [
        {"url": entry.get("url"), "kind": entry.get("kind"), "name": entry.get("name")}
        for entry in settled.pages
    ]
    if requested != stored:
        raise ConflictError(
            "import_request_key_reused",
            "this request key has already imported a different set of pages",
            resource=IMPORT_RESOURCE,
            identifier=request_key,
        )
    return settled


def _duplicate_identity(product: DraftProduct, accepted: Sequence[DraftProduct]) -> Omission | None:
    """Whether this product repeats an identity another page already published.

    A canonical URL and a variant URL for one product is an everyday storefront shape, and both
    pages publish the same SKU. A source document requires product identifiers unique among
    products and SKUs unique among variants, so importing both would produce a document that
    passes the request schema and is then refused by the domain, deeper down, with nothing the
    merchant could act on. Refusing the second page here says which page and why.
    """
    identities = {existing.external_id for existing in accepted}
    if product.external_id in identities:
        return Omission(
            product.source_url,
            "duplicate_product",
            "another page in this import already published this product",
            product.external_id,
        )
    skus = {variant.sku for existing in accepted for variant in existing.variants}
    shared = sorted(skus.intersection(variant.sku for variant in product.variants))
    if shared:
        return Omission(
            product.source_url,
            "duplicate_variant",
            "another page in this import already published this SKU",
            shared[0],
        )
    return None


def blockers_for(
    record: MerchantSourceImport, draft: SourceDraft, *, stock_level: int | None
) -> list[ImportBlocker]:
    """Everything that stands between one import and a source snapshot, in the order to fix it.

    Stated as a list a console renders rather than as an exception, because the merchant is meant
    to read these before pressing anything. The confirmation command checks the same list, so a
    caller that skipped the console is refused by the same rules rather than by a second set.
    """
    blockers: list[ImportBlocker] = []
    if record.state is ImportState.FAILED:
        blockers.append(
            ImportBlocker(
                "import_failed", "this import did not finish, so there is nothing to confirm"
            )
        )
        return blockers
    if not draft.products:
        blockers.append(
            ImportBlocker(
                "no_products",
                "no product could be imported from these pages, so there is no source to create",
            )
        )
    if len(draft.products) > MAX_PRODUCTS:  # pragma: no cover - bounded before persistence
        blockers.append(
            ImportBlocker(
                "too_many_products", "this import found more products than a source holds"
            )
        )
    if draft.variant_count > MAX_TOTAL_VARIANTS:  # pragma: no cover - bounded before persistence
        blockers.append(
            ImportBlocker(
                "too_many_variants", "this import found more variants than a source holds"
            )
        )
    if draft.stock_level_required and stock_level is None:
        blockers.append(
            ImportBlocker(
                "stock_level_required",
                "these pages say what is in stock and not how many, so the stock level the"
                " evaluation world should hold has to be stated",
            )
        )
    if stock_level is not None and not 0 <= stock_level <= MAX_STOCK_LEVEL:
        blockers.append(
            ImportBlocker(
                "stock_level_out_of_range",
                f"the evaluation stock level must be between 0 and {MAX_STOCK_LEVEL}",
            )
        )
    return blockers


def submission_key(import_id: uuid.UUID) -> str:
    """The one source submission key one import may ever produce.

    Under the prefix `SourceSubmissionRequest` refuses, so a merchant cannot claim this key with
    other evidence and make their own confirmation permanently refusable.
    """
    return f"{RESERVED_KEY_PREFIX}{import_id.hex}"


def _bounded(
    products: list[DraftProduct], policies: list[DraftPolicy], findings: list[Finding]
) -> tuple[list[DraftProduct], list[DraftPolicy], list[Finding]]:
    """Cut a draft to what a source document holds, saying so rather than failing later.

    Reached only by an import whose pages carry an implausible number of things, because the page
    count is already bounded at a dozen. It exists so that such an import produces a usable draft
    with a stated cut rather than a validation failure at confirmation time that names a limit the
    merchant never saw.
    """
    kept = products
    if len(kept) > MAX_PRODUCTS:
        findings.append(
            Finding(
                kept[MAX_PRODUCTS].source_url,
                "products_truncated",
                f"these pages publish {len(kept)} products and AgentRank imported the first"
                f" {MAX_PRODUCTS}",
            )
        )
        kept = kept[:MAX_PRODUCTS]
    total = 0
    within: list[DraftProduct] = []
    for product in kept:
        if total + len(product.variants) > MAX_TOTAL_VARIANTS:
            findings.append(
                Finding(
                    product.source_url,
                    "variants_truncated",
                    "these pages publish more variants than a source document holds and the"
                    " remaining products were not imported",
                )
            )
            break
        total += len(product.variants)
        within.append(product)
    return within, policies, findings


def _retrieved(
    target: ImportTarget, page: RequestedPage, document: RetrievedDocument
) -> PageRecord:
    return PageRecord(
        url=target.text,
        kind=page.kind,
        name=page.name,
        retrieved=True,
        reason=None,
        detail=None,
        status_code=document.status_code,
        byte_count=document.byte_count,
        content_hash=document.content_hash,
        final_url=document.final_url,
        redirect_count=len(document.redirects),
        retrieved_at=datetime.now(UTC).isoformat(),
    )


def _failed(target: ImportTarget, page: RequestedPage, outcome: RetrievalFailure) -> PageRecord:
    return PageRecord(
        url=target.text,
        kind=page.kind,
        name=page.name,
        retrieved=False,
        reason=outcome.reason,
        detail=outcome.detail,
        status_code=outcome.status_code,
        byte_count=0,
        content_hash=None,
        final_url=None,
        redirect_count=0,
        retrieved_at=datetime.now(UTC).isoformat(),
    )


_REFUSALS = {
    "import_byte_budget": "this import had already read as much as AgentRank will read",
    "import_deadline": "this import ran out of time before reaching this page",
}


def _refused(target: ImportTarget, page: RequestedPage, reason: str) -> PageRecord:
    return PageRecord(
        url=target.text,
        kind=page.kind,
        name=page.name,
        retrieved=False,
        reason=reason,
        detail=_REFUSALS[reason],
        status_code=None,
        byte_count=0,
        content_hash=None,
        final_url=None,
        redirect_count=0,
        retrieved_at=None,
    )
