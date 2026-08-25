"""FastAPI dependencies shared by routes.

Authentication lives here rather than in `agentrank_api.auth` on purpose. Bearer parsing,
status codes and OpenAPI security are HTTP concerns, and the package underneath knows nothing
about any of them: it takes a string and answers with a principal or with nothing. That split
is what lets the command line provision credentials without importing FastAPI, and what keeps
the one place a 401 is decided in the one layer that can answer with one.
"""

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agentrank_api.auth.principal import AuthenticatedMerchant
from agentrank_api.auth.service import MerchantCredentialService
from agentrank_api.benchmark.mutation import BenchmarkMutationGuard
from agentrank_api.config import RazorpayCredentials, Settings
from agentrank_api.errors import AuthenticationError
from agentrank_api.payments.provider import PaymentProvider
from agentrank_api.razorpay.client import RazorpayClient


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    """One session per request, closed when the request ends.

    The factory comes from application state rather than a module global so that a test
    can point an application at a different database by constructing it with different
    settings.
    """
    factory: async_sessionmaker[AsyncSession] = request.app.state.session_factory
    async with factory() as session:
        yield session


SessionDep = Annotated[AsyncSession, Depends(get_session)]


def get_payment_provider(request: Request) -> PaymentProvider:
    """The payment provider this application was built with.

    From application state rather than a module global, for the same reason the session factory
    is: a test points an application at a provider it can configure, and there is no way for a
    request to choose one. A caller cannot ask for a decline, a timeout or a lost response,
    because there is no parameter that reaches this and no field in any request schema that
    could carry one.

    One implementation exists and it is a deterministic fake. Nothing here calls anything
    external. See docs/integrations.md.
    """
    provider: PaymentProvider = request.app.state.payment_provider
    return provider


ProviderDep = Annotated[PaymentProvider, Depends(get_payment_provider)]


def get_razorpay_client(request: Request) -> RazorpayClient | None:
    """The Razorpay transport this application was built with, or None.

    None is an ordinary answer rather than a failure. An unconfigured integration is a
    deployment that has no Razorpay key pair, which every other part of this application works
    perfectly well without, and the service refuses by name so a caller learns what is missing.

    From application state for the same reason the payment provider is: a test points an
    application at a transport it can inspect, and no request can choose one.
    """
    client: RazorpayClient | None = request.app.state.razorpay_client
    return client


RazorpayDep = Annotated[RazorpayClient | None, Depends(get_razorpay_client)]


def get_razorpay_credentials(request: Request) -> RazorpayCredentials | None:
    """The Razorpay Test Mode credentials, or None when the integration is not configured.

    Separate from the transport because a response needs the public key id and the transport
    does not expose one. The secret half stays inside a `SecretStr` and is never read here.
    """
    settings: Settings = request.app.state.settings
    return settings.razorpay


RazorpayCredentialsDep = Annotated[RazorpayCredentials | None, Depends(get_razorpay_credentials)]


def get_settings(request: Request) -> Settings:
    """The configuration this application was built with.

    From application state rather than the process cache, for the same reason every other
    dependency here is: a test points an application at the settings it wants to exercise, and
    no request can choose them. Routes read declarative values from it, never a secret.
    """
    settings: Settings = request.app.state.settings
    return settings


SettingsDep = Annotated[Settings, Depends(get_settings)]


# `auto_error=False` because FastAPI's own refusal is the wrong one twice over. It answers 403
# for a missing header, which says "you may not" to a caller who has not said who they are, and
# its body is a bare `detail` string rather than this application's structured error shape. With
# the flag off this returns None for a missing header and for a header in any other scheme, and
# the one refusal below covers both.
#
# It is still an `HTTPBearer`, so FastAPI records it in the generated OpenAPI document as an
# HTTP bearer security scheme and marks every operation that depends on it. Writing the header
# parsing by hand would have cost that, and an API whose schema does not say it needs a
# credential is an API nobody can generate a working client for.
_bearer = HTTPBearer(
    scheme_name="MerchantApiKey",
    description=(
        "A merchant API key, presented as `Authorization: Bearer ar_dev_<credential>_<secret>`."
        " Keys are issued by the operator command line and are never returned by this API."
    ),
    auto_error=False,
)


async def require_merchant(
    session: SessionDep,
    presented: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> AuthenticatedMerchant:
    """Establish which merchant this request acts for, or refuse it.

    The one place an HTTP caller acquires a merchant identity. Every route that touches
    merchant owned state depends on this, and the identifier it returns is what every scoped
    query is filtered by. Nothing else in this application turns a request into a merchant: no
    body field, no path parameter and no header other than this one.

    The read is rolled back before the principal is returned, and that is load bearing rather
    than tidiness. Without it, the transaction the service goes on to use would have begun at
    the instant the caller was authenticated, and `audit_event.occurred_at` is
    `transaction_timestamp()`. Every event a request wrote would then carry the moment its
    credential was checked rather than the moment its work began, which would quietly change a
    documented property of the audit trail. Ending the read here means the service opens its own
    transaction, exactly as it does when the command line calls it. See docs/decisions.md.

    Rolling back is safe because what is returned is stable identifiers and, where appropriate,
    a small immutable benchmark-run capability. A principal built out of the credential row
    would be expired by this, and expired again by every deliberate rollback the services perform
    on a refusal.
    """
    if presented is None:
        raise AuthenticationError()

    principal = await MerchantCredentialService(session).authenticate(presented.credentials)
    if principal is None:
        raise AuthenticationError()

    await session.rollback()
    return principal


MerchantDep = Annotated[AuthenticatedMerchant, Depends(require_merchant)]


async def require_operator_merchant(merchant: MerchantDep) -> AuthenticatedMerchant:
    """Refuse a benchmark executor's own credential the merchant command surface.

    A credential the benchmark runner issued carries the run it was minted for, and it exists so
    one buyer process can shop one merchant for the length of one run. It is still a merchant
    credential, so it authenticates; what it must not do is command the benchmark it is running
    inside. A buyer that could queue an evaluation could touch the lifecycle of its own run.

    The loopback endpoint an isolated buyer is given already has no benchmark command surface on
    it at all, which is the layer that holds even if this one were removed. This is the second
    layer, and it holds for any deployment where a benchmark credential can reach the ordinary
    API. `AuthenticationError` rather than a 403, for the same reason every other refusal here
    is: this credential has not established that it is a merchant operator, and saying which of
    the two it is would be saying something about the credential.
    """
    if merchant.benchmark_capability is not None:
        raise AuthenticationError()
    return merchant


OperatorDep = Annotated[AuthenticatedMerchant, Depends(require_operator_merchant)]


async def require_mutation_permission(session: SessionDep, merchant: MerchantDep) -> None:
    """Refuse an ordinary credential changing a merchant an active run owns."""
    await BenchmarkMutationGuard(session).require_allowed(
        merchant.merchant_id, capability=merchant.benchmark_capability
    )


MutationDep = Annotated[None, Depends(require_mutation_permission)]
