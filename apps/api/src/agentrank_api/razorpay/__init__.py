"""The Razorpay Test Mode bridge: an interactive checkout, not an autonomous provider.

Razorpay Standard Checkout is a browser flow. The Payments API does not collect a new payment
server side, so a `PaymentProvider.execute` adapter for it would be a lie about who performs the
operation. What actually happens is five steps across two processes:

```text
backend    create a Razorpay Order for an admitted PaymentAttempt
browser    open Standard Checkout against that order
customer   pay
browser    hand back razorpay_payment_id, razorpay_order_id, razorpay_signature
backend    verify the signature, confirm the payment with Razorpay, apply the outcome
```

`FakePaymentProvider` remains the provider the payment execution kernel runs against, and the
`PaymentProvider` contract is untouched. What this package adds is a second way to reach the
same authoritative outcome, converging on the existing success machinery rather than forking a
second definition of paid.
"""
