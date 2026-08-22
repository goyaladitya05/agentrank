"""A buyer in a process with no database, and what the trusted side can say about it.

These tests spawn real subprocesses. That is the entire point: every in process arrangement of
private names is a convention, because anything holding an object can reach what the object
holds, and Python offers no way to prevent it. A process has a boundary that does not rest on
anybody's restraint, and asserting it means starting one.

Three groups. What the worker is given, which is asserted on the environment the runner builds
and again on a process refusing to start when that environment is polluted. What the worker can
reach, which is asserted by having it try. And what the trusted side concludes when the worker
dies, lies or takes too long, which is asserted from the process and the server rather than from
anything the worker said.
"""

import json
import os
import subprocess
import sys
import tempfile
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from agentrank_api.auth.service import MerchantCredentialService
from agentrank_api.auth.tokens import TokenMarker
from agentrank_api.benchmark.definitions import (
    AgentMissionBrief,
    BenchmarkMissionDefinition,
    BenchmarkSuiteDefinition,
    ExpectedOutcome,
    MissionOracle,
)
from agentrank_api.benchmark.endpoint import (
    CREDENTIAL_LABEL,
    LocalCommerceEndpoint,
    RequestLedger,
    ServedRequest,
)
from agentrank_api.benchmark.environment import BenchmarkEnvironmentService
from agentrank_api.benchmark.faults import FaultOrigin
from agentrank_api.benchmark.fixtures import BenchmarkFixture
from agentrank_api.benchmark.isolation import (
    ISOLATED_REFERENCE,
    WORKER_MODULE,
    IsolatedMissionExecutor,
    PaymentUnaccountedError,
)
from agentrank_api.benchmark.lifecycle import BenchmarkRunStatus, MissionRunStatus
from agentrank_api.benchmark.runner import BenchmarkRunService
from agentrank_api.benchmark.suites import BenchmarkSuiteService
from agentrank_api.benchmark.wire import MissionRequest
from agentrank_api.benchmark.worker import (
    PERMITTED_ENVIRONMENT,
    EnvironmentNotIsolatedError,
    require_isolated_environment,
    worker_environment,
)
from agentrank_api.commerce.catalog_fixture import SeedProduct, SeedVariant
from agentrank_api.commerce.repository import CatalogRepository
from agentrank_api.config import Settings
from agentrank_api.constraints.rules import ConstraintOperator
from agentrank_api.mandates.intent import AllowedCategory, MaxTotalAmount, RequiredAttribute
from agentrank_api.payments.fake import FakePaymentProvider

pytestmark = pytest.mark.anyio

CURRENCY = "INR"
PRICE = 100000
SLUG = "isolated-shop"
SKU = "IS-BLK"

CHARGERS = AllowedCategory("chargers")
BLACK = RequiredAttribute("color", "black", ConstraintOperator.EQ)

WORLD = BenchmarkFixture(
    key="isolated-catalog",
    version=1,
    merchant_slug=SLUG,
    merchant_name="Isolated Shop",
    products=(
        SeedProduct(
            external_id="IS-CHG",
            title="Charger",
            description=None,
            category="chargers",
            variants=(
                SeedVariant(
                    sku=SKU,
                    label="Black",
                    price_amount_minor=PRICE,
                    currency=CURRENCY,
                    inventory_quantity=3,
                    attributes={"color": "black"},
                ),
            ),
        ),
    ),
)


SUITE_KEY = "isolated-suite"


def brief(key: str = "one", *, budget_minor: int = PRICE) -> AgentMissionBrief:
    return AgentMissionBrief(
        key=key,
        objective="Buy one black charger.",
        budget=MaxTotalAmount(amount_minor=budget_minor, currency=CURRENCY),
        hard_constraints=(CHARGERS, BLACK),
    )


def suite_of(*keys: str) -> BenchmarkSuiteDefinition:
    """One suite where `unaffordable` is the control and everything else is purchasable."""
    return BenchmarkSuiteDefinition(
        key=SUITE_KEY,
        version=1,
        merchant_slug=SLUG,
        name="Isolated suite",
        missions=tuple(
            BenchmarkMissionDefinition(
                brief=brief(key, budget_minor=1 if key == "unaffordable" else PRICE),
                oracle=MissionOracle(
                    expected_outcome=(
                        ExpectedOutcome.NO_ACCEPTABLE_PURCHASE
                        if key == "unaffordable"
                        else ExpectedOutcome.PURCHASE_AVAILABLE
                    ),
                    simulated_value_amount_minor=0 if key == "unaffordable" else PRICE,
                ),
            )
            for key in keys
        ),
    )


@pytest.fixture
def served() -> RequestLedger:
    return RequestLedger()


@pytest.fixture
async def endpoint(
    catalog_settings: Settings, served: RequestLedger
) -> AsyncIterator[LocalCommerceEndpoint]:
    async with LocalCommerceEndpoint(
        catalog_settings, provider=FakePaymentProvider(), observer=served
    ) as running:
        yield running


async def prepared(session: AsyncSession) -> uuid.UUID:
    environments = BenchmarkEnvironmentService(session)
    environment = await environments.register(WORLD)
    await environments.prepare(WORLD)
    return environment.merchant_id


async def credential(session: AsyncSession, merchant_id: uuid.UUID) -> str:
    issued = await MerchantCredentialService(session).issue(
        merchant_id=merchant_id, label=CREDENTIAL_LABEL, marker=TokenMarker.DEVELOPMENT
    )
    return issued.token


def worker(
    *,
    stdin: str,
    environment: dict[str, str] | None = None,
    cwd: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a real worker process and hand back what it did.

    A subprocess rather than a call into `main`, because the properties here are about a process:
    what its environment contains, what it can reach, and what it leaves on its streams.

    The working directory is a fresh empty one, which is what `IsolatedMissionExecutor` gives a
    worker and what the worker's own settings check requires. Running these in the checkout would
    test the refusal that fires on a readable `.env` rather than the behaviour under test.
    """
    with tempfile.TemporaryDirectory(prefix="agentrank-test-worker-") as sandbox:
        return subprocess.run(  # noqa: S603  the interpreter and module are this repository's
            [sys.executable, "-m", WORKER_MODULE],
            input=stdin,
            capture_output=True,
            text=True,
            env=worker_environment(os.environ) if environment is None else environment,
            cwd=sandbox if cwd is None else cwd,
            timeout=120,
            check=False,
        )


# What the worker is given.


def test_the_environment_the_runner_builds_carries_no_credential() -> None:
    """An allowlist, asserted against a parent environment full of things it must drop."""
    parent = {
        "PATH": "/usr/bin",
        "HOME": "/home/somebody",
        "DATABASE_URL": "postgresql://user:secret@localhost/agentrank",
        "POSTGRES_PASSWORD": "secret",
        "RAZORPAY_KEY_SECRET": "secret",
        "AGENTRANK_ENVIRONMENT": "development",
        "AWS_SECRET_ACCESS_KEY": "secret",
        "SOMETHING_INVENTED_NEXT_YEAR": "secret",
    }

    built = worker_environment(parent)

    assert built == {"PATH": "/usr/bin", "HOME": "/home/somebody"}
    assert not any(value == "secret" for value in built.values())


def test_the_allowlist_contains_nothing_that_could_be_a_credential() -> None:
    """Read as a whole, because the risk is a name added later that looks harmless."""
    assert {
        "PATH",
        "HOME",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "TMPDIR",
        "PYTHONPATH",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
    } == PERMITTED_ENVIRONMENT


def test_a_worker_refuses_to_start_where_a_database_url_is_visible() -> None:
    """The second, independent statement of the same rule.

    The runner cannot pass a secret in by accident because it builds the environment by
    allowlist. This is what happens when somebody starts a worker by hand in a shell that has
    one, and it is a refusal rather than a warning.
    """
    with pytest.raises(EnvironmentNotIsolatedError) as raised:
        require_isolated_environment({"PATH": "/usr/bin", "DATABASE_URL": "postgresql://x/y"})

    assert raised.value.names == frozenset({"DATABASE_URL"})
    assert "postgresql" not in str(raised.value)


def test_the_refusal_names_variables_and_never_their_values() -> None:
    """A refusal that printed a connection string would be the leak it exists to prevent."""
    with pytest.raises(EnvironmentNotIsolatedError) as raised:
        require_isolated_environment({"RAZORPAY_KEY_SECRET": "rzp_test_supersecret"})

    assert "rzp_test_supersecret" not in str(raised.value)
    assert "RAZORPAY_KEY_SECRET" in str(raised.value)


def test_a_real_worker_process_exits_rather_than_running_with_a_database_url() -> None:
    """The whole rule, through a real process rather than through a function."""
    polluted = worker_environment(os.environ) | {"DATABASE_URL": "postgresql://user:pw@host/db"}

    finished = worker(stdin="", environment=polluted)

    assert finished.returncode == 2
    assert finished.stdout == ""
    assert "DATABASE_URL" in finished.stderr
    assert "user:pw" not in finished.stderr


def test_a_real_worker_process_has_no_database_url_of_its_own(tmp_path: object) -> None:
    """Asserted by making the process report its own environment rather than by trusting one.

    The probe runs the same allowlist the worker would run in, so what it prints is exactly what
    a worker started by `IsolatedMissionExecutor` can see.
    """
    probe = subprocess.run(
        [sys.executable, "-c", "import json,os;print(json.dumps(sorted(os.environ)))"],
        capture_output=True,
        text=True,
        env=worker_environment(os.environ),
        timeout=60,
        check=True,
    )

    visible = set(json.loads(probe.stdout))

    assert visible <= PERMITTED_ENVIRONMENT
    assert "DATABASE_URL" not in visible
    assert not [name for name in visible if "POSTGRES" in name or "RAZORPAY" in name]


# What the worker can reach.


async def test_a_whole_mission_runs_in_another_process(
    session: AsyncSession, endpoint: LocalCommerceEndpoint, served: RequestLedger
) -> None:
    """The reference buyer, in a process with no database, buying a real thing over HTTP."""
    merchant_id = await prepared(session)
    token = await credential(session, merchant_id)
    executor = IsolatedMissionExecutor(base_url=endpoint.base_url, token=token, served=served)

    executor.begin()
    observed = await executor(brief(), merchant_id=merchant_id)

    assert observed.purchased
    assert observed.selection is not None
    assert executor.fault() is None
    assert executor.payment_attempted()
    session.expire_all()
    variant = await CatalogRepository(session).get_variant_by_sku(merchant_id, SKU)
    assert variant is not None and variant.inventory_quantity == 2


def test_a_worker_cannot_build_settings_and_therefore_cannot_build_an_engine(
    tmp_path: Path,
) -> None:
    """The property the process boundary actually buys, asserted by trying it.

    The probe asks the application's own settings loader for a configuration, in exactly the
    environment and the working directory a worker gets. There is nothing for it to read, so it
    raises, and without settings there is no URL for `create_engine` to build from.

    This test is why the working directory is set at all. Run in the checkout it passes trivially
    and wrongly: `Settings` reads `.env` from the current directory, so the developer's
    `POSTGRES_PASSWORD` arrives from disk with an environment containing nothing but `PATH`.
    """
    source = (
        "from agentrank_api.config import get_settings\n"
        "try:\n"
        "    get_settings()\n"
        "except Exception as denied:\n"
        "    print(type(denied).__name__)\n"
        "else:\n"
        "    print('SETTINGS')\n"
    )

    probe = subprocess.run(  # noqa: S603  the interpreter is this repository's
        [sys.executable, "-c", source],
        capture_output=True,
        text=True,
        env=worker_environment(os.environ),
        cwd=tmp_path,
        timeout=60,
        check=True,
    )

    assert probe.stdout.strip() != "SETTINGS"


def test_a_worker_started_where_a_settings_file_is_readable_refuses(tmp_path: Path) -> None:
    """The second half of the same rule, and the one that catches the directory being wrong.

    A worker whose working directory holds a `.env` can configure a database however carefully
    its environment was built, so it exits rather than running.
    """
    (tmp_path / ".env").write_text("POSTGRES_PASSWORD=whatever\n", encoding="utf-8")

    finished = subprocess.run(  # noqa: S603  the interpreter and the module are this repository's
        [sys.executable, "-m", WORKER_MODULE],
        input="",
        capture_output=True,
        text=True,
        env=worker_environment(os.environ),
        cwd=tmp_path,
        timeout=120,
        check=False,
    )

    assert finished.returncode == 2
    assert finished.stdout == ""
    assert "settings" in finished.stderr


def test_the_worker_module_never_spells_a_benchmark_run_or_an_oracle() -> None:
    """The worker imports the buyer side of the package and nothing that knows an answer."""
    from pathlib import Path

    source = Path(
        __import__("agentrank_api.benchmark.worker", fromlist=["worker"]).__file__ or ""
    ).read_text(encoding="utf-8")

    for forbidden in (
        "MissionOracle",
        "ExpectedOutcome",
        "BenchmarkMissionDefinition",
        "evaluate_mission",
        "BenchmarkRunService",
        "BenchmarkRunRepository",
        "expected_outcome",
        "simulated_value_amount_minor",
    ):
        assert forbidden not in source


async def test_a_mission_request_carries_no_oracle_and_has_nowhere_to_put_one(
    session: AsyncSession, endpoint: LocalCommerceEndpoint
) -> None:
    """Asserted on the document that actually crosses, field by field.

    A caller that meant well and passed the wrong argument cannot hand an oracle across this
    boundary, because there is no field for one to travel in.
    """
    merchant_id = await prepared(session)
    token = await credential(session, merchant_id)
    request = MissionRequest(
        brief=brief(), merchant_id=merchant_id, base_url=endpoint.base_url, token=token
    )

    payload = request.to_payload()

    assert set(payload) == {"protocol", "strategy", "merchant_id", "base_url", "token", "brief"}
    document = json.dumps(payload)
    for forbidden in (
        "expected_outcome",
        "simulated_value",
        "oracle",
        "PURCHASE_AVAILABLE",
        "NO_ACCEPTABLE_PURCHASE",
        "suite",
        "run_id",
    ):
        assert forbidden not in document


async def test_the_credential_decides_which_shop_and_not_the_identifier_it_was_given(
    session: AsyncSession, endpoint: LocalCommerceEndpoint, served: RequestLedger
) -> None:
    """A worker cannot shop anywhere its credential does not reach, whatever it is told.

    The merchant identifier in the request is what the buyer believes and the credential is what
    the server enforces. Told to shop at a merchant that does not exist, this worker holds a
    credential for the one that does, so every request it makes is answered for that merchant
    and it can reach nothing else. That is the Phase 1H property doing the work rather than the
    executor's own check, which is the right way round: the executor is the untrusted half.
    """
    mine = await prepared(session)
    token = await credential(session, mine)
    stranger = uuid.uuid7()
    executor = IsolatedMissionExecutor(base_url=endpoint.base_url, token=token, served=served)

    executor.begin()
    observed = await executor(brief(), merchant_id=stranger)

    assert observed.selection is not None
    session.expire_all()
    bought = await CatalogRepository(session).get_variant_by_sku(mine, SKU)
    assert bought is not None and observed.selection.variant_id == bought.id
    # And the report says which merchant it believed it was at, which is what the evaluator
    # compares against the run's own merchant and marks WRONG_MERCHANT on.
    assert observed.merchant_id == stranger


# What the trusted side concludes.


async def test_a_worker_that_never_starts_is_a_harness_fault(
    session: AsyncSession, served: RequestLedger
) -> None:
    """Nothing the worker says is involved, because there is no worker to say anything."""
    merchant_id = await prepared(session)
    executor = IsolatedMissionExecutor(
        base_url="http://127.0.0.1:1",
        token="ar_dev_" + "0" * 32 + "_" + "0" * 64,
        served=served,
        interpreter="/nonexistent/python",
    )

    executor.begin()
    observed = await executor(brief(), merchant_id=merchant_id)

    fault = executor.fault()
    assert fault is not None
    assert fault.origin is FaultOrigin.HARNESS
    assert observed.selection is None


async def test_a_worker_that_takes_too_long_is_killed_and_attributed(
    session: AsyncSession, served: RequestLedger
) -> None:
    """A benchmark that waited forever would produce no result at all, which is worse."""
    merchant_id = await prepared(session)
    executor = IsolatedMissionExecutor(
        base_url="http://127.0.0.1:1",
        token="ar_dev_" + "0" * 32 + "_" + "0" * 64,
        served=served,
        timeout=0.5,
        interpreter=sys.executable,
    )

    executor.begin()
    observed = await executor(brief(), merchant_id=merchant_id)

    fault = executor.fault()
    assert fault is not None
    assert fault.origin is FaultOrigin.HARNESS
    assert observed.selection is None


async def test_a_worker_that_dispatched_a_payment_and_vanished_stops_the_run(
    session: AsyncSession, served: RequestLedger
) -> None:
    """The one failure that must never be recorded and carried on from.

    The server saw a request to the payment route, so money may have moved. Recording ERRORED
    would say the harness could not carry the mission out and would move the mission's value out
    of lost demand, which is the wrong thing to say about a mission that may have bought
    something. It raises instead, and the mission stays RUNNING.
    """
    merchant_id = await prepared(session)
    served.record(
        ServedRequest(method="POST", path="/api/v1/commerce/checkouts/x/payments", status=200)
    )
    executor = IsolatedMissionExecutor(
        base_url="http://127.0.0.1:1",
        token="ar_dev_" + "0" * 32 + "_" + "0" * 64,
        served=served,
        interpreter="/nonexistent/python",
    )

    with pytest.raises(PaymentUnaccountedError) as raised:
        await executor(brief("paid-and-vanished"), merchant_id=merchant_id)

    assert raised.value.mission_key == "paid-and-vanished"


def test_a_worker_that_speaks_nonsense_is_refused_rather_than_believed() -> None:
    """A protocol violation is a fault on the harness side, not a report."""
    finished = worker(stdin="this is not a mission request")

    assert finished.returncode == 3
    assert finished.stdout == ""


def test_a_worker_given_nothing_says_nothing_on_standard_output() -> None:
    """Standard output carries a report or is empty. A diagnostic there would be a violation."""
    finished = worker(stdin="")

    assert finished.returncode == 3
    assert finished.stdout == ""
    assert finished.stderr != ""


async def test_a_merchant_that_answers_with_a_failure_is_attributed_to_the_merchant(
    session: AsyncSession, served: RequestLedger
) -> None:
    """The server side record is what says so, not the worker's account of what it got."""
    del session
    served.begin()
    served.record(ServedRequest(method="POST", path="/api/v1/commerce/products/search", status=503))
    executor = IsolatedMissionExecutor(
        base_url="http://127.0.0.1:1",
        token="ar_dev_" + "0" * 32 + "_" + "0" * 64,
        served=served,
    )

    fault = executor.fault()

    assert fault is not None
    assert fault.origin is FaultOrigin.MERCHANT
    assert fault.operation == "/api/v1/commerce/products/search"


async def test_the_witness_forgets_the_previous_mission(
    session: AsyncSession, served: RequestLedger
) -> None:
    del session
    served.record(ServedRequest(method="POST", path="/anything", status=500))
    executor = IsolatedMissionExecutor(
        base_url="http://127.0.0.1:1",
        token="ar_dev_" + "0" * 32 + "_" + "0" * 64,
        served=served,
    )
    assert executor.fault() is not None

    executor.begin()

    assert executor.fault() is None
    assert not executor.payment_attempted()


def test_the_isolated_executor_is_not_the_in_process_one_by_identity() -> None:
    """Two transports with different failure modes are two measurements, not one.

    A run produced through the isolated boundary records `reference-isolated`, so it can never
    be compared with an in process `reference-v1` run as though the two were the same thing.
    """
    assert ISOLATED_REFERENCE.kind == "reference-isolated"
    assert ISOLATED_REFERENCE.label == "reference-isolated-v1"


# A whole suite, through the boundary a model will use.


async def test_a_whole_suite_runs_through_the_isolated_boundary(
    session: AsyncSession, endpoint: LocalCommerceEndpoint, served: RequestLedger
) -> None:
    """Three missions, three processes, and a run that records what actually did the shopping.

    Two purchasable missions and one control, so the boundary is exercised on the path that buys
    and on the path that declines. The recorded executor identity is `reference-isolated`, which
    is what stops a run produced this way being compared with an in process one as though the
    two were the same measurement.
    """
    merchant_id = await prepared(session)
    await BenchmarkSuiteService(session).publish(suite_of("first", "unaffordable", "second"))
    token = await credential(session, merchant_id)
    executor = IsolatedMissionExecutor(base_url=endpoint.base_url, token=token, served=served)

    finished = await BenchmarkRunService(session).run_suite(
        executor,
        suite_key=SUITE_KEY,
        suite_version=1,
        fixture=WORLD,
        witness=executor,
    )

    assert finished.status is BenchmarkRunStatus.COMPLETED
    assert {result.mission.mission_key: result.status for result in finished.mission_runs} == {
        "first": MissionRunStatus.SUCCEEDED,
        "unaffordable": MissionRunStatus.ABSTAINED,
        "second": MissionRunStatus.SUCCEEDED,
    }
    assert finished.executor_kind == "reference-isolated"
    assert finished.executor_version == 1
    assert all(result.primary_failure_reason is None for result in finished.mission_runs)


async def test_every_mission_gets_a_process_that_knows_nothing_about_the_last_one(
    session: AsyncSession, endpoint: LocalCommerceEndpoint, served: RequestLedger
) -> None:
    """One process per mission is what makes a context carrying executor impossible here.

    The shelf holds three units and each mission wants one, so a run that leaked state would
    still succeed. What this asserts is the stronger structural fact: the two successful
    missions were carried out by processes that could not have shared anything, because each
    was given one brief on standard input and then exited.
    """
    merchant_id = await prepared(session)
    await BenchmarkSuiteService(session).publish(suite_of("first", "second"))
    token = await credential(session, merchant_id)
    executor = IsolatedMissionExecutor(base_url=endpoint.base_url, token=token, served=served)

    finished = await BenchmarkRunService(session).run_suite(
        executor, suite_key=SUITE_KEY, suite_version=1, fixture=WORLD, witness=executor
    )

    # The witness was reset before each mission, so what it holds now describes the last one
    # only. A ledger carrying the whole run would attribute mission one's faults to mission two.
    assert executor.payment_attempted()
    assert len([served for served in served.served if served.path.endswith("/payments")]) == 1
    assert all(result.status is MissionRunStatus.SUCCEEDED for result in finished.mission_runs)
