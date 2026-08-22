"""Whose fault an interruption was, and why the executor is not the one who decides.

`ErrorOrigin` used to be a field on `ObservedResult`, so the thing under test classified its own
failures. Returning HARNESS put the mission in ERRORED, which carries no failure reason, leaves
`missions_failed` and every reason count untouched, and counts the mission's authored value as
not measured rather than as lost. Returning MERCHANT turned the executor's own bug into a
commerce readiness finding about somebody else's shop. Both were one line changes.

Attribution is now decided at the tool boundary from what the merchant surface actually did, and
these tests are written to fail if that ever stops being true. The three that matter most are the
ones asserting a negative: a business refusal is not a fault, an executor's prose about a
catastrophe classifies nothing, and there is no field on the report that could carry an origin.
"""

import ast
import importlib
import inspect
import uuid
from pathlib import Path
from types import ModuleType
from typing import Any
from unittest.mock import patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agentrank_api.benchmark import evaluation
from agentrank_api.benchmark.buyer import MerchantBuyerSurface
from agentrank_api.benchmark.definitions import (
    AgentMissionBrief,
    BenchmarkMissionDefinition,
    BenchmarkSuiteDefinition,
    ExpectedOutcome,
    MissionOracle,
)
from agentrank_api.benchmark.environment import BenchmarkEnvironmentService
from agentrank_api.benchmark.evaluation import evaluate_mission, evaluator_version
from agentrank_api.benchmark.failures import FailureReason
from agentrank_api.benchmark.faults import (
    CATALOG_REFUSALS,
    CATALOG_RESOURCES,
    ExecutionFault,
    FaultOrigin,
)
from agentrank_api.benchmark.fixtures import BenchmarkFixture
from agentrank_api.benchmark.lifecycle import MissionRunStatus
from agentrank_api.benchmark.observation import ObservedError, ObservedResult
from agentrank_api.benchmark.reference_executor import ReferenceMissionExecutor
from agentrank_api.benchmark.runner import BenchmarkRunService
from agentrank_api.benchmark.suites import BenchmarkSuiteService
from agentrank_api.benchmark.tools import (
    BuyerArgumentError,
    BuyerOperation,
    MeasuredBuyerSurface,
    ToolCall,
    ToolLedger,
    ToolOutcome,
)
from agentrank_api.commerce.catalog_fixture import SeedProduct, SeedVariant
from agentrank_api.constraints.rules import ConstraintOperator
from agentrank_api.errors import AuthenticationError, ConflictError, NotFoundError, UpstreamError
from agentrank_api.mandates.intent import AllowedCategory, MaxTotalAmount, RequiredAttribute
from agentrank_api.payments.admission import AdmissionRefusal
from agentrank_api.payments.fake import FakePaymentProvider

pytestmark = pytest.mark.anyio

CURRENCY = "INR"
PRICE = 100000
SLUG = "attribution-shop"
SUITE_KEY = "attribution-suite"

CHARGERS = AllowedCategory("chargers")
BLACK = RequiredAttribute("color", "black", ConstraintOperator.EQ)

WORLD = BenchmarkFixture(
    key="attribution-catalog",
    version=1,
    merchant_slug=SLUG,
    merchant_name="Attribution Shop",
    products=(
        SeedProduct(
            external_id="AS-CHG",
            title="Charger",
            description=None,
            category="chargers",
            variants=(
                SeedVariant(
                    sku="AS-BLK",
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


def mission_brief(key: str = "one") -> AgentMissionBrief:
    return AgentMissionBrief(
        key=key,
        objective="Buy one black charger.",
        budget=MaxTotalAmount(amount_minor=PRICE, currency=CURRENCY),
        hard_constraints=(CHARGERS, BLACK),
    )


def suite_of(*keys: str) -> BenchmarkSuiteDefinition:
    return BenchmarkSuiteDefinition(
        key=SUITE_KEY,
        version=1,
        merchant_slug=SLUG,
        name="Attribution suite",
        missions=tuple(
            BenchmarkMissionDefinition(
                brief=mission_brief(key),
                oracle=MissionOracle(
                    expected_outcome=ExpectedOutcome.PURCHASE_AVAILABLE,
                    simulated_value_amount_minor=PRICE,
                ),
            )
            for key in keys
        ),
    )


async def prepared(session: AsyncSession) -> uuid.UUID:
    environments = BenchmarkEnvironmentService(session)
    environment = await environments.register(WORLD)
    await environments.prepare(WORLD)
    return environment.merchant_id


async def watched(
    session: AsyncSession,
    factory: async_sessionmaker[AsyncSession],
    surface: type[MerchantBuyerSurface] = MerchantBuyerSurface,
) -> tuple[ToolLedger, ReferenceMissionExecutor, uuid.UUID]:
    """A reference executor whose calls pass through a ledger it cannot see."""
    merchant_id = await prepared(session)
    ledger = ToolLedger()
    inner = surface(factory, merchant_id=merchant_id, provider=FakePaymentProvider())
    measured = MeasuredBuyerSurface(inner, ledger)
    return ledger, ReferenceMissionExecutor(measured), merchant_id


# A refusal is an answer.


async def test_a_business_refusal_is_not_a_fault(
    session: AsyncSession, factory: async_sessionmaker[AsyncSession]
) -> None:
    """The line that makes the rest workable, and the same line HTTP draws at 4xx.

    A merchant declining to quote for something it does not sell is most of what a benchmark
    measures. A boundary that recorded every refusal as a fault would report a commerce
    readiness finding for every ordinary no.
    """

    class Refusing(MerchantBuyerSurface):
        async def create_checkout(self, request: Any) -> Any:
            raise ConflictError("insufficient_inventory", "one available, two requested")

    ledger, executor, merchant_id = await watched(session, factory, Refusing)

    await executor(mission_brief(), merchant_id=merchant_id)

    assert ledger.fault() is None
    refused = [call for call in ledger.calls if call.outcome is ToolOutcome.REFUSED]
    assert [call.detail for call in refused] == ["insufficient_inventory"]


async def test_a_not_found_is_a_refusal_and_names_the_resource(
    session: AsyncSession, factory: async_sessionmaker[AsyncSession]
) -> None:
    class Missing(MerchantBuyerSurface):
        async def create_checkout(self, request: Any) -> Any:
            raise NotFoundError("variant", "whatever")

    ledger, executor, merchant_id = await watched(session, factory, Missing)

    await executor(mission_brief(), merchant_id=merchant_id)

    assert ledger.fault() is None
    assert any(call.detail == "variant was not found" for call in ledger.calls)


# A failure is a fault, and which side it belongs to is read off the failure.


async def test_a_surface_that_fails_rather_than_answers_is_the_merchants(
    session: AsyncSession, factory: async_sessionmaker[AsyncSession]
) -> None:
    """In process this is an exception nothing modelled. Over the wire it is a 5xx."""

    class Broken(MerchantBuyerSurface):
        async def search_products(self, request: Any) -> Any:
            raise RuntimeError("the catalog query blew up")

    ledger, executor, merchant_id = await watched(session, factory, Broken)

    with pytest.raises(RuntimeError):
        await executor(mission_brief(), merchant_id=merchant_id)

    fault = ledger.fault()
    assert fault is not None
    assert fault.origin is FaultOrigin.MERCHANT
    assert fault.operation == BuyerOperation.SEARCH_PRODUCTS.value


async def test_a_merchant_dependency_that_did_not_answer_is_the_merchants(
    session: AsyncSession, factory: async_sessionmaker[AsyncSession]
) -> None:
    class Upstream(MerchantBuyerSurface):
        async def search_products(self, request: Any) -> Any:
            raise UpstreamError("provider_timeout", "the catalog's dependency timed out")

    ledger, executor, merchant_id = await watched(session, factory, Upstream)

    await executor(mission_brief(), merchant_id=merchant_id)

    fault = ledger.fault()
    assert fault is not None
    assert fault.origin is FaultOrigin.MERCHANT
    assert fault.detail == "provider_timeout"


async def test_an_unauthorized_call_is_the_harnesss(
    session: AsyncSession, factory: async_sessionmaker[AsyncSession]
) -> None:
    """Our credential, our problem.

    A benchmark that reported its own expired key as a merchant API error would publish a
    commerce readiness finding about a shop that answered correctly.
    """

    class Unauthorized(MerchantBuyerSurface):
        async def search_products(self, request: Any) -> Any:
            raise AuthenticationError

    ledger, executor, merchant_id = await watched(session, factory, Unauthorized)

    await executor(mission_brief(), merchant_id=merchant_id)

    fault = ledger.fault()
    assert fault is not None
    assert fault.origin is FaultOrigin.HARNESS


async def test_the_first_failure_is_the_one_attributed(
    session: AsyncSession, factory: async_sessionmaker[AsyncSession]
) -> None:
    """An executor stops at its first refusal, so a later failure only happened because the
    first was swallowed, and naming it would name the wrong call."""
    ledger = ToolLedger()
    ledger.record(_failed(BuyerOperation.SEARCH_PRODUCTS, FaultOrigin.MERCHANT, "the first one"))
    ledger.record(_failed(BuyerOperation.CREATE_CHECKOUT, FaultOrigin.HARNESS, "the second one"))

    fault = ledger.fault()

    assert fault is not None
    assert fault.detail == "the first one"


def _failed(operation: BuyerOperation, origin: FaultOrigin, detail: str) -> ToolCall:
    return ToolCall(operation=operation, outcome=ToolOutcome.FAILED, detail=detail, origin=origin)


# What the executor can and cannot say about itself.


def test_an_observed_error_has_no_origin_to_set() -> None:
    """Structural rather than behavioural. There is no field to put a claim in."""
    assert set(ObservedError.__dataclass_fields__) == {"detail"}


def test_an_executors_account_of_a_catastrophe_classifies_nothing() -> None:
    """A future model saying the merchant API failed is text, not evidence."""
    merchant = uuid.uuid7()
    defined = BenchmarkMissionDefinition(
        brief=mission_brief(),
        oracle=MissionOracle(
            expected_outcome=ExpectedOutcome.PURCHASE_AVAILABLE,
            simulated_value_amount_minor=PRICE,
        ),
    )
    observed = ObservedResult(
        merchant_id=merchant, error=ObservedError(detail="the merchant API failed")
    )

    result = evaluate_mission(defined, observed, merchant_id=merchant)

    assert result.status is MissionRunStatus.FAILED
    assert FailureReason.MERCHANT_API_ERROR not in result.failure_reasons


def test_a_ledger_forgets_the_previous_mission() -> None:
    """Without this a fault from mission one is attributed to every mission after it."""
    ledger = ToolLedger()
    ledger.record(_failed(BuyerOperation.SEARCH_PRODUCTS, FaultOrigin.MERCHANT, "mission one"))
    ledger.note_payment_attempt()

    ledger.begin()

    assert ledger.fault() is None
    assert not ledger.payment_attempted()


async def test_a_payment_call_is_remembered_even_when_it_never_returns(
    session: AsyncSession, factory: async_sessionmaker[AsyncSession]
) -> None:
    """A payment that vanished may have moved money.

    This is what stops a crashed mission being recorded and carried on from: the witness knows a
    payment was dispatched even though nothing came back to say so.
    """

    class Vanishing(MerchantBuyerSurface):
        async def complete_checkout(self, checkout_id: uuid.UUID, request: Any) -> Any:
            raise RuntimeError("the payment call never returned")

    ledger, executor, merchant_id = await watched(session, factory, Vanishing)

    with pytest.raises(RuntimeError):
        await executor(mission_brief(), merchant_id=merchant_id)

    assert ledger.payment_attempted()
    fault = ledger.fault()
    assert fault is not None
    assert fault.operation == BuyerOperation.COMPLETE_CHECKOUT.value


# End to end, through the runner.


async def test_a_merchant_fault_reaches_the_recorded_result(
    session: AsyncSession, factory: async_sessionmaker[AsyncSession]
) -> None:
    """The whole chain: surface fails, boundary attributes, runner passes, evaluator marks."""

    class Upstream(MerchantBuyerSurface):
        async def search_products(self, request: Any) -> Any:
            raise UpstreamError("provider_timeout", "the catalog's dependency timed out")

    merchant_id = await prepared(session)
    await BenchmarkSuiteService(session).publish(suite_of("one"))
    ledger = ToolLedger()
    surface = MeasuredBuyerSurface(
        Upstream(factory, merchant_id=merchant_id, provider=FakePaymentProvider()), ledger
    )

    finished = await BenchmarkRunService(session).run_suite(
        ReferenceMissionExecutor(surface),
        suite_key=SUITE_KEY,
        suite_version=1,
        fixture=WORLD,
        witness=ledger,
    )

    result = finished.mission_runs[0]
    assert result.status is MissionRunStatus.FAILED
    assert result.primary_failure_reason is FailureReason.MERCHANT_API_ERROR


async def test_without_a_witness_no_interruption_is_attributed(
    session: AsyncSession, factory: async_sessionmaker[AsyncSession]
) -> None:
    """A run with nothing watching has no evidence, so it attributes nothing.

    The alternative is believing the executor, which is what this whole change removes. The
    mission is still marked, on what was actually observed.
    """

    class Upstream(MerchantBuyerSurface):
        async def search_products(self, request: Any) -> Any:
            raise UpstreamError("provider_timeout", "the catalog's dependency timed out")

    merchant_id = await prepared(session)
    await BenchmarkSuiteService(session).publish(suite_of("one"))
    surface = Upstream(factory, merchant_id=merchant_id, provider=FakePaymentProvider())

    finished = await BenchmarkRunService(session).run_suite(
        ReferenceMissionExecutor(surface), suite_key=SUITE_KEY, suite_version=1, fixture=WORLD
    )

    result = finished.mission_runs[0]
    assert result.status is MissionRunStatus.FAILED
    assert result.primary_failure_reason is FailureReason.DISCOVERY_FAILURE


async def test_a_harness_fault_errors_the_mission_rather_than_failing_it(
    session: AsyncSession, factory: async_sessionmaker[AsyncSession]
) -> None:
    """ERRORED says nothing about the merchant, which is why only trusted evidence reaches it."""
    merchant_id = await prepared(session)
    await BenchmarkSuiteService(session).publish(suite_of("one"))
    service = BenchmarkRunService(session)
    run = await service.start_run(suite_key=SUITE_KEY, suite_version=1, merchant_slug=SLUG)
    await service.start_mission(run.id, "one", merchant_id=merchant_id)

    recorded = await service.record_result(
        run.id,
        "one",
        ObservedResult(merchant_id=merchant_id),
        merchant_id=merchant_id,
        fault=ExecutionFault(origin=FaultOrigin.HARNESS, detail="the worker died"),
    )

    assert recorded.status is MissionRunStatus.ERRORED
    assert recorded.primary_failure_reason is None


def executor_modules() -> list[ModuleType]:
    """Every module in the benchmark package that implements an executor.

    Discovered rather than listed, because the module these checks exist for is the one nobody
    has written yet. The naming convention is the contract: an executor lives in a module whose
    name ends in `_executor`.
    """
    package = importlib.import_module("agentrank_api.benchmark")
    root = Path(package.__file__ or "").parent
    found = [
        importlib.import_module(f"agentrank_api.benchmark.{path.stem}")
        for path in sorted(root.glob("*_executor.py"))
    ]
    assert found, "no executor module was discovered, so this check would pass vacuously"
    return found


@pytest.mark.parametrize("module", executor_modules(), ids=lambda module: module.__name__)
def test_an_executor_cannot_name_the_thing_that_attributes_its_faults(module: ModuleType) -> None:
    """Read from the source, so a lazy import inside a function is caught too.

    The import allowlist in tests/test_reference_executor.py already refuses these modules. This
    says why it matters: an executor that could reach `FaultOrigin` or a ledger would be one
    attribute access away from writing its own attribution, and the whole point of moving the
    decision out of `ObservedResult` was that there is nowhere left for it to write one.
    """
    source = Path(inspect.getfile(module)).read_text(encoding="utf-8")
    spelled = (
        {node.id for node in ast.walk(ast.parse(source)) if isinstance(node, ast.Name)}
        | {node.attr for node in ast.walk(ast.parse(source)) if isinstance(node, ast.Attribute)}
        | {
            alias.name
            for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.Import | ast.ImportFrom)
            for alias in node.names
        }
        | {
            node.module
            for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.ImportFrom) and node.module is not None
        }
    )

    forbidden = {
        "FaultOrigin",
        "ExecutionFault",
        "ExecutionWitness",
        "ToolLedger",
        "ToolCall",
        "MeasuredBuyerSurface",
        "agentrank_api.benchmark.faults",
        "agentrank_api.benchmark.tools",
    }
    assert spelled & forbidden == set()


# What the executor cannot make the boundary say.


async def test_an_executor_cannot_choose_which_exception_the_boundary_observes(
    session: AsyncSession, factory: async_sessionmaker[AsyncSession]
) -> None:
    """The forged origin, found by an independent review and closed here.

    The surface's own code runs the argument: `MerchantBuyerSurface.search_products` calls
    `request.to_criteria(...)` on whatever it is handed. An executor passing an object whose
    method raises `AuthenticationError` therefore chose which origin was attributed, and HARNESS
    is the flattering one: ERRORED carries no failure reason and moves the mission's value out of
    lost demand.

    The argument is type checked before anything is watched, so the refusal is a caller that
    cannot call rather than a fault about anybody.
    """
    merchant_id = await prepared(session)
    ledger = ToolLedger()
    surface = MeasuredBuyerSurface(
        MerchantBuyerSurface(factory, merchant_id=merchant_id, provider=FakePaymentProvider()),
        ledger,
    )

    class Trap:
        limit = 10

        def to_criteria(self, merchant: uuid.UUID) -> object:
            raise AuthenticationError

    with pytest.raises(BuyerArgumentError):
        await surface.search_products(Trap())  # type: ignore[arg-type]

    assert ledger.calls == ()
    assert ledger.fault() is None


async def test_a_hallucinated_identifier_is_not_a_merchant_fault_either(
    session: AsyncSession, factory: async_sessionmaker[AsyncSession]
) -> None:
    """The same hole through the other door.

    A string where an identifier belongs reached SQLAlchemy and came back as an exception
    nothing modelled, which the boundary attributed to the merchant. That fabricates a commerce
    readiness finding out of a caller that cannot call.
    """
    merchant_id = await prepared(session)
    ledger = ToolLedger()
    surface = MeasuredBuyerSurface(
        MerchantBuyerSurface(factory, merchant_id=merchant_id, provider=FakePaymentProvider()),
        ledger,
    )

    with pytest.raises(BuyerArgumentError):
        await surface.get_product("not-a-uuid")  # type: ignore[arg-type]

    assert ledger.fault() is None


# Which refusals are answers.


def test_a_catalog_refusal_is_an_answer_and_anything_else_is_this_callers_own_state() -> None:
    """The split, as data rather than as a status code.

    Both arrive as a 404 or a 409, so the status cannot separate them. A variant the merchant
    does not sell is a measurement; a mandate this execution created moments ago having vanished
    is the harness in a state it should not be in, and marking that as a buyer reasoning failure
    is what happened before the split existed.
    """
    assert "variant" in CATALOG_RESOURCES
    assert "product" in CATALOG_RESOURCES
    assert "mandate" not in CATALOG_RESOURCES
    assert "checkout" not in CATALOG_RESOURCES
    assert "insufficient_inventory" in CATALOG_REFUSALS
    assert "mandate_already_consumed" not in CATALOG_REFUSALS


async def test_a_missing_mandate_is_attributed_to_the_harness_rather_than_measured(
    session: AsyncSession, factory: async_sessionmaker[AsyncSession]
) -> None:
    """A mandate this execution created a second ago cannot be a fact about the merchant."""

    class MandateVanished(MerchantBuyerSurface):
        async def create_checkout(self, request: Any) -> Any:
            raise NotFoundError("mandate", str(uuid.uuid7()))

    ledger, executor, merchant_id = await watched(session, factory, MandateVanished)

    await executor(mission_brief(), merchant_id=merchant_id)

    fault = ledger.fault()
    assert fault is not None
    assert fault.origin is FaultOrigin.HARNESS


async def test_an_admission_refusal_that_is_not_a_denial_is_the_harnesss(
    session: AsyncSession, factory: async_sessionmaker[AsyncSession]
) -> None:
    """Read off the merchant's own answer, by trusted code, and not out of the report.

    An expired reservation is wall clock dependent, so before this the same suite on a slow
    machine published one more buyer reasoning failure and up to a mission's whole authored
    value as demand the merchant lost. Nothing about that was a fact about the merchant.
    """
    merchant_id = await prepared(session)
    ledger = ToolLedger()

    class Consumed(MerchantBuyerSurface):
        async def complete_checkout(self, checkout_id: uuid.UUID, request: Any) -> Any:
            paid = await super().complete_checkout(checkout_id, request)
            return paid.model_copy(
                update={
                    "admitted": False,
                    "created": False,
                    "refusal": AdmissionRefusal.MANDATE_ALREADY_CONSUMED,
                    "attempt": None,
                }
            )

    surface = MeasuredBuyerSurface(
        Consumed(factory, merchant_id=merchant_id, provider=FakePaymentProvider()), ledger
    )
    await ReferenceMissionExecutor(surface)(mission_brief(), merchant_id=merchant_id)

    fault = ledger.fault()
    assert fault is not None
    assert fault.origin is FaultOrigin.HARNESS
    assert fault.detail == AdmissionRefusal.MANDATE_ALREADY_CONSUMED.value


async def test_an_authorization_denial_stays_a_finding_rather_than_a_fault(
    session: AsyncSession, factory: async_sessionmaker[AsyncSession]
) -> None:
    """The one admission refusal that is the safety layer working.

    A benchmark that recorded this as a harness fault would stop measuring the thing it most
    exists to measure, which is what happens when a buyer is refused.
    """
    merchant_id = await prepared(session)
    ledger = ToolLedger()

    class Denied(MerchantBuyerSurface):
        async def complete_checkout(self, checkout_id: uuid.UUID, request: Any) -> Any:
            paid = await super().complete_checkout(checkout_id, request)
            return paid.model_copy(
                update={
                    "admitted": False,
                    "created": False,
                    "refusal": AdmissionRefusal.NOT_AUTHORIZED,
                    "attempt": None,
                }
            )

    surface = MeasuredBuyerSurface(
        Denied(factory, merchant_id=merchant_id, provider=FakePaymentProvider()), ledger
    )
    await ReferenceMissionExecutor(surface)(mission_brief(), merchant_id=merchant_id)

    assert ledger.fault() is None


def test_the_evaluator_stamp_moves_when_the_attribution_rules_do() -> None:
    """A marking change with an unchanged stamp is two runs compared as though they were one.

    That is not hypothetical: moving whose fault a refusal is changed what missions were marked
    as without touching a word of the failure vocabulary, and the stamp did not move.
    """
    before = evaluator_version()

    # Patched where the digest reads it rather than where it is declared, because the stamp is
    # what has to move and a `from` import binds a second name.
    with patch.object(evaluation, "CATALOG_REFUSALS", frozenset({"something-else"})):
        after = evaluator_version()

    assert before != after
