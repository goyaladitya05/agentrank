"""Authenticated merchant commands and reads for importing a merchant's own public pages.

Two reads and two commands. The reads answer what this merchant has imported and what one import
found; the commands run one and confirm one. Confirming is separate from running for the same
reason compiling is separate from submitting a source: an import is a retrieval and a
confirmation is a decision, and one endpoint producing both would leave a merchant unable to say
which of the two they meant, on the one workflow where the difference is "somebody else's website
was fetched" versus "my source history changed".

This is the one endpoint in AgentRank that connects to an address a request body chose, which
makes it the one endpoint where server side request forgery is reachable. The whole of that
defence is `agentrank_api.importer.network` and none of it is here: this route validates nothing
about a URL, because a check written beside a route is a check the operator command line or a
future caller would not have.

`OperatorDep` rather than `MerchantDep`, exactly as the source, workspace and evaluation routes
use, so a credential the benchmark runner minted for a run is refused. A buyer that could run an
import could write the source its own measurement is derived from, and could additionally aim
this application's outbound requests.

What a browser may say is a list of URLs, what each one is, and a request key. It may not say
which merchant, which source line, which version, how many pages, how large a page may be or how
long a fetch may take, and it no longer says anything about stock: a source variant holds the
availability state a storefront publishes, so a confirmation states nothing at all. The first
three are resolved from the credential and from what the merchant already holds; the rest are
constants this repository states.

Two translations happen here and nowhere else. `RefusedTargetError` is a 422 because the request
named something AgentRank will not fetch, which is a fact about the request. A `ValueError` from
the source schema or the source domain is a 422 for the same reason a submitted document's is,
and it goes through `invalid_request` rather than into a bare `HTTPException`, because
`pydantic.ValidationError` is a `ValueError` and rendering one directly would put the caller's
own unbounded input value in the response. Everything else the domain raises already has a
response: `NotFoundError` is a 404 including for another merchant's import, and `ConflictError`
is a 409 for a confirmation the draft refuses.

The body size bound applies to the import command, which is the body a caller composes freely.
The confirmation body is empty and is not separately bounded, in common with every other route in
this application that takes a small fixed shape.
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Query, status

from agentrank_api.dependencies import OperatorDep, SessionDep, SettingsDep
from agentrank_api.errors import InvalidField, InvalidRequestError, invalid_request
from agentrank_api.importer.network import RefusedTargetError
from agentrank_api.importer.schemas import (
    ConfirmImportRequest,
    ImportConfirmationView,
    SourceImportRequest,
    SourceImportSummaryView,
    SourceImportView,
)
from agentrank_api.importer.service import MerchantSourceImportService

router = APIRouter(prefix="/api/v1/sources/imports", tags=["sources"])


@router.get("")
async def list_imports(
    session: SessionDep,
    merchant: OperatorDep,
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
) -> list[SourceImportSummaryView]:
    """This merchant's imports, newest first, as identity and counts only."""
    summaries = await MerchantSourceImportService(session).history(
        merchant.merchant_id, limit=limit
    )
    return [SourceImportSummaryView.of(summary) for summary in summaries]


@router.get("/{import_id}")
async def read_import(
    import_id: uuid.UUID, session: SessionDep, merchant: OperatorDep
) -> SourceImportView:
    """One import: every page it fetched, everything it extracted, and everything it left out.

    A foreign identifier is indistinguishable from an unknown one, which is the same 404 every
    other merchant-scoped read in this API answers with.
    """
    record = await MerchantSourceImportService(session).read(merchant.merchant_id, import_id)
    return SourceImportView.of(record)


@router.post("", status_code=status.HTTP_201_CREATED)
async def run_import(
    request: SourceImportRequest,
    session: SessionDep,
    merchant: OperatorDep,
    settings: SettingsDep,
) -> SourceImportView:
    """Retrieve the pages this merchant named and read them, or answer with what this key did.

    201 whether or not this request did the fetching. A retry after a lost response has to be able
    to learn what happened, and answering 409 to a caller repeating its own command would say
    something went wrong when nothing did.

    Nothing is created in AgentRank by this beyond the record of what was found. No source
    snapshot, no compiler run, no representation, no workspace and no model call.
    """
    service = MerchantSourceImportService(session, policy=settings.import_address_policy)
    try:
        record = await service.run(
            merchant.merchant_id,
            request_key=request.request_key,
            pages=request.requested(),
        )
    except RefusedTargetError as refused:
        # The URL goes in the field location, which is where a caller's own string already goes
        # and where it is already bounded, rather than into the sentence, which is this
        # repository's prose and stays that way.
        raise InvalidRequestError(
            refused.detail,
            fields=[
                InvalidField(
                    location=["body", "pages", refused.url or "", "url"],
                    message=refused.reason,
                )
            ],
        ) from refused
    return SourceImportView.of(record)


@router.post("/{import_id}/confirm", status_code=status.HTTP_201_CREATED)
async def confirm_import(
    import_id: uuid.UUID,
    request: ConfirmImportRequest,
    session: SessionDep,
    merchant: OperatorDep,
) -> ImportConfirmationView:
    """Turn one inspected import into an ordinary immutable source snapshot.

    The one command that lets an import reach source history, and it names the import the merchant
    was shown. There is no path on which running an import creates a snapshot.

    Confirming twice is one submission and answers with what the first one did, because the
    submission key is derived from the import rather than supplied.
    """
    try:
        outcome = await MerchantSourceImportService(session).confirm(
            merchant.merchant_id, import_id
        )
    except ValueError as error:
        # A draft that cannot become a source document. `blockers_for` catches the reasons a
        # merchant can act on and cannot catch every one: the source schema and the domain both
        # refuse documents for reasons this workflow does not model, and either would otherwise
        # reach a caller as a 500. Translated here rather than swallowed, and by the same rule the
        # source submission route uses, so a caller reads what was wrong with the request.
        raise invalid_request(error) from error
    return ImportConfirmationView(
        import_id=outcome.record.id,
        already_confirmed=outcome.already_confirmed,
        created_snapshot=outcome.submission.submission.created_snapshot,
        source_snapshot_id=outcome.submission.snapshot.id,
        source_label=outcome.submission.snapshot.label,
    )
