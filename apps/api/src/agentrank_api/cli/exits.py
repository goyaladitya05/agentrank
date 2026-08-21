"""What an operator command tells its caller when it stops.

Its own module so that the command implementations can import it without importing the
package that imports them.
"""

from enum import IntEnum


class ExitCode(IntEnum):
    """The four ways a command can end that are worth telling apart.

    OK
        The command ran and reported what it found. That includes findings an operator will
        not like: a payment still unresolved after a query, a sweep in which nothing moved, an
        empty work list. None of those is a process failure, and treating them as one would
        leave a script unable to tell "it did not work" from "there was nothing to do".

    FAILED
        Something unexpected went wrong. Reached by an exception propagating rather than by
        anything returning it, so an operator gets the traceback. In a trusted local tool that
        is the useful behavior; a tidy one line message would cost the only evidence.

    USAGE
        The arguments were wrong. Two, because that is what argparse already exits with, and a
        second convention would only be a second thing to remember.

    NOT_FOUND
        The payment named does not exist.

    REFUSED
        The payment exists and its current state does not allow this operation. Distinct from
        NOT_FOUND and from FAILED because the next move differs: read the payment, and possibly
        run a different command against it.
    """

    OK = 0
    FAILED = 1
    USAGE = 2
    NOT_FOUND = 3
    REFUSED = 4
