"""What can go wrong at the Razorpay boundary, split by what a caller may safely conclude.

Three classes, and the split is the same one the payment provider interface makes for the same
reason: the expensive mistake at a payment boundary is treating "I did not hear back" as "it did
not happen".

```text
RazorpayUnavailableError   the request may or may not have been performed. Nothing may
                           be concluded, and specifically nothing may be created again
RazorpayRefusedError       Razorpay answered definitively and refused. The request as
                           sent is not going to succeed by being sent again
RazorpayUnreadableError    Razorpay answered with something this application cannot
                           parse. Definitive in the sense that retrying will not help,
                           and a defect somewhere rather than a state
```

None of these subclasses `AgentRankError`. They are vendor shaped facts and they stop at the
service that called the transport, which translates them into this application's own vocabulary
with a stable reason a caller can act on. A transport error reaching an HTTP handler unchanged
would put a vendor's prose in this application's error body.

A timeout, a reset connection, a 429 and a 5xx are all `RazorpayUnavailableError`. A 5xx is
grouped with the timeouts deliberately: a gateway that reports a server error may still have
created the order, and treating it as a definitive refusal is how a second order gets created
for a payment that already has one.
"""


class RazorpayError(Exception):
    """Anything that went wrong talking to Razorpay."""


class RazorpayUnavailableError(RazorpayError):
    """The answer never arrived, or arrived as an error that proves nothing.

    Ambiguous on purpose. The operation may have been performed. A caller that creates
    something must recover by asking Razorpay what exists rather than by creating again, which
    is what the deterministic receipt is for.
    """


class RazorpayRefusedError(RazorpayError):
    """Razorpay answered and said no.

    `code` and `description` come from the documented error envelope. They are carried for the
    audit trail and for an operator reading a log, and they are never put on the wire: a
    vendor's prose in this application's error body is a vendor's prose in a buyer agent's
    parser.
    """

    def __init__(self, status_code: int, code: str, description: str) -> None:
        super().__init__(f"razorpay refused with {status_code} {code}: {description}")
        self.status_code = status_code
        self.code = code
        self.description = description


class RazorpayUnreadableError(RazorpayError):
    """A 200 whose body is not the entity it was supposed to be.

    Its own class rather than folded into either of the others, because it means neither "try
    later" nor "the request was wrong". It means the assumption this transport is built on has
    stopped holding, which is worth being able to find in a log by type.
    """


class RazorpayOrderMismatchError(RazorpayError):
    """A provider entity does not describe the payment this application authorized.

    Not a transport failure. Razorpay answered, and the answer was well formed, and it was about
    something else: an order carrying a different amount, a payment against a different order, a
    currency that is not the one the quote was priced in.

    It is a `RazorpayError` because the fact came from Razorpay, and it is its own class because
    the response to it is different from the response to every other error here. An unavailable
    gateway is asked again later. A refusal is a request to fix. A mismatch means stop, and
    specifically means do not open Standard Checkout against this order and do not consume any
    stock for this payment.

    The expected and observed values are carried for the trail. Neither is secret: they are an
    amount, a currency and an identifier this application already holds.
    """

    def __init__(self, entity_id: str, field: str, expected: str, observed: str) -> None:
        super().__init__(f"razorpay {entity_id} has {field} {observed}, expected {expected}")
        self.entity_id = entity_id
        self.field = field
        self.expected = expected
        self.observed = observed
