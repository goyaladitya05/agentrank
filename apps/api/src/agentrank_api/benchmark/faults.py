"""Whose fault an interruption was, decided from evidence rather than reported by the executor.

Pure domain code. No SQLAlchemy, no HTTP, no service and no model, so the modules that decide a
fault can be imported by trusted code without dragging anything else along, and so that nothing
here can be reached from an executor by importing it.

This exists because `ErrorOrigin` used to live on the executor's own report, and that was the one
place this benchmark let the thing under test classify itself. The evaluator's rule everywhere
else is that an executor's account of its own reasoning never marks a mission, which is why
`AbstentionCode` is recorded and never read. An executor that returned `origin=HARNESS` rather
than letting a merchant refusal stand was marked ERRORED with no failure reason, left
`missions_failed` and every reason count untouched, and had its authored value counted as not
measured rather than as lost. Every dial its author would care about moved in the flattering
direction on a one line change. Claiming `origin=MERCHANT` is the same trick pointed the other
way: it turns the executor's own bug into a commerce readiness finding about somebody else.

So the origin is decided by whatever holds the tool boundary, from what actually happened at it,
and the rule is deliberately the one an HTTP transport can also apply:

```text
404 or 409, NotFoundError or ConflictError   a business answer. Not a fault at all
401, AuthenticationError                     HARNESS. Our credential, our problem
502, UpstreamError                           MERCHANT. Its dependency did not answer
5xx, anything else raised inside a call      MERCHANT. The surface failed rather than answered
transport error, timeout, unreadable body    MERCHANT. The surface did not answer
worker died, protocol violation, our bug     HARNESS
```

The first line is what makes the rest workable. A merchant refusing to quote for a variant it
does not sell is an answer and is measured as one, so a boundary that recorded every refusal as
a fault would report a commerce finding for every ordinary "no".

A future model saying "the merchant API failed" is text. It is not evidence that the merchant API
failed, and nothing in this module can be reached by writing that sentence.
"""

from dataclasses import dataclass
from enum import StrEnum


class FaultOrigin(StrEnum):
    """Which side of the boundary failed, which decides whether it is a finding or a fault.

    MERCHANT
        A merchant surface returned an error rather than an answer or a business refusal. A
        commerce readiness finding, and the mission is marked FAILED with MERCHANT_API_ERROR.

    HARNESS
        The benchmark's own machinery could not carry the mission out: the runner, the transport
        to the executor, the executor process, or a credential this side is responsible for. Not
        a fact about the merchant, and the mission is ERRORED rather than FAILED so that a flaky
        harness cannot look like a bad catalog.
    """

    MERCHANT = "MERCHANT"
    HARNESS = "HARNESS"


@dataclass(frozen=True, slots=True)
class ExecutionFault:
    """One interruption, as trusted code saw it.

    `detail` is written by whichever boundary observed the failure and never by the executor. It
    is diagnostic prose and nothing reads it to decide anything.

    `operation` names the buyer operation that was being carried out, when there was one. None
    means the failure was not inside a call to the merchant, which is itself evidence: it is the
    executor or the harness rather than the surface.
    """

    origin: FaultOrigin
    detail: str
    operation: str | None = None

    def __post_init__(self) -> None:
        if not self.detail.strip():
            raise ValueError("an execution fault must say what went wrong")
