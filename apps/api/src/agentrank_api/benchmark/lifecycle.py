"""The states a benchmark run and a mission within it can be in.

Pure domain code, separated from the tables that store it so that the evaluator can decide a
status without importing SQLAlchemy. The vocabulary belongs to the benchmark rather than to its
persistence, and a pure function that has to reach into an ORM module for an enumeration is one
import away from reaching into it for a row.
"""

from enum import StrEnum


class BenchmarkRunStatus(StrEnum):
    """Where one execution of one suite against one merchant has got to.

    Four, and each names something a reader has to be able to tell apart.

    PENDING
        Every mission run exists and none has started. The shape of the run is already fixed by
        the suite, so a run always has exactly as many mission runs as its suite has missions.

    RUNNING
        Execution has begun.

    COMPLETED
        Every mission run reached a terminal state. This is the only status under which a report
        describes the whole workload.

    ABORTED
        Execution stopped and some missions never reached a terminal state. Its own value rather
        than COMPLETED with fewer results, because a partial run presented as a complete one
        would report a task completion rate over a denominator nobody chose.
    """

    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    ABORTED = "ABORTED"


class MissionRunStatus(StrEnum):
    """What became of one mission in one run.

    PENDING
        Recorded and not started.

    RUNNING
        Started, with no outcome yet.

    SUCCEEDED
        A compliant purchase completed, on a mission whose ground truth said one was available.
        This is the only status that counts as task completion, and the only one under which
        simulated GMV is captured.

    FAILED
        An attempt was made and did not produce the expected outcome. Always carries a reason.

    ABSTAINED
        The executor deliberately declined to buy. Its own status rather than a kind of failure,
        because declining is correct on a mission where nothing acceptable is for sale and is a
        finding on a mission where something is, and one status covering both would make a
        cautious agent and a broken catalog look the same. Correctness is recorded as the
        presence or absence of a failure reason rather than as a second status.

    ERRORED
        The harness itself could not carry the mission out. Deliberately not FAILED: an
        infrastructure fault is not a fact about the merchant, and counting one as a commerce
        failure would make a flaky runner look like a bad catalog. A merchant surface returning
        an error is the other case and is a FAILED with MERCHANT_API_ERROR.
    """

    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    ABSTAINED = "ABSTAINED"
    ERRORED = "ERRORED"


TERMINAL_MISSION_STATUSES = frozenset(
    {
        MissionRunStatus.SUCCEEDED,
        MissionRunStatus.FAILED,
        MissionRunStatus.ABSTAINED,
        MissionRunStatus.ERRORED,
    }
)

TERMINAL_RUN_STATUSES = frozenset({BenchmarkRunStatus.COMPLETED, BenchmarkRunStatus.ABORTED})
