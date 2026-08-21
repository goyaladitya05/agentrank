"""What every operator command is, and what it is given.

Its own module so that each command group can implement this without importing the others, and
so that the runner can dispatch to any of them through one type.

One signature for every command, whether or not a particular command uses every part of it. A
credential command is handed a payment provider it will never call, and that is the deliberate
choice: the alternative is a second dispatch path, and two paths that have to be kept in step
are one more thing to get wrong than one path with an unused parameter.
"""

import argparse
from collections.abc import Awaitable
from typing import Protocol, TextIO

from sqlalchemy.ext.asyncio import AsyncSession

from agentrank_api.config import Settings
from agentrank_api.payments.provider import PaymentProvider


class Command(Protocol):
    """One operator command, given everything it needs and no way to find anything else.

    Every collaborator is handed in rather than discovered, which is what keeps a command from
    building its own of any of them. A command that could construct a provider could be pointed
    at one the application is not running with, and a command that read configuration itself
    could be pointed at a different database than the one the runner opened.
    """

    def __call__(
        self,
        session: AsyncSession,
        provider: PaymentProvider,
        arguments: argparse.Namespace,
        out: TextIO,
        settings: Settings,
    ) -> Awaitable[int]: ...
