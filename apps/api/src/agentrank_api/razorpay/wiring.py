"""Which Razorpay transport this application runs with, decided in one place.

The same rule as `agentrank_api.payments.wiring` and for the same reason: a transport that could
be constructed anywhere is a transport that could be pointed somewhere the application is not,
which for an operation that moves money is the problem rather than a detail.

None is an ordinary answer. An unconfigured integration is not a startup failure, because every
other payment path in this application works without it and refusing to start would make an
optional integration mandatory. The endpoints that need it refuse by name instead, so a caller
is told the integration is not configured rather than being handed a 500 from inside a
transport.
"""

from agentrank_api.config import Settings
from agentrank_api.razorpay.client import HttpRazorpayClient, RazorpayClient


def build_razorpay_client(settings: Settings) -> RazorpayClient | None:
    """The Razorpay transport, or None when no Test Mode key pair is configured.

    Settings carry the credentials and the validator has already refused a half configured pair
    and refused a live key, so anything that arrives here is a complete Test Mode credential.
    """
    credentials = settings.razorpay
    if credentials is None:
        return None
    return HttpRazorpayClient(credentials)
