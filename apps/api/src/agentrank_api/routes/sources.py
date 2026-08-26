"""Authenticated merchant commands and reads for merchant source evidence.

Two reads and one command. The reads answer what this merchant has supplied and what one
snapshot says; the command supplies newer evidence. Compiling that evidence is deliberately not
here: `POST /api/v1/compiler/runs` is its own command, because a snapshot is what the merchant
says and a compiler run is a reading of it, and one endpoint producing both would leave a
merchant unable to say which of the two they meant.

What a browser may say is a document and a request key. It may not say which merchant, which
source line or which version: all three are resolved from the credential that authenticated the
request and from what that merchant already holds, and none of the three is a field on any
schema here.

`OperatorDep` rather than `MerchantDep`, so a credential the benchmark runner minted for a run
is refused. Source evidence is the compiler's only input, and a buyer that could write it could
write the input to the representation it is being measured against.

Only one translation happens in this module. `MerchantSourceDefinition` raises `ValueError` for
a document that is well typed and still not a source document, such as one that reuses a SKU
across two products, and that is a 422 because the request itself is wrong. Everything else the
domain raises already has a response: `NotFoundError` is a 404, including for another merchant's
identifier, and the size bound is answered by `agentrank_api.limits` before the body is read.
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Query, status

from agentrank_api.dependencies import OperatorDep, SessionDep
from agentrank_api.errors import invalid_request
from agentrank_api.representation.intake import MerchantSourceIntakeService
from agentrank_api.representation.schemas import (
    SourceOverviewView,
    SourceSnapshotView,
    SourceSubmissionRequest,
    SourceSubmissionView,
)

router = APIRouter(prefix="/api/v1/sources", tags=["sources"])


@router.get("")
async def list_sources(
    session: SessionDep,
    merchant: OperatorDep,
    limit: Annotated[int, Query(ge=1, le=50)] = 20,
) -> SourceOverviewView:
    """This merchant's source snapshots, newest first, as identity and size only."""
    return await MerchantSourceIntakeService(session).overview(merchant.merchant_id, limit=limit)


@router.get("/{source_snapshot_id}")
async def read_source(
    source_snapshot_id: uuid.UUID, session: SessionDep, merchant: OperatorDep
) -> SourceSnapshotView:
    """One snapshot, what it says, and every compiler run that has read it.

    A foreign identifier is indistinguishable from an unknown one, which is the same 404 every
    other merchant-scoped read in this API answers with.
    """
    return await MerchantSourceIntakeService(session).snapshot_view(
        merchant.merchant_id, source_snapshot_id
    )


@router.post("", status_code=status.HTTP_201_CREATED)
async def submit_source(
    request: SourceSubmissionRequest, session: SessionDep, merchant: OperatorDep
) -> SourceSubmissionView:
    """Supply newer source evidence, or answer with what this request key already produced.

    201 whether or not a snapshot was written, and that is deliberate rather than sloppy. A
    retry after a lost response has to be able to learn what happened, and answering 409 to a
    caller repeating its own request would say something went wrong when nothing did. What the
    command actually did is `created_snapshot`, which is false when the evidence submitted was
    identical to the merchant's current snapshot and no new one was needed.
    """
    service = MerchantSourceIntakeService(session)
    try:
        outcome = await service.submit(
            merchant.merchant_id,
            request_key=request.request_key,
            document=request,
            base_source_snapshot_id=request.base_source_snapshot_id,
        )
    except ValueError as error:
        raise invalid_request(error) from error
    return await service.submission_view(outcome)
