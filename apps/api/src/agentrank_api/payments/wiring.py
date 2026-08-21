"""Which payment provider this application runs with, decided in one place.

Two processes now reach a provider: the HTTP application built by `create_app`, and the
operator command line in `agentrank_api.cli`. They must reach the same one. A tool that
constructed its own provider would be a tool that could be pointed somewhere the application
is not, which for an operation that moves money is the whole problem rather than a detail.

There is exactly one implementation and it is a deterministic fake. That is deliberate for
this phase and it is why this module is three lines: when a real processor exists it is
configured here, from settings, and both callers get it without either of them changing.

Nothing selects a provider from a request, an argument or an environment variable that a
caller controls. See docs/integrations.md.
"""

from agentrank_api.payments.fake import FakePaymentProvider
from agentrank_api.payments.provider import PaymentProvider


def build_payment_provider() -> PaymentProvider:
    """The provider every entry point into this application is wired with.

    Takes no arguments today because nothing is configurable today. A real integration adds
    `settings` here and nowhere else, so the choice stays one function rather than one
    function per process that happens to need a provider.
    """
    return FakePaymentProvider()
