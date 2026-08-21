"""May this checkout be prepared for execution, according to both gates at once.

Phase 1C answered whether the money is within what was authorized. Phase 1D answered
whether the thing being bought is what the buyer asked for. Neither substitutes for the
other, and until now nothing required both, which left the rule "both must allow" as
documentation rather than as code. This is where it becomes code.

What this is not: permission to pay. Nothing in this application moves money, and this
decision does not claim a checkout is ready either. Readiness also needs stock, which needs
a lock and a write, and neither belongs in a pure function. The name says what it is:
authorization for a future execution, evaluated now.

The two evaluators underneath stay separate and are not merged. Each is still callable and
testable on its own, each still reports its own violations in its own vocabulary, and this
holds both answers rather than flattening them into one boolean. A caller that only wants
to know whether to proceed reads `authorized`; a caller that has to explain a refusal still
has everything both gates found.

Everything here is pure. It reads no clock beyond the instant it is handed, touches no
database, calls no external service, consults no model and writes nothing, including no
audit event.

The load bearing rule is what happens when there is no `IntentConstraintSet`. A mandate can
exist without one: it is created first and qualified afterwards through a separate call. An
absent set must never be read as "there were no semantic requirements", because that is the
most dangerous default this system could have. It is reported as `INTENT_CONSTRAINTS_MISSING`
and the result is not authorized, whatever the financial gate said. A set that exists and
holds no constraints is a different thing and is not this case.
"""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Self

from agentrank_api.checkout.authorization import (
    CheckoutAuthorizationDecision,
    authorize_checkout,
)
from agentrank_api.checkout.intent_authorization import (
    IntentConstraintDecision,
    evaluate_intent_constraints,
)
from agentrank_api.checkout.models import CheckoutSession
from agentrank_api.constraints.models import IntentConstraintSet
from agentrank_api.mandates.models import SpendingMandate


class ExecutionAuthorizationViolation(StrEnum):
    """Why execution authorization could not be established.

    Deliberately not a copy of the two vocabularies underneath. Those are reported as
    themselves, in their own decisions. This names only what is wrong with the pair, and
    today there is exactly one such thing: half of the authorization does not exist.
    """

    INTENT_CONSTRAINTS_MISSING = "INTENT_CONSTRAINTS_MISSING"


@dataclass(frozen=True, slots=True)
class CheckoutExecutionAuthorization:
    """Both gates over one checkout at one instant, with neither answer thrown away.

    `authorized` is derived rather than stored, so a result carrying a denial cannot also
    claim to permit anything. It requires the semantic decision to exist at all, which is
    what makes a missing constraint set fail closed by construction rather than by a caller
    remembering to check.

    `intent` is None only when there was no constraint set to evaluate, and that case always
    carries `INTENT_CONSTRAINTS_MISSING`. The financial decision is still made and still
    reported, because a caller fixing one problem should be able to see the other.
    """

    financial: CheckoutAuthorizationDecision
    intent: IntentConstraintDecision | None
    violations: tuple[ExecutionAuthorizationViolation, ...] = ()

    @property
    def authorized(self) -> bool:
        return (
            not self.violations
            and self.financial.allowed
            and self.intent is not None
            and self.intent.satisfied
        )

    def with_financial(self, financial: CheckoutAuthorizationDecision) -> Self:
        """This authorization with the financial half decided again, at a later instant.

        Only half of this decision can move with the clock. The financial gate reads a
        mandate's validity window and a quote's expiry and status, all of which can be
        different a moment later. The semantic gate reads the quote's own snapshot against
        an immutable constraint set, so re-evaluating it against a different instant would
        do the same work and reach the same answer.

        For a caller that blocked on a lock after deciding once and has to decide again
        before committing anything. Carrying the semantic half forward rather than
        recomputing it is what says out loud which half was capable of changing.
        """
        return type(self)(financial=financial, intent=self.intent, violations=self.violations)


def authorize_checkout_execution(
    checkout: CheckoutSession,
    mandate: SpendingMandate,
    constraint_set: IntentConstraintSet | None,
    *,
    at: datetime,
) -> CheckoutExecutionAuthorization:
    """Answer whether both gates allow this checkout at `at`.

    Both are evaluated, always, even when the first one denies. A denied checkout is an
    ordinary outcome and a caller usually wants the whole picture rather than the first
    reason found, which is the same choice both evaluators already make internally.

    The constraint set is passed in rather than looked up, for the same reason the mandate
    is: this function reads nothing. Resolving the set through the mandate the checkout was
    quoted against is the service's job, and it is what stops an authorization from being
    paired with terms someone chose afterwards.

    Both decisions are made against what was recorded. The financial one reads the quoted
    totals and the semantic one reads the quote's own snapshot, so a catalog change since
    the quote cannot move either answer. The one thing execution reads live is stock, and
    that is not decided here.
    """
    financial = authorize_checkout(checkout, mandate, at=at)

    if constraint_set is None:
        # Absence of a semantic authorization is not a passed one. Nothing about this
        # depends on what the financial gate said, which is why it is reported beside it
        # rather than instead of it.
        return CheckoutExecutionAuthorization(
            financial=financial,
            intent=None,
            violations=(ExecutionAuthorizationViolation.INTENT_CONSTRAINTS_MISSING,),
        )

    return CheckoutExecutionAuthorization(
        financial=financial,
        intent=evaluate_intent_constraints(checkout, constraint_set),
    )
