"""Verifying a Standard Checkout callback: what is proven, what is refused, and what converges.

Three subjects.

The first is the happy path, and the assertion that matters there is not that the endpoint
returned 200. It is that a Razorpay Test Mode payment and a `FakePaymentProvider` payment produce
the same AgentRank business truth: attempt SUCCEEDED, checkout PAID, reservation CONSUMED,
inventory decremented by exactly the quantity, once. Two settlement shapes, one definition of
paid, and the test compares them directly rather than asserting each separately.

The second is tampering. Every field in the callback comes from a customer's browser, so each
one gets its own test: a wrong signature, a signature for a different payment, an order
identifier that is not the one this payment was prepared for, one merchant submitting a callback
for another merchant's binding, and a genuinely signed callback whose provider payment turns out
to carry the wrong amount or the wrong currency. All of them must fail closed, meaning no
inventory is consumed, and the four that can be decided locally must reach Razorpay zero times.

The third is what a valid signature does not prove. A signature is computed over two identifiers
and verifies just as well for a payment that was authorized and never captured. Those cases are
reported honestly and applied to nothing, and there is a test per documented Razorpay state so
the mapping cannot quietly widen.
"""

import uuid

import pytest
from commerce_support import PRICE, admit, build_shop, quote
from conftest import CredentialIssuer, bearer
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agentrank_api.audit.repository import AuditRepository
from agentrank_api.checkout.models import CheckoutSession, CheckoutStatus
from agentrank_api.commerce.models import Variant
from agentrank_api.config import RazorpayCredentials, Settings
from agentrank_api.errors import ConflictError, NotFoundError, UpstreamError
from agentrank_api.inventory.models import InventoryReservation, ReservationStatus
from agentrank_api.main import create_app
from agentrank_api.payments.fake import FakeOutcome, FakePaymentProvider
from agentrank_api.payments.models import OutcomeSource, PaymentAttempt, PaymentAttemptStatus
from agentrank_api.payments.service import PaymentService
from agentrank_api.razorpay.entities import RazorpayPayment
from agentrank_api.razorpay.fake import FakeRazorpayClient
from agentrank_api.razorpay.models import RazorpayCheckout, RazorpayCheckoutStatus
from agentrank_api.razorpay.service import RAZORPAY_RESOURCE, RazorpayCheckoutService
from agentrank_api.razorpay.signature import expected_signature
from agentrank_api.razorpay.translation import ObservedState
from agentrank_api.razorpay.verification import (
    RAZORPAY_CALLBACK_REJECTED,
    RAZORPAY_PAYMENT_CONFIRMED,
    RAZORPAY_PAYMENT_VERIFIED,
    CheckoutCallback,
    RazorpayVerificationService,
    VerifiedPayment,
)

pytestmark = pytest.mark.anyio

KEY = "razorpay-verify-001"
KEY_ID = "rzp_test_0123456789abcd"
KEY_SECRET = "not-a-real-secret"
STOCK = 5
PAYMENTS_URL = "/api/v1/commerce/payments"


def credentials() -> RazorpayCredentials:
    return RazorpayCredentials(
        key_id=KEY_ID,
        key_secret=SecretStr(KEY_SECRET),
        api_base_url="https://api.razorpay.com/v1",
        timeout_seconds=5.0,
    )


class Prepared:
    """A shop, a quote, an admitted payment and a Razorpay order waiting to be paid."""

    def __init__(
        self,
        session: AsyncSession,
        gateway: FakeRazorpayClient,
        attempt: PaymentAttempt,
        binding: RazorpayCheckout,
    ) -> None:
        self.session = session
        self.gateway = gateway
        self.attempt = attempt
        self.binding = binding
        self.order_id = binding.provider_order_id or ""

    def verification(
        self, provider: FakePaymentProvider | None = None
    ) -> RazorpayVerificationService:
        return RazorpayVerificationService(
            self.session, self.gateway, credentials(), provider or FakePaymentProvider()
        )

    def callback(
        self,
        payment_id: str,
        *,
        order_id: str | None = None,
        signed_order_id: str | None = None,
        signed_payment_id: str | None = None,
    ) -> CheckoutCallback:
        """A callback carrying a genuine signature, unless a test asks for a different one.

        The signature is produced by the same function the verifier uses, which is deliberate:
        a test that reimplemented the formula would pass while both copies were wrong together.
        Tampering is expressed as a difference between what was signed and what is claimed,
        which is exactly the shape a real forgery attempt has.
        """
        return CheckoutCallback(
            payment_id=payment_id,
            order_id=order_id or self.order_id,
            signature=expected_signature(
                signed_order_id or self.order_id,
                signed_payment_id or payment_id,
                SecretStr(KEY_SECRET),
            ),
        )

    def pay(
        self, *, status: str = "captured", amount_minor: int = PRICE, currency: str = "INR"
    ) -> RazorpayPayment:
        """A Razorpay payment against this order, as a completed checkout would produce."""
        return self.gateway.add_payment(
            order_id=self.order_id, status=status, amount_minor=amount_minor, currency=currency
        )


async def prepared(session: AsyncSession, slug: str = "ampere-supply") -> Prepared:
    shop = await build_shop(session, slug, inventory=STOCK)
    attempt = await admit(session, shop, await quote(session, shop), key=KEY)
    gateway = FakeRazorpayClient()
    checkout = await RazorpayCheckoutService(session, gateway, credentials()).prepare(
        attempt.id, merchant_id=attempt.merchant_id
    )
    return Prepared(session, gateway, checkout.attempt, checkout.binding)


async def stock(session: AsyncSession, merchant_id: uuid.UUID) -> int:
    quantity = await session.scalar(
        select(Variant.inventory_quantity).where(Variant.merchant_id == merchant_id)
    )
    assert quantity is not None
    return int(quantity)


async def truth(session: AsyncSession, attempt_id: uuid.UUID) -> dict[str, object]:
    """The four facts that define paid, read back from committed state.

    One helper, used by both the Razorpay path and the fake provider path, so the assertion
    that they agree is a comparison of two dictionaries rather than two lists of assertions
    somebody has to check line up.
    """
    # Expired first, so every read below comes from the database rather than from whatever this
    # session happens to be holding. Several of these rows were written by services using this
    # same session, and a stale identity map would let a test pass on a value that was never
    # committed.
    session.expire_all()
    attempt = await session.get(PaymentAttempt, attempt_id)
    assert attempt is not None
    checkout = await session.get(CheckoutSession, attempt.checkout_id)
    reservation = await session.get(InventoryReservation, attempt.reservation_id)
    assert checkout is not None
    assert reservation is not None
    return {
        "attempt_status": attempt.status,
        "checkout_status": checkout.status,
        "reservation_status": reservation.status,
        "inventory": await stock(session, attempt.merchant_id),
    }


async def test_a_captured_payment_converges_on_the_ordinary_success_semantics(
    session: AsyncSession,
) -> None:
    """The whole point of the phase, in one assertion.

    A confirmed Razorpay capture produces the same business truth an autonomous payment does,
    because it produces it through the same code: the same locks in the same order, the same
    atomic transaction, the same inventory arithmetic.
    """
    subject = await prepared(session)
    payment = subject.pay()

    verified = await subject.verification().verify(
        subject.attempt.id,
        merchant_id=subject.attempt.merchant_id,
        callback=subject.callback(payment.id),
    )

    assert verified.confirmed is True
    assert verified.changed is True
    assert verified.state is ObservedState.SUCCEEDED
    assert verified.attempt.provider_reference == payment.id
    assert verified.attempt.outcome_source is OutcomeSource.INTERACTIVE
    assert verified.binding.status is RazorpayCheckoutStatus.CONFIRMED
    assert verified.binding.provider_payment_id == payment.id
    # Read last, because it expires this session's identity map to force every value below to
    # come from the database rather than from an object a service handed back.
    assert await truth(session, subject.attempt.id) == {
        "attempt_status": PaymentAttemptStatus.SUCCEEDED,
        "checkout_status": CheckoutStatus.PAID,
        "reservation_status": ReservationStatus.CONSUMED,
        "inventory": STOCK - 1,
    }


async def test_the_razorpay_path_and_the_fake_provider_path_agree(
    session: AsyncSession,
) -> None:
    """One definition of paid, reached two ways, compared directly.

    Two merchants, two identical shops, two identical quotes. One is settled by a customer
    completing a Razorpay checkout and one by the autonomous kernel against the deterministic
    fake. Every fact that matters about the result is the same, which is what "do not fork two
    incompatible definitions of paid" means in practice.
    """
    interactive = await prepared(session, "ampere-supply")
    payment = interactive.pay()
    interactive_id = interactive.attempt.id
    await interactive.verification().verify(
        interactive_id,
        merchant_id=interactive.attempt.merchant_id,
        callback=interactive.callback(payment.id),
    )

    shop = await build_shop(session, "volta-goods", inventory=STOCK)
    checkout_id = await quote(session, shop)
    await session.commit()
    autonomous = await PaymentService(
        session, FakePaymentProvider(default=FakeOutcome.SUCCESS)
    ).pay(checkout_id, merchant_id=shop.merchant_id, idempotency_key=KEY)
    assert autonomous.attempt is not None
    autonomous_id = autonomous.attempt.id

    assert await truth(session, interactive_id) == await truth(session, autonomous_id)


async def test_a_repeated_callback_changes_nothing_and_asks_nothing(
    session: AsyncSession,
) -> None:
    """Inventory decrements once, the reservation consumes once, the checkout is paid once.

    A browser can retry, a user can reload and a network can duplicate, so this is the ordinary
    case rather than an edge one. The second call still verifies its signature, because a repeat
    is not a reason to stop checking, and then answers from settled state without asking the
    gateway anything.
    """
    subject = await prepared(session)
    payment = subject.pay()
    callback = subject.callback(payment.id)
    service = subject.verification()

    first = await service.verify(
        subject.attempt.id, merchant_id=subject.attempt.merchant_id, callback=callback
    )
    calls_after_first = subject.gateway.calls
    second = await service.verify(
        subject.attempt.id, merchant_id=subject.attempt.merchant_id, callback=callback
    )

    assert first.changed is True
    assert second.changed is False
    assert second.confirmed is True
    assert subject.gateway.calls == calls_after_first
    assert await truth(session, subject.attempt.id) == {
        "attempt_status": PaymentAttemptStatus.SUCCEEDED,
        "checkout_status": CheckoutStatus.PAID,
        "reservation_status": ReservationStatus.CONSUMED,
        "inventory": STOCK - 1,
    }


async def unchanged(session: AsyncSession, subject: Prepared) -> None:
    """Nothing that matters moved: the fail closed assertion, in one place."""
    assert await truth(session, subject.attempt.id) == {
        "attempt_status": PaymentAttemptStatus.ADMITTED,
        "checkout_status": CheckoutStatus.OPEN,
        "reservation_status": ReservationStatus.COMMITTED,
        "inventory": STOCK,
    }


async def test_a_wrong_signature_fails_closed_before_the_gateway(
    session: AsyncSession,
) -> None:
    subject = await prepared(session)
    payment = subject.pay()
    before = subject.gateway.calls
    forged = CheckoutCallback(payment_id=payment.id, order_id=subject.order_id, signature="0" * 64)

    with pytest.raises(ConflictError) as refused:
        await subject.verification().verify(
            subject.attempt.id, merchant_id=subject.attempt.merchant_id, callback=forged
        )

    assert refused.value.reason == "razorpay_signature_invalid"
    assert subject.gateway.calls == before
    await unchanged(session, subject)


async def test_a_signature_for_a_different_payment_fails_closed(
    session: AsyncSession,
) -> None:
    """A genuine digest is still the wrong digest for the payment being claimed.

    This is the interesting forgery: the attacker holds a real signature from some other
    payment and swaps the payment identifier. The message includes both halves, so it does not
    verify.
    """
    subject = await prepared(session)
    mine = subject.pay()
    theirs = subject.pay()
    before = subject.gateway.calls
    swapped = subject.callback(mine.id, signed_payment_id=theirs.id)

    with pytest.raises(ConflictError) as refused:
        await subject.verification().verify(
            subject.attempt.id, merchant_id=subject.attempt.merchant_id, callback=swapped
        )

    assert refused.value.reason == "razorpay_signature_invalid"
    assert subject.gateway.calls == before
    await unchanged(session, subject)


async def test_a_callback_naming_another_order_fails_closed(session: AsyncSession) -> None:
    """The stored order identifier anchors verification, and a disagreement is named.

    Signed consistently with the order the browser claims, which is what a payload verified
    against itself would look like. It is refused before the signature is even computed, because
    the claimed order is not the one this payment was prepared for.
    """
    subject = await prepared(session)
    payment = subject.pay()
    before = subject.gateway.calls
    elsewhere = subject.callback(
        payment.id, order_id="order_SOMEWHERE_ELSE", signed_order_id="order_SOMEWHERE_ELSE"
    )

    with pytest.raises(ConflictError) as refused:
        await subject.verification().verify(
            subject.attempt.id, merchant_id=subject.attempt.merchant_id, callback=elsewhere
        )

    assert refused.value.reason == "razorpay_order_mismatch"
    assert subject.gateway.calls == before
    await unchanged(session, subject)


async def test_a_signature_computed_over_the_supplied_order_does_not_verify(
    session: AsyncSession,
) -> None:
    """The specific attack the stored order identifier exists to stop.

    The callback claims the real order so the equality check passes, and carries a signature
    computed over a different one. If verification anchored on anything the browser sent, an
    attacker who obtained a signature for their own order could present it against somebody
    else's payment. It anchors on the column, so this fails.
    """
    subject = await prepared(session)
    payment = subject.pay()
    before = subject.gateway.calls
    forged = subject.callback(payment.id, signed_order_id="order_ATTACKER_OWN")

    with pytest.raises(ConflictError) as refused:
        await subject.verification().verify(
            subject.attempt.id, merchant_id=subject.attempt.merchant_id, callback=forged
        )

    assert refused.value.reason == "razorpay_signature_invalid"
    assert subject.gateway.calls == before
    await unchanged(session, subject)


async def test_a_cross_merchant_callback_answers_not_found_and_calls_nothing(
    session: AsyncSession,
) -> None:
    """Merchant A submitting a genuine looking callback for merchant B's binding.

    Refused on ownership, before the binding is read and before the gateway is asked anything.
    The signature is genuine here on purpose: authenticity is not authorization, and a callback
    that Razorpay really did sign still belongs to exactly one merchant.
    """
    subject = await prepared(session, "ampere-supply")
    payment = subject.pay()
    intruder = await build_shop(session, "volta-goods")
    before = subject.gateway.calls

    with pytest.raises(NotFoundError):
        await subject.verification().verify(
            subject.attempt.id,
            merchant_id=intruder.merchant_id,
            callback=subject.callback(payment.id),
        )

    assert subject.gateway.calls == before
    await unchanged(session, subject)


@pytest.mark.parametrize(
    ("amount_minor", "currency"),
    [(PRICE // 2, "INR"), (PRICE, "USD")],
)
async def test_a_provider_payment_that_disagrees_about_money_fails_closed(
    session: AsyncSession, amount_minor: int, currency: str
) -> None:
    """A valid signature over a payment that collected the wrong amount is still not a sale.

    The signature covers two identifiers and says nothing about money, which is exactly why a
    second, independent check exists. The amount is compared against the binding, which carries
    the attempt's own amount through a composite foreign key, so this compares what Razorpay
    collected against what the mandate authorized.
    """
    subject = await prepared(session)
    payment = subject.pay(amount_minor=amount_minor, currency=currency)

    with pytest.raises(ConflictError) as refused:
        await subject.verification().verify(
            subject.attempt.id,
            merchant_id=subject.attempt.merchant_id,
            callback=subject.callback(payment.id),
        )

    assert refused.value.reason == "razorpay_payment_mismatch"
    # The attempt is IN_FLIGHT rather than ADMITTED: a verified callback proved a provider
    # payment exists, and that is what IN_FLIGHT means. Nothing else moved.
    settled = await truth(session, subject.attempt.id)
    assert settled["attempt_status"] is PaymentAttemptStatus.IN_FLIGHT
    assert settled["checkout_status"] is CheckoutStatus.OPEN
    assert settled["reservation_status"] is ReservationStatus.COMMITTED
    assert settled["inventory"] == STOCK


async def test_a_payment_belonging_to_another_order_fails_closed(
    session: AsyncSession,
) -> None:
    subject = await prepared(session)
    elsewhere = subject.gateway.add_payment(
        order_id="order_SOMEBODY_ELSE", status="captured", amount_minor=PRICE
    )

    with pytest.raises(ConflictError) as refused:
        await subject.verification().verify(
            subject.attempt.id,
            merchant_id=subject.attempt.merchant_id,
            callback=subject.callback(elsewhere.id),
        )

    assert refused.value.reason == "razorpay_payment_mismatch"
    assert (await truth(session, subject.attempt.id))["inventory"] == STOCK


@pytest.mark.parametrize(
    ("status", "state"),
    [
        ("authorized", ObservedState.PENDING),
        ("created", ObservedState.PENDING),
        ("failed", ObservedState.FAILED),
        ("refunded", ObservedState.REVERSED),
        ("something_new", ObservedState.UNRECOGNIZED),
    ],
)
async def test_only_a_captured_payment_settles_anything(
    session: AsyncSession, status: str, state: ObservedState
) -> None:
    """A valid signature proves authenticity and says nothing about state.

    One case per documented Razorpay payment state, plus one the integration has never seen, so
    the mapping cannot quietly widen. Each is reported honestly and applied to nothing: no
    outcome, no consumption, no inventory movement, no paid checkout.

    `authorized` is the one worth reading twice. The funds are held and the merchant has not
    taken them, and Razorpay refunds an uncaptured authorization after three days. Fulfilling
    against one would mean shipping goods for money that may never arrive, and nothing tells
    this application when that window closes.
    """
    subject = await prepared(session)
    payment = subject.pay(status=status)

    verified = await subject.verification().verify(
        subject.attempt.id,
        merchant_id=subject.attempt.merchant_id,
        callback=subject.callback(payment.id),
    )

    assert verified.confirmed is False
    assert verified.changed is False
    assert verified.state is state
    assert verified.binding.status is RazorpayCheckoutStatus.AWAITING_PAYMENT
    settled = await truth(session, subject.attempt.id)
    assert settled["checkout_status"] is CheckoutStatus.OPEN
    assert settled["reservation_status"] is ReservationStatus.COMMITTED
    assert settled["inventory"] == STOCK


async def test_a_captured_flag_that_contradicts_the_status_settles_nothing(
    session: AsyncSession,
) -> None:
    """Two incompatible facts from one vendor, and neither is chosen quietly."""
    subject = await prepared(session)
    payment = subject.gateway.add_payment(
        order_id=subject.order_id, status="captured", amount_minor=PRICE, captured=False
    )

    verified = await subject.verification().verify(
        subject.attempt.id,
        merchant_id=subject.attempt.merchant_id,
        callback=subject.callback(payment.id),
    )

    assert verified.state is ObservedState.UNRECOGNIZED
    assert verified.confirmed is False
    assert (await truth(session, subject.attempt.id))["inventory"] == STOCK


async def test_a_payment_razorpay_has_no_record_of_is_a_gateway_failure(
    session: AsyncSession,
) -> None:
    """A signature this application can verify for a payment the gateway denies is not a sale."""
    subject = await prepared(session)

    with pytest.raises(UpstreamError) as refused:
        await subject.verification().verify(
            subject.attempt.id,
            merchant_id=subject.attempt.merchant_id,
            callback=subject.callback("pay_NEVER_EXISTED"),
        )

    assert refused.value.reason == "razorpay_payment_missing"
    assert (await truth(session, subject.attempt.id))["inventory"] == STOCK


async def test_verifying_before_a_checkout_is_prepared_is_refused(
    session: AsyncSession,
) -> None:
    shop = await build_shop(session, "ampere-supply", inventory=STOCK)
    attempt = await admit(session, shop, await quote(session, shop), key=KEY)
    gateway = FakeRazorpayClient()

    with pytest.raises(ConflictError) as refused:
        await RazorpayVerificationService(
            session, gateway, credentials(), FakePaymentProvider()
        ).verify(
            attempt.id,
            merchant_id=attempt.merchant_id,
            callback=CheckoutCallback(
                payment_id="pay_ANY", order_id="order_ANY", signature="0" * 64
            ),
        )

    assert refused.value.reason == "razorpay_checkout_missing"
    assert gateway.calls == 0


async def test_the_trail_records_the_verification_and_the_confirmation(
    session: AsyncSession,
) -> None:
    """Evidence beside the authoritative state, with no secret anywhere in it."""
    subject = await prepared(session)
    payment = subject.pay()

    await subject.verification().verify(
        subject.attempt.id,
        merchant_id=subject.attempt.merchant_id,
        callback=subject.callback(payment.id),
    )

    events = await AuditRepository(session).list_for_resource(
        resource_type=RAZORPAY_RESOURCE, resource_id=subject.binding.id
    )
    assert [event.event_type for event in events] == [
        "razorpay.order_created",
        RAZORPAY_PAYMENT_VERIFIED,
        RAZORPAY_PAYMENT_CONFIRMED,
    ]
    assert KEY_SECRET not in str([event.payload for event in events])
    assert events[1].payload["provider_state"] == "SUCCEEDED"
    assert events[2].payload["provider_payment_id"] == payment.id


async def test_a_rejected_callback_is_recorded(session: AsyncSession) -> None:
    """A payload that did not verify is either a broken client or somebody trying.

    Both are worth being able to find afterwards. The claimed payment identifier is recorded
    because it is the only handle on what was asserted; the rejected signature is not, because
    storing forged digests is storing an attacker's work for them.
    """
    subject = await prepared(session)
    forged = CheckoutCallback(
        payment_id="pay_CLAIMED", order_id=subject.order_id, signature="f" * 64
    )

    with pytest.raises(ConflictError):
        await subject.verification().verify(
            subject.attempt.id, merchant_id=subject.attempt.merchant_id, callback=forged
        )

    events = await AuditRepository(session).list_for_resource(
        resource_type=RAZORPAY_RESOURCE, resource_id=subject.binding.id
    )
    rejected = [event for event in events if event.event_type == RAZORPAY_CALLBACK_REJECTED]
    assert len(rejected) == 1
    assert rejected[0].payload["claimed_payment_id"] == "pay_CLAIMED"
    assert rejected[0].payload["reason"] == "razorpay_signature_invalid"
    assert "f" * 64 not in str(rejected[0].payload)


# The HTTP surface.


@pytest.fixture
def shop_settings(catalog_settings: Settings) -> Settings:
    return catalog_settings.model_copy(
        update={"razorpay_key_id": KEY_ID, "razorpay_key_secret": SecretStr(KEY_SECRET)}
    )


async def test_the_endpoint_settles_a_test_payment_end_to_end(
    shop_settings: Settings, session: AsyncSession, issue_credential: CredentialIssuer
) -> None:
    """The whole browser flow, over HTTP, with the gateway faked and nothing else."""
    shop = await build_shop(session, "ampere-supply", inventory=STOCK)
    checkout_id = await quote(session, shop)
    await session.commit()
    token = await issue_credential(shop.merchant_id)
    await session.commit()
    gateway = FakeRazorpayClient()

    with TestClient(
        create_app(shop_settings, razorpay_client=gateway), headers=bearer(token)
    ) as client:
        preparation = client.post(
            f"/api/v1/commerce/checkouts/{checkout_id}/razorpay-checkout",
            json={"idempotency_key": KEY},
        )
        assert preparation.status_code == 200
        prepared_body = preparation.json()["razorpay"]
        attempt_id = prepared_body["payment_attempt_id"]
        order_id = prepared_body["provider_order_id"]

        # What a customer completing Standard Checkout produces at the gateway.
        payment = gateway.add_payment(order_id=order_id, status="captured", amount_minor=PRICE)

        verification = client.post(
            f"{PAYMENTS_URL}/{attempt_id}/razorpay-checkout/verify",
            json={
                "razorpay_payment_id": payment.id,
                "razorpay_order_id": order_id,
                "razorpay_signature": expected_signature(
                    order_id, payment.id, SecretStr(KEY_SECRET)
                ),
            },
        )

    assert verification.status_code == 200
    body = verification.json()
    assert body["confirmed"] is True
    assert body["changed"] is True
    assert body["provider_state"] == "SUCCEEDED"
    assert body["razorpay_status"] == "CONFIRMED"
    assert body["attempt"]["status"] == "SUCCEEDED"
    assert body["attempt"]["outcome_source"] == "INTERACTIVE"
    assert KEY_SECRET not in verification.text
    assert await truth(session, uuid.UUID(attempt_id)) == {
        "attempt_status": PaymentAttemptStatus.SUCCEEDED,
        "checkout_status": CheckoutStatus.PAID,
        "reservation_status": ReservationStatus.CONSUMED,
        "inventory": STOCK - 1,
    }


async def test_the_endpoint_refuses_a_malformed_signature_as_a_validation_error(
    shop_settings: Settings, session: AsyncSession, issue_credential: CredentialIssuer
) -> None:
    """A value that cannot be a digest is refused before it reaches a comparison.

    Bounded at the schema so a Unicode string cannot reach `hmac.compare_digest`, which raises
    on one, and so nothing unbounded reaches a log line or an audit payload.
    """
    subject = await prepared(session)
    await session.commit()
    token = await issue_credential(subject.attempt.merchant_id)
    await session.commit()

    with TestClient(
        create_app(shop_settings, razorpay_client=subject.gateway), headers=bearer(token)
    ) as client:
        response = client.post(
            f"{PAYMENTS_URL}/{subject.attempt.id}/razorpay-checkout/verify",
            json={
                "razorpay_payment_id": "pay_ANY",
                "razorpay_order_id": subject.order_id,
                "razorpay_signature": "not a digest é",
            },
        )

    assert response.status_code == 422
    await unchanged(session, subject)


async def test_a_cross_merchant_http_callback_answers_404_and_calls_nothing(
    shop_settings: Settings, session: AsyncSession, issue_credential: CredentialIssuer
) -> None:
    subject = await prepared(session, "ampere-supply")
    payment = subject.pay()
    intruder = await build_shop(session, "volta-goods")
    await session.commit()
    token = await issue_credential(intruder.merchant_id)
    await session.commit()
    before = subject.gateway.calls

    with TestClient(
        create_app(shop_settings, razorpay_client=subject.gateway), headers=bearer(token)
    ) as client:
        response = client.post(
            f"{PAYMENTS_URL}/{subject.attempt.id}/razorpay-checkout/verify",
            json={
                "razorpay_payment_id": payment.id,
                "razorpay_order_id": subject.order_id,
                "razorpay_signature": expected_signature(
                    subject.order_id, payment.id, SecretStr(KEY_SECRET)
                ),
            },
        )

    assert response.status_code == 404
    assert subject.gateway.calls == before
    await unchanged(session, subject)


def test_the_verified_payment_result_is_frozen() -> None:
    """A caller is handed this and a caller is not this application's code."""
    assert VerifiedPayment.__dataclass_params__.frozen  # type: ignore[attr-defined]
