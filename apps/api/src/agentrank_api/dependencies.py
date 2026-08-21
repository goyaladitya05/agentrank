"""FastAPI dependencies shared by routes."""

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agentrank_api.payments.provider import PaymentProvider


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
