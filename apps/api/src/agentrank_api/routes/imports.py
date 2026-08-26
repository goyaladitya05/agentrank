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

What a browser may say is a list of URLs, what each one is, a request key, and, at confirmation,
the stock level the evaluation world should hold. It may not say which merchant, which source
line, which version, how many pages, how large a page may be or how long a fetch may take. The
first three are resolved from the credential and from what the merchant already holds; the rest
are constants this repository states.

Two translations happen here and nowhere else. `RefusedTargetError` is a 422 because the request
named something AgentRank will not fetch, which is a fact about the request. Everything else the
domain raises already has a response: `NotFoundError` is a 404 including for another merchant's
import, `ConflictError` is a 409 for a confirmation the draft refuses, and the body size bound is
answered by `agentrank_api.limits` before the body is read.
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, status

from agentrank_api.dependencies import OperatorDep, SessionDep, SettingsDep
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
    records = await MerchantSourceImportService(session).history(merchant.merchant_id, limit=limit)
    return [SourceImportSummaryView.of(record) for record in records]


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
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=refused.detail
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
    outcome = await MerchantSourceImportService(session).confirm(
        merchant.merchant_id, import_id, stock_level=request.stock_level
    )
    return ImportConfirmationView(
        import_id=outcome.record.id,
        already_confirmed=outcome.already_confirmed,
        created_snapshot=outcome.submission.submission.created_snapshot,
        source_snapshot_id=outcome.submission.snapshot.id,
        source_label=outcome.submission.snapshot.label,
    )
