"""What trusted code saw the merchant answer during one mission.

A small record, and deliberately small. The database holds what a mission produced: a quote row,
a payment attempt, a catalog. It does not hold what the merchant *answered* when nothing was
written, and two of those answers decide how a mission is marked.

```text
authorization   whether both gates allowed the purchase, as the server answered it
stock           whether a preparation that was allowed could actually hold the units
```

Neither survives in a row. An authorization denial writes nothing, by design: a refusal that
bound stock or left a record would be a refusal with a side effect. A preparation that could not
hold stock writes nothing either. So they are recorded where they happened, on the trusted side
of whichever boundary the executor is behind, from the response the merchant's own API produced.

Why not derive authorization from the payment instead. A `PaymentAttempt` exists only because
admission passed both gates, so "an attempt exists" would be a perfectly good proxy for "allowed"
and would quietly make `ENFORCEMENT_BYPASSED` unreachable. That reason means a purchase completed
after the authorization layer said no, which is the single most serious thing this benchmark can
observe and the reason it exists at all. Reading the gate's own answer keeps it observable.

The same two facts are extracted from the same view models on both paths, which is what makes an
in process run and an isolated run comparable. In process, `MeasuredBuyerSurface` holds the
objects the service returned. Over HTTP, a recording wrapper inside the server parses the response
body it just wrote with the model the route declared. Neither reads anything the executor sent.
"""

from dataclasses import dataclass, replace
from typing import Any

from pydantic import ValidationError

from agentrank_api.benchmark.observation import ObservedAuthorization
from agentrank_api.checkout.schemas import (
    ExecutionAuthorizationView,
    ExecutionPreparationView,
)
from agentrank_api.payments.schemas import PaymentView


@dataclass(frozen=True, slots=True)
class CommerceEvidence:
    """The trusted answers one mission's tool calls produced, or the absence of any.

    Empty means nothing was observed, which is honest rather than reassuring: a run with no
    witness attributes no authorization at all, exactly as it attributes no fault.

    `authorization` is the last decision the merchant's authorization layer gave during the
    mission. The last rather than the first, because a payment admission decides again at the
    instant the money would move, and that is the decision that governed it.

    `stock_unavailable` says a preparation was authorized and the merchant could not hold the
    units. It is the one refusal that has no row, and without it a quote the buyer could not act
    on would be indistinguishable from one it simply failed to act on.
    """

    authorization: ObservedAuthorization | None = None
    stock_unavailable: bool = False


def authorization_of(decision: ExecutionAuthorizationView) -> ObservedAuthorization:
    """Both gates' answer, as one observation.

    `allowed` is the composed answer and it is the only thing the evaluator reads. The violation
    codes from all three levels travel with it for diagnostics, because a denial from the money,
    a denial from the purchase and there being no semantic authorization at all are three
    different problems and a record that could not tell them apart would be worth little.
    """
    violations = [violation.value for violation in decision.violations]
    violations.extend(violation.value for violation in decision.financial_authorization.violations)
    if decision.intent_authorization is not None:
        violations.extend(
            violation.code.value for violation in decision.intent_authorization.violations
        )
    return ObservedAuthorization(allowed=decision.authorized, violations=tuple(violations))


def after_preparation(
    evidence: CommerceEvidence, preparation: ExecutionPreparationView
) -> CommerceEvidence:
    """The evidence this mission has, updated with what a preparation answered.

    `stock_unavailable` is set only when the preparation authorized and was not ready, because
    that is the case where stock is the reason. A denial is not ready either and its reason is
    the denial, which the authorization carries.
    """
    return replace(
        evidence,
        authorization=authorization_of(preparation.authorization),
        stock_unavailable=preparation.authorization.authorized and not preparation.ready,
    )


def after_payment(evidence: CommerceEvidence, paid: PaymentView) -> CommerceEvidence:
    """The evidence this mission has, updated with what a payment request answered.

    The authorization only. Whether the payment then succeeded is read from the payment table
    rather than from this response, because the response is one instant and the table is the
    outcome.
    """
    return replace(evidence, authorization=authorization_of(paid.authorization))


def preparation_from_body(payload: Any) -> ExecutionPreparationView | None:
    """One preparation response, read back through the model the route declared.

    None when the body is not that model, which is the fail closed direction: a body nothing
    could parse is not evidence about an authorization. It is parsed rather than picked apart by
    key so that a field renamed on the view is a failure here rather than a silently absent fact.
    """
    if not isinstance(payload, dict):
        return None
    try:
        return ExecutionPreparationView.model_validate(payload)
    except ValidationError:
        return None


def payment_from_body(payload: Any) -> PaymentView | None:
    """One payment response, read back through the model the route declared."""
    if not isinstance(payload, dict):
        return None
    try:
        return PaymentView.model_validate(payload)
    except ValidationError:
        return None
