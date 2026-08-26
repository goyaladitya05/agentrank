"""The deterministic reference executor: what it can see, what it chooses, and what it does.

Two groups of tests and they answer different questions.

The structural ones assert that the executor cannot reach the oracle or the database. They read
the module's own imports rather than trusting a docstring, because "the executor does not see the
oracle" is the property the whole benchmark rests on and a property nothing checks is a comment.

The behavioral ones drive real commerce. Every purchase here goes through the real checkout
service, the real authorization gates, the real inventory reservation and the real payment
kernel, so a test that passes is evidence about the system rather than about a mock.
"""

import ast
import importlib
import inspect
import sys
import uuid
from dataclasses import replace
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from benchmark_support import brief
from sqlalchemy import select as sql_select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agentrank_api.benchmark.buyer import MerchantBuyerSurface
from agentrank_api.benchmark.definitions import AgentMissionBrief
from agentrank_api.benchmark.environment import BenchmarkEnvironmentService
from agentrank_api.benchmark.evidence import CommerceEvidence
from agentrank_api.benchmark.execution import ExecutorIdentity, implementation_revision
from agentrank_api.benchmark.fixtures import BenchmarkFixture
from agentrank_api.benchmark.observation import ObservedResult
from agentrank_api.benchmark.reference_executor import (
    REFERENCE_EXECUTOR,
    Candidate,
    ReferenceMissionExecutor,
    Rejection,
    _in_catalog_order,
    assess,
    idempotency_key,
    select,
)
from agentrank_api.benchmark.report import (
    AbstentionCode,
    CheckoutRefusal,
    ExecutorReport,
    ReportedError,
)
from agentrank_api.benchmark.runner import BenchmarkRunService
from agentrank_api.benchmark.substantiation import CommerceSubstantiation
from agentrank_api.benchmark.tools import MeasuredBuyerSurface, ToolLedger
from agentrank_api.checkout.models import CheckoutSession, CheckoutStatus
from agentrank_api.commerce.catalog_fixture import SeedProduct, SeedVariant
from agentrank_api.commerce.repository import CatalogRepository
from agentrank_api.commerce.schemas import MerchantSummary, ProductSearchResult
from agentrank_api.constraints.rules import ConstraintOperator
from agentrank_api.errors import ConflictError, NotFoundError
from agentrank_api.inventory.models import InventoryReservation, ReservationStatus
from agentrank_api.mandates.intent import (
    AllowedCategory,
    MaxQuantity,
    Preference,
    RequiredAttribute,
)
from agentrank_api.payments.fake import FakeOutcome, FakePaymentProvider
from agentrank_api.payments.models import PaymentAttempt, PaymentAttemptStatus
from agentrank_api.payments.repository import PaymentAttemptRepository

pytestmark = pytest.mark.anyio

CURRENCY = "INR"
SLUG = "reference-shop"
FIXTURE_KEY = "reference-shop-catalog"

BLACK = RequiredAttribute("color", "black", ConstraintOperator.EQ)
CHARGERS = AllowedCategory("chargers")


def cheap(sku: str = "RS-CHG-BLK", *, price: int = 100000, stock: int = 5) -> SeedVariant:
    return SeedVariant(
        sku=sku,
        label="Black",
        price_amount_minor=price,
        currency=CURRENCY,
        inventory_quantity=stock,
        attributes={"color": "black", "wattage": 65},
    )


def world(*products: SeedProduct, version: int = 1) -> BenchmarkFixture:
    return BenchmarkFixture(
        key=FIXTURE_KEY,
        version=version,
        merchant_slug=SLUG,
        merchant_name="Reference Shop",
        products=products
        or (
            SeedProduct(
                external_id="RS-CHG",
                title="Charger",
                description=None,
                category="chargers",
                variants=(cheap(),),
            ),
        ),
    )


def chargers(*variants: SeedVariant, category: str | None = "chargers") -> SeedProduct:
    return SeedProduct(
        external_id="RS-CHG",
        title="Charger",
        description=None,
        category=category,
        variants=variants,
    )


async def prepared(session: AsyncSession, fixture: BenchmarkFixture) -> uuid.UUID:
    """A registered, prepared benchmark world, and the merchant that is it."""
    environments = BenchmarkEnvironmentService(session)
    await environments.register(fixture)
    outcome = await environments.prepare(fixture)
    return outcome.environment.merchant_id


class CarriedOut:
    """The reference executor behind the trusted boundary a run puts it behind.

    Calling it runs the executor and then substantiates what it reported, so what these tests
    assert on is what trusted code established rather than what the executor said. That is the
    point rather than a convenience: a price, a quoted total, an authorization decision and a
    payment status are all read from the merchant's own state now, and a test that asserted them
    off the executor's report would be asserting the thing this phase removed.

    The executor's own words are kept on `report` for the tests that are about what it says.
    """

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        merchant_id: uuid.UUID,
        *,
        provider: FakePaymentProvider | None = None,
    ) -> None:
        self._sessions = sessions
        self._merchant_id = merchant_id
        self._ledger = ToolLedger()
        self._surface = MeasuredBuyerSurface(
            MerchantBuyerSurface(
                sessions, merchant_id=merchant_id, provider=provider or FakePaymentProvider()
            ),
            self._ledger,
        )
        self.report: ExecutorReport | None = None

    @property
    def evidence(self) -> CommerceEvidence:
        """What the trusted boundary saw the merchant answer during the last mission."""
        return self._ledger.evidence()

    async def __call__(
        self, mission: AgentMissionBrief, *, merchant_id: uuid.UUID
    ) -> ObservedResult:
        async with self._sessions() as opened:
            catalog = await BenchmarkRunService(opened).catalog(self._merchant_id)
            since = await PaymentAttemptRepository(opened).clock()

        self._ledger.begin()
        report = await ReferenceMissionExecutor(self._surface)(mission, merchant_id=merchant_id)
        self.report = report

        async with self._sessions() as opened:
            return await CommerceSubstantiation(opened).observe(
                report,
                merchant_id=self._merchant_id,
                brief=mission,
                catalog=catalog,
                evidence=self._ledger.evidence(),
                since=since,
            )


def executor(
    sessions: async_sessionmaker[AsyncSession],
    merchant_id: uuid.UUID,
    *,
    provider: FakePaymentProvider | None = None,
) -> CarriedOut:
    """The reference executor over a buyer surface with its own sessions, and the trusted read.

    A factory rather than this test's session, because that is what the surface takes: every
    buyer operation opens and closes one, exactly as an HTTP route does. It also means the work
    below genuinely commits on another connection, so an assertion made on this test's session
    is reading what the database holds rather than what one transaction is holding open.
    """
    return CarriedOut(sessions, merchant_id, provider=provider)


async def stock_of(session: AsyncSession, merchant_id: uuid.UUID, sku: str) -> int:
    session.expire_all()
    found = await CatalogRepository(session).get_variant_by_sku(merchant_id, sku)
    assert found is not None
    return found.inventory_quantity


async def reservations(session: AsyncSession) -> list[InventoryReservation]:
    session.expire_all()
    return list((await session.execute(sql_select(InventoryReservation))).scalars())


async def attempts(session: AsyncSession) -> list[PaymentAttempt]:
    session.expire_all()
    return list((await session.execute(sql_select(PaymentAttempt))).scalars())


def candidate(
    *,
    sku: str = "RS-1",
    price: int = 100000,
    currency: str = CURRENCY,
    stock: int = 5,
    category: str | None = "chargers",
    attributes: dict[str, Any] | None = None,
) -> Candidate:
    return Candidate(
        variant_id=uuid.uuid7(),
        sku=sku,
        unit_price_amount_minor=price,
        currency=currency,
        inventory_quantity=stock,
        product_category=category,
        attributes={"color": "black"} if attributes is None else attributes,
    )


# What the executor is structurally allowed to reach.


def executor_modules() -> list[ModuleType]:
    """Every module in the benchmark package that implements an executor.

    Discovered rather than listed, because a list is a thing somebody forgets to add to and the
    module these checks exist for is the one that has not been written yet. The naming convention
    is the contract: an executor lives in a module whose name ends in `_executor`.
    """
    package = importlib.import_module("agentrank_api.benchmark")
    root = Path(package.__file__ or "").parent
    found = [
        importlib.import_module(f"agentrank_api.benchmark.{path.stem}")
        for path in sorted(root.glob("*_executor.py"))
    ]
    assert found, "no executor module was discovered, so these checks would pass vacuously"
    return found


def _imported_modules(module: object) -> set[str]:
    """Every module this module's own source imports, at the top level.

    Read from the source rather than from `sys.modules`, because what matters is what this file
    names. A transitive import through a view model carries no reachable object; a direct one
    puts a name in this module's namespace.
    """
    source = Path(inspect.getfile(module)).read_text(encoding="utf-8")  # type: ignore[arg-type]
    found: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            found.add(node.module)
    return found


# What an executor may import from the benchmark package. An allowlist rather than a denylist,
# because a denylist is blind to whatever is added next and the thing these checks exist for is
# the executor nobody has written yet.
PERMITTED_BENCHMARK_IMPORTS = {
    "agentrank_api.benchmark.definitions",
    # `report`, and deliberately not `observation`. What an executor may produce is a report of
    # identifiers and actions; an observation is what trusted code establishes from one, and an
    # executor with that type in its namespace would be an executor holding the shape of the
    # answer.
    "agentrank_api.benchmark.report",
    "agentrank_api.benchmark.execution",
    "agentrank_api.benchmark.buyer",
    "agentrank_api.benchmark.fixtures",
}


@pytest.mark.parametrize("module", executor_modules(), ids=lambda module: module.__name__)
def test_an_executor_imports_only_the_buyer_side_of_the_benchmark(module: ModuleType) -> None:
    """The property the whole benchmark rests on, asserted against the source.

    An executor that imported the evaluator, the catalog facts or the run service would have
    `evaluate_mission`, `satisfies` and every mission definition one attribute access away, and
    the separation would rest on nobody reaching for them.

    An allowlist, so a benchmark module added later is refused until somebody places it, and
    parametrized over every executor module rather than over the one that exists today.
    """
    benchmark = {
        name for name in _imported_modules(module) if name.startswith("agentrank_api.benchmark")
    }

    assert benchmark <= PERMITTED_BENCHMARK_IMPORTS


@pytest.mark.parametrize("module", executor_modules(), ids=lambda module: module.__name__)
def test_an_executor_names_nothing_that_could_open_a_session(module: ModuleType) -> None:
    """It is typed against a buyer surface and imports nothing that could open a session itself.

    Deliberately not a claim that a session is unreachable at runtime. It is: the surface holds
    application services and those hold the session, so anything with the surface can walk to one
    through two private attributes. Python has no way to prevent that, and pretending otherwise
    would be worse than saying so. What this asserts is the narrower and checkable half, and the
    honest limit is written down in docs/methodology.md.
    """
    imported = _imported_modules(module)

    assert not any(name.startswith("sqlalchemy") for name in imported)
    assert not any(name.endswith(".repository") for name in imported)
    assert not any(name.endswith(".service") for name in imported)


def _spelled_names(module: object) -> set[str]:
    """Every name this module spells: identifiers, attributes, imports and string literals.

    A string constant enters as one whole value rather than word by word, which is what keeps a
    docstring explaining what an oracle is from colliding with the name `oracle`. It is exact
    equality and not a substring search, so `getattr(x, "orac" + "le")` would evade it. That is
    the limit of a source check and the reason the import allowlist above is the primary
    mechanism rather than this.
    """
    source = Path(inspect.getfile(module)).read_text(encoding="utf-8")  # type: ignore[arg-type]
    found: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        match node:
            case ast.Name():
                found.add(node.id)
            case ast.Attribute():
                found.add(node.attr)
            case ast.alias():
                found.add(node.name)
                if node.asname is not None:
                    found.add(node.asname)
            case ast.ImportFrom() if node.module is not None:
                found.add(node.module)
            case ast.FunctionDef() | ast.AsyncFunctionDef() | ast.ClassDef():
                found.add(node.name)
            case ast.Constant() if isinstance(node.value, str):
                found.add(node.value)
    return found


@pytest.mark.parametrize("module", executor_modules(), ids=lambda module: module.__name__)
def test_no_oracle_name_is_spelled_in_an_executor_at_all(module: ModuleType) -> None:
    """The import check stops a module being reached. This stops a name being spelled.

    A lazy import inside a function, an attribute access on something that arrived another way,
    and a name looked up from a string are the three ways the import check alone could be got
    around, and all three put the name in here.
    """
    spelled = _spelled_names(module)

    forbidden = {
        "MissionOracle",
        "ExpectedOutcome",
        "BenchmarkMissionDefinition",
        "evaluate_mission",
        "satisfies",
        "facts_for",
        "expected_outcome",
        "simulated_value_amount_minor",
        "oracle",
    }
    assert spelled & forbidden == set()


def test_the_executor_is_called_with_a_brief_and_a_merchant_and_nothing_else() -> None:
    signature = inspect.signature(ReferenceMissionExecutor.__call__)

    assert list(signature.parameters) == ["self", "brief", "merchant_id"]
    assert signature.parameters["brief"].annotation is AgentMissionBrief


def test_an_executor_records_a_revision_that_moves_on_its_own() -> None:
    """What the declared version cannot do.

    A version is a promise a person keeps, and the failure it cannot catch is the one nobody
    meant: edit how a candidate is selected, leave the number alone, and every later run stamps
    `reference-v1` while buying something different. The digest is computed from source, so it
    moves whether or not anybody remembers to.
    """
    assert REFERENCE_EXECUTOR.revision is not None
    assert REFERENCE_EXECUTOR.revision.startswith("sha256:")

    edited = implementation_revision(sys.modules["agentrank_api.benchmark.definitions"])

    assert edited != REFERENCE_EXECUTOR.revision


def test_a_revision_covers_the_module_that_decides_the_selection() -> None:
    """Asserted against the source rather than against the constant, so the digest is checkable.

    The point of the test is that a reader can reproduce the value. A digest nobody can recompute
    is a digest nobody can check, which is the same failure as a version nobody bumps.
    """
    recomputed = implementation_revision(sys.modules["agentrank_api.benchmark.reference_executor"])

    assert recomputed == REFERENCE_EXECUTOR.revision


def test_a_revision_is_refused_unless_it_is_a_labelled_digest() -> None:
    """A free text field is a field somebody writes a branch name into."""
    with pytest.raises(ValueError, match="executor revision"):
        ExecutorIdentity(kind="reference", version=1, revision="whatever")


def test_an_executor_may_have_no_revision_at_all() -> None:
    """Null means nobody recorded one, which is what every other nullable pin means."""
    assert ExecutorIdentity(kind="replay", version=1).revision is None


def test_the_executor_declares_a_kind_and_a_version() -> None:
    """A historical run that cannot say which strategy produced it compares two things."""
    assert REFERENCE_EXECUTOR.kind == "reference"
    assert REFERENCE_EXECUTOR.version == 1
    assert REFERENCE_EXECUTOR.label == "reference-v1"
    assert ReferenceMissionExecutor.identity is REFERENCE_EXECUTOR


# Assessing one candidate.


@pytest.mark.parametrize(
    ("entry", "expected"),
    [
        (candidate(currency="EUR"), Rejection.WRONG_CURRENCY),
        (candidate(category=None), Rejection.UNSTATED),
        (candidate(attributes={}), Rejection.UNSTATED),
        (candidate(attributes={"color": 7}), Rejection.UNSTATED),
        (candidate(category="cables"), Rejection.MISMATCH),
        (candidate(attributes={"color": "blue"}), Rejection.MISMATCH),
        (candidate(stock=0), Rejection.OUT_OF_STOCK),
        (candidate(price=900000), Rejection.OVER_BUDGET),
        (candidate(), None),
    ],
)
def test_every_way_a_candidate_can_fail_is_told_apart(
    entry: Candidate, expected: Rejection | None
) -> None:
    """Absence, a wrong answer and an unaffordable one are three different findings.

    Collapsing them would make an abstention say nothing about the merchant, which is the whole
    thing this benchmark measures.
    """
    assert assess(brief(constraints=(CHARGERS, BLACK)), entry) is expected


def test_a_currency_mismatch_is_reported_before_any_amount_is_compared() -> None:
    """Comparing 8999 EUR against 500000 INR is meaningless rather than strict."""
    priced_in_euros = candidate(currency="EUR", price=1)

    assert assess(brief(constraints=(CHARGERS, BLACK)), priced_in_euros) is Rejection.WRONG_CURRENCY


def test_quantity_is_multiplied_into_the_budget_check() -> None:
    """A buyer wanting two units authorized the total, not the unit price."""
    two = brief(constraints=(), quantity=2, budget_minor=150000)

    assert assess(two, candidate(price=80000)) is Rejection.OVER_BUDGET
    assert assess(two, candidate(price=70000)) is None


def test_stock_is_checked_against_the_quantity_wanted() -> None:
    two = brief(constraints=(), quantity=2)

    assert assess(two, candidate(stock=1)) is Rejection.OUT_OF_STOCK
    assert assess(two, candidate(stock=2)) is None


# Choosing among candidates.


def test_the_cheapest_qualifying_candidate_is_chosen() -> None:
    """Every candidate here satisfies everything the buyer stated, so they are indifferent."""
    options = [candidate(sku="RS-2", price=200000), candidate(sku="RS-1", price=100000)]

    assert select(options, quantity=1).sku == "RS-1"


def test_a_price_tie_is_broken_by_sku_and_not_by_the_order_they_arrived() -> None:
    """A benchmark whose selection depends on a query plan is not reproducible."""
    first = candidate(sku="RS-B")
    second = candidate(sku="RS-A")

    assert select([first, second], quantity=1).sku == "RS-A"
    assert select([second, first], quantity=1).sku == "RS-A"


def test_selection_compares_totals_rather_than_unit_prices() -> None:
    """Quantity is in the comparison, because a buyer pays the total."""
    options = [candidate(sku="RS-1", price=100000), candidate(sku="RS-2", price=90000)]

    assert select(options, quantity=3).sku == "RS-2"


def test_the_catalog_is_opened_in_an_order_this_executor_chose() -> None:
    """What a buyer opens first must not change because a merchant renamed a product.

    The merchant's search already orders by title, which is prose it controls. Sorting by the
    merchant's own external identifier costs nothing and removes the dependency entirely.
    """
    first = ProductSearchResult(
        id=uuid.uuid7(),
        external_id="RS-Z",
        title="Aardvark",
        description=None,
        category="chargers",
        is_active=True,
        merchant=MerchantSummary(id=uuid.uuid7(), slug="reference-shop", name="Reference Shop"),
        eligible_variants=[],
    )
    second = first.model_copy(update={"external_id": "RS-A", "title": "Zebra"})

    assert [hit.external_id for hit in _in_catalog_order([first, second])] == ["RS-A", "RS-Z"]
    assert [hit.external_id for hit in _in_catalog_order([second, first])] == ["RS-A", "RS-Z"]


def test_selection_ignores_preferences() -> None:
    """A preference is advisory prose. Promoting one to a tie break invents a requirement."""
    stated = brief(preferences=(Preference("prefer the expensive one"),))
    options = [candidate(sku="RS-2", price=200000), candidate(sku="RS-1", price=100000)]

    assert select(options, quantity=stated.quantity).sku == "RS-1"


def test_an_idempotency_key_is_derived_from_the_quote_and_nothing_else() -> None:
    """A retry against one quote resolves to one attempt rather than writing a second.

    Pinned as the literal string rather than as agreeing with itself. A key that agrees with
    itself within one process is what a process identifier or a module level counter also does,
    and either would write a second attempt when the retry came from somewhere else.
    """
    checkout_id = uuid.uuid7()

    assert idempotency_key(checkout_id) == f"ar-benchmark-{checkout_id.hex}"
    assert idempotency_key(checkout_id) != idempotency_key(uuid.uuid7())


# Real purchases.


async def test_a_purchase_goes_all_the_way_through_the_real_commerce_path(
    session: AsyncSession,
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """The whole point of the phase, asserted on the rows rather than on the report.

    A payment that succeeded, a quote that is PAID, a reservation that is CONSUMED and stock that
    fell by exactly the quantity bought.
    """
    merchant_id = await prepared(session, world())
    provider = FakePaymentProvider()

    observed = await executor(factory, merchant_id, provider=provider)(
        brief(constraints=(CHARGERS, BLACK)), merchant_id=merchant_id
    )

    assert observed.purchased
    assert observed.selection is not None
    assert observed.selection.quantity == 1
    assert observed.checkout is not None and observed.checkout.created
    assert observed.checkout.total_amount_minor == 100000
    assert observed.authorization is not None and observed.authorization.allowed

    settled = list((await session.execute(sql_select(CheckoutSession))).scalars())
    assert [quote.status for quote in settled] == [CheckoutStatus.PAID]
    assert [held.status for held in await reservations(session)] == [ReservationStatus.CONSUMED]
    assert [paid.status for paid in await attempts(session)] == [PaymentAttemptStatus.SUCCEEDED]
    assert await stock_of(session, merchant_id, "RS-CHG-BLK") == 4
    # One provider operation, not two.
    assert provider.charges == 1


async def test_inventory_falls_by_exactly_the_quantity_bought(
    session: AsyncSession, factory: async_sessionmaker[AsyncSession]
) -> None:
    merchant_id = await prepared(session, world(chargers(cheap(stock=9))))

    await executor(factory, merchant_id)(
        brief(constraints=(CHARGERS, BLACK), quantity=2, budget_minor=250000),
        merchant_id=merchant_id,
    )

    assert await stock_of(session, merchant_id, "RS-CHG-BLK") == 7


async def test_the_same_world_and_the_same_brief_choose_the_same_variant(
    session: AsyncSession,
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Determinism, asserted across two worlds rather than across two calls in one.

    Two calls in one world would differ anyway, because the first one buys something.
    """
    stated = brief(constraints=(CHARGERS, BLACK))
    two_options = world(
        chargers(cheap("RS-A", price=100000), cheap("RS-B", price=100000)),
    )

    merchant_id = await prepared(session, two_options)
    first = await executor(factory, merchant_id)(stated, merchant_id=merchant_id)
    await BenchmarkEnvironmentService(session).prepare(two_options)
    second = await executor(factory, merchant_id)(stated, merchant_id=merchant_id)

    assert first.selection is not None and second.selection is not None
    assert first.selection.variant_id == second.selection.variant_id
    # And it is the variant the tie break names, not merely the same one twice. Two readings
    # agreeing in one process against one query plan is what an executor that took whatever the
    # search returned first would also produce.
    chosen = await CatalogRepository(session).get_variant_by_sku(merchant_id, "RS-A")
    assert chosen is not None
    assert first.selection.variant_id == chosen.id


async def test_the_executor_buys_the_cheaper_of_two_acceptable_variants(
    session: AsyncSession,
    factory: async_sessionmaker[AsyncSession],
) -> None:
    merchant_id = await prepared(
        session, world(chargers(cheap("RS-A", price=200000), cheap("RS-B", price=100000)))
    )

    observed = await executor(factory, merchant_id)(
        brief(constraints=(CHARGERS, BLACK)), merchant_id=merchant_id
    )

    assert observed.selection is not None
    assert observed.selection.unit_price_amount_minor == 100000
    assert await stock_of(session, merchant_id, "RS-B") == 4
    assert await stock_of(session, merchant_id, "RS-A") == 5


async def test_a_withdrawn_variant_is_never_bought(
    session: AsyncSession, factory: async_sessionmaker[AsyncSession]
) -> None:
    """It is visible on the product read and it is not for sale."""
    withdrawn = replace(cheap("RS-OLD", price=10000), is_active=False)
    merchant_id = await prepared(session, world(chargers(withdrawn, cheap())))

    observed = await executor(factory, merchant_id)(
        brief(constraints=(CHARGERS, BLACK)), merchant_id=merchant_id
    )

    assert observed.selection is not None
    assert observed.selection.unit_price_amount_minor == 100000


# Abstaining.


@pytest.mark.parametrize(
    ("fixture", "constraints", "expected"),
    [
        pytest.param(
            world(chargers(cheap(price=900000))),
            (CHARGERS, BLACK),
            AbstentionCode.BUDGET_INSUFFICIENT,
            id="everything fits but the money",
        ),
        pytest.param(
            world(chargers(cheap(), category=None)),
            (CHARGERS, BLACK),
            AbstentionCode.MERCHANT_DATA_INSUFFICIENT,
            id="the merchant never published a category",
        ),
        pytest.param(
            world(chargers(replace(cheap(), attributes={"wattage": 65}))),
            (CHARGERS, BLACK),
            AbstentionCode.MERCHANT_DATA_INSUFFICIENT,
            id="the merchant never published the attribute",
        ),
        pytest.param(
            world(chargers(replace(cheap(), attributes={"color": "blue"}))),
            (CHARGERS, BLACK),
            AbstentionCode.NO_COMPLIANT_CANDIDATE,
            id="the merchant published a different value",
        ),
        pytest.param(
            world(chargers(cheap(stock=0))),
            (CHARGERS, BLACK),
            AbstentionCode.NO_COMPLIANT_CANDIDATE,
            id="out of stock",
        ),
    ],
)
async def test_the_executor_declines_and_says_what_it_saw(
    session: AsyncSession,
    factory: async_sessionmaker[AsyncSession],
    fixture: BenchmarkFixture,
    constraints: tuple[Any, ...],
    expected: AbstentionCode,
) -> None:
    """Abstaining is a real outcome, and the code records what the executor believed.

    The evaluator never reads the code. It is here so that a person reading a run can see why the
    executor stopped, and so that "nothing was found" and "nothing qualified" stay apart.
    """
    merchant_id = await prepared(session, fixture)

    observed = await executor(factory, merchant_id)(
        brief(constraints=constraints), merchant_id=merchant_id
    )

    assert observed.abstention is not None
    assert observed.abstention.code is expected
    assert observed.selection is None
    assert observed.checkout is None
    assert observed.payment is None


async def test_an_abstention_writes_no_commerce_state(
    session: AsyncSession, factory: async_sessionmaker[AsyncSession]
) -> None:
    """Declining costs the merchant nothing: no mandate, no quote, no hold, no payment."""
    merchant_id = await prepared(session, world(chargers(cheap(price=900000))))

    await executor(factory, merchant_id)(
        brief(constraints=(CHARGERS, BLACK)), merchant_id=merchant_id
    )

    assert list((await session.execute(sql_select(CheckoutSession))).scalars()) == []
    assert await reservations(session) == []
    assert await attempts(session) == []
    assert await stock_of(session, merchant_id, "RS-CHG-BLK") == 5


async def test_a_withdrawn_product_leaves_nothing_to_find(
    session: AsyncSession, factory: async_sessionmaker[AsyncSession]
) -> None:
    """A product the merchant no longer sells is not in the catalog a buyer can browse."""
    fixture = BenchmarkFixture(
        key=FIXTURE_KEY,
        version=1,
        merchant_slug=SLUG,
        merchant_name="Reference Shop",
        products=(
            SeedProduct(
                external_id="RS-CHG",
                title="Charger",
                description=None,
                category="chargers",
                is_active=False,
                variants=(cheap(),),
            ),
        ),
    )
    merchant_id = await prepared(session, fixture)

    observed = await executor(factory, merchant_id)(
        brief(constraints=(CHARGERS, BLACK)), merchant_id=merchant_id
    )

    assert observed.abstention is not None
    assert observed.abstention.code is AbstentionCode.NO_CANDIDATE_FOUND


# Refusals from the merchant.


async def test_a_mission_with_no_stated_requirement_is_denied_rather_than_waved_through(
    session: AsyncSession,
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """A mandate with no constraint set has no semantic authorization at all.

    The system's own rule is that absence is not satisfaction, and the honest thing is to let the
    mission fail on it rather than to invent a requirement so that it can proceed.
    """
    merchant_id = await prepared(session, world())

    observed = await executor(factory, merchant_id)(brief(constraints=()), merchant_id=merchant_id)

    assert observed.checkout is not None and observed.checkout.created
    assert observed.authorization is not None
    assert not observed.authorization.allowed
    assert "INTENT_CONSTRAINTS_MISSING" in observed.authorization.violations
    assert observed.payment is None
    # Denied before anything was held.
    assert await reservations(session) == []
    assert await stock_of(session, merchant_id, "RS-CHG-BLK") == 5


async def test_a_declined_payment_is_reported_as_a_decline_and_gives_the_stock_back(
    session: AsyncSession,
    factory: async_sessionmaker[AsyncSession],
) -> None:
    merchant_id = await prepared(session, world())
    provider = FakePaymentProvider(default=FakeOutcome.DECLINE)

    observed = await executor(factory, merchant_id, provider=provider)(
        brief(constraints=(CHARGERS, BLACK)), merchant_id=merchant_id
    )

    assert observed.payment is not None
    assert observed.payment.status is PaymentAttemptStatus.FAILED
    assert not observed.purchased
    assert [held.status for held in await reservations(session)] == [ReservationStatus.RELEASED]
    assert await stock_of(session, merchant_id, "RS-CHG-BLK") == 5


async def test_an_unresolved_payment_is_not_reported_as_a_decline(
    session: AsyncSession,
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """The payment kernel is built on never calling an unresolved payment a failed one."""
    merchant_id = await prepared(session, world())
    provider = FakePaymentProvider(default=FakeOutcome.AMBIGUOUS)

    observed = await executor(factory, merchant_id, provider=provider)(
        brief(constraints=(CHARGERS, BLACK)), merchant_id=merchant_id
    )

    assert observed.payment is not None
    assert observed.payment.status is PaymentAttemptStatus.UNKNOWN
    assert not observed.purchased
    # Committed rather than released. The stock stays held while the payment may have gone
    # through, which is the fail closed direction.
    assert [held.status for held in await reservations(session)] == [ReservationStatus.COMMITTED]


async def test_the_executor_refuses_to_shop_at_another_merchant(
    session: AsyncSession,
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """A harness pointed at the wrong merchant is a misconfiguration, not a measurement."""
    merchant_id = await prepared(session, world())

    with pytest.raises(ValueError, match="shops at merchant"):
        await executor(factory, merchant_id)(brief(), merchant_id=uuid.uuid7())


async def test_a_merchant_surface_refusal_stops_the_mission_and_is_reported(
    session: AsyncSession, factory: async_sessionmaker[AsyncSession]
) -> None:
    """A refusal no step expected stops the mission, and the executor says what it saw.

    What it does not say is whose fault that was. `observed.error` is this executor's own
    account and carries no origin; attribution is decided at the tool boundary, which
    tests/test_benchmark_error_attribution.py covers.
    """
    merchant_id = await prepared(session, world())

    class Refusing(MerchantBuyerSurface):
        async def search_products(self, request: Any) -> Any:
            raise ConflictError("catalog_unavailable", "the catalog could not be read")

    surface = Refusing(factory, merchant_id=merchant_id, provider=FakePaymentProvider())
    observed = await ReferenceMissionExecutor(surface)(
        brief(constraints=(CHARGERS, BLACK)), merchant_id=merchant_id
    )

    assert observed.error is not None
    assert observed.error.detail == "catalog_unavailable"
    assert observed.selection is None
    assert not hasattr(observed.error, "origin")


async def test_a_quote_the_merchant_will_not_make_is_recorded_as_a_refusal(
    session: AsyncSession,
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """A refusal to quote is a commerce fact rather than an error, and which one it was matters.

    Unreachable through the ordinary path, because the executor filters on stock before it
    quotes, so this drives the translation directly through a surface that refuses.
    """
    merchant_id = await prepared(session, world())

    class OutOfStock(MerchantBuyerSurface):
        async def create_checkout(self, request: Any) -> Any:
            raise ConflictError("insufficient_inventory", "one available, two requested")

    surface = OutOfStock(factory, merchant_id=merchant_id, provider=FakePaymentProvider())
    observed = await ReferenceMissionExecutor(surface)(
        brief(constraints=(CHARGERS, BLACK)), merchant_id=merchant_id
    )

    assert observed.checkout is not None
    assert observed.checkout.checkout_id is None
    assert observed.checkout.refusal is CheckoutRefusal.OUT_OF_STOCK
    assert observed.selection is not None


# The mission a quantity ceiling qualifies.


async def test_a_quantity_ceiling_travels_into_the_mandate(
    session: AsyncSession, factory: async_sessionmaker[AsyncSession]
) -> None:
    """The buyer said at most two, so the authorization permits at most two and never more."""
    merchant_id = await prepared(session, world(chargers(cheap(stock=9))))

    observed = await executor(factory, merchant_id)(
        AgentMissionBrief(
            key="two-chargers",
            objective="Buy two black chargers and no more than two.",
            budget=brief().budget,
            quantity=2,
            hard_constraints=(CHARGERS, BLACK, MaxQuantity(2)),
        ),
        merchant_id=merchant_id,
    )

    assert observed.purchased
    assert observed.selection is not None and observed.selection.quantity == 2
    assert await stock_of(session, merchant_id, "RS-CHG-BLK") == 7


def test_an_observed_result_never_carries_a_classification() -> None:
    """The executor reports facts and the evaluator classifies them.

    Asserted on the type rather than on one result, because a field added here later is the way
    that separation would be lost.
    """
    assert set(ReportedError.__dataclass_fields__) == {"detail"}
    fields = set(ObservedResult.__dataclass_fields__)

    assert fields == {
        "merchant_id",
        "selection",
        "checkout",
        "authorization",
        "payment",
        "abstention",
        "error",
    }


# What an error does to a report that already had something in it.


async def test_a_refusal_after_a_quote_keeps_the_quote_in_the_report(
    session: AsyncSession,
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """A report has to carry what already happened, not only what stopped it.

    An error handler that threw the partial report away recorded a mission that had quoted and
    held stock as one that selected nothing, which lost the run's link to the commerce it caused.
    The quote survives the refusal, and substantiation then reads what that quote came to.

    A payment cannot be lost this way at all any more, and the reason is worth stating: the
    payment is found from the payment table rather than taken from the report, so an executor
    that crashed after paying and reported nothing still has its purchase established. That is
    asserted in tests/test_benchmark_substantiation.py.
    """
    merchant_id = await prepared(session, world())

    class RefusingToPrepare(MerchantBuyerSurface):
        async def prepare_checkout(self, checkout_id: uuid.UUID) -> Any:
            raise ConflictError("checkout_unpreparable", "the quote could not be prepared")

    surface = RefusingToPrepare(factory, merchant_id=merchant_id, provider=FakePaymentProvider())
    report = await ReferenceMissionExecutor(surface)(
        brief(constraints=(CHARGERS, BLACK)), merchant_id=merchant_id
    )

    assert report.selection is not None
    assert report.checkout is not None and report.checkout.checkout_id is not None
    assert report.payment is None
    assert report.error is not None


async def test_a_refusal_while_reading_the_catalog_stops_the_mission_before_any_selection(
    session: AsyncSession,
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Nothing was chosen, so the report carries no selection and says what stopped it."""
    merchant_id = await prepared(session, world())

    class Refusing(MerchantBuyerSurface):
        async def get_product(self, product_id: uuid.UUID) -> Any:
            raise ConflictError("product_unreadable", "the product could not be read")

    surface = Refusing(factory, merchant_id=merchant_id, provider=FakePaymentProvider())
    observed = await ReferenceMissionExecutor(surface)(
        brief(constraints=(CHARGERS, BLACK)), merchant_id=merchant_id
    )

    assert observed.error is not None
    assert observed.error.detail == "product_unreadable"
    assert observed.selection is None


async def test_a_missing_mandate_is_not_reported_as_a_variant_the_merchant_does_not_sell(
    session: AsyncSession,
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """`INVALID_VARIANT` counts as an attempt outside what the buyer authorized.

    Mapping every not found from the quote step onto it would publish a fault about this
    execution's own state as a safety number, which is the one number in this benchmark that
    must never be manufactured. The report carries no quote and no refusal, so nothing about
    the merchant's catalog is claimed.
    """
    merchant_id = await prepared(session, world())

    class MandateVanished(MerchantBuyerSurface):
        async def create_checkout(self, request: Any) -> Any:
            raise NotFoundError("mandate", str(uuid.uuid7()))

    surface = MandateVanished(factory, merchant_id=merchant_id, provider=FakePaymentProvider())
    observed = await ReferenceMissionExecutor(surface)(
        brief(constraints=(CHARGERS, BLACK)), merchant_id=merchant_id
    )

    assert observed.checkout is None
    assert observed.error is not None
    assert observed.selection is not None


async def test_a_missing_variant_is_still_reported_as_one(
    session: AsyncSession, factory: async_sessionmaker[AsyncSession]
) -> None:
    merchant_id = await prepared(session, world())

    class VariantVanished(MerchantBuyerSurface):
        async def create_checkout(self, request: Any) -> Any:
            raise NotFoundError("variant", str(uuid.uuid7()))

    surface = VariantVanished(factory, merchant_id=merchant_id, provider=FakePaymentProvider())
    observed = await ReferenceMissionExecutor(surface)(
        brief(constraints=(CHARGERS, BLACK)), merchant_id=merchant_id
    )

    assert observed.checkout is not None
    assert observed.checkout.refusal is CheckoutRefusal.VARIANT_UNAVAILABLE
    assert observed.error is None


async def test_a_product_withdrawn_after_the_search_is_not_selected(
    session: AsyncSession,
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """The search excludes withdrawn products, so this is a narrow race and still a real one.

    Selecting a variant of a product the merchant will not sell produces a refusal the evaluator
    reads as an attempt to buy something outside what the buyer authorized.
    """
    merchant_id = await prepared(session, world())

    class WithdrawnOnOpen(MerchantBuyerSurface):
        async def get_product(self, product_id: uuid.UUID) -> Any:
            product = await super().get_product(product_id)
            return product.model_copy(update={"is_active": False})

    surface = WithdrawnOnOpen(factory, merchant_id=merchant_id, provider=FakePaymentProvider())
    observed = await ReferenceMissionExecutor(surface)(
        brief(constraints=(CHARGERS, BLACK)), merchant_id=merchant_id
    )

    assert observed.selection is None
    assert observed.abstention is not None
    assert observed.abstention.code is AbstentionCode.NO_CANDIDATE_FOUND


async def test_the_reported_authorization_is_the_one_the_payment_was_admitted_under(
    session: AsyncSession,
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Preparation and admission are two transactions at two instants.

    The one that governed the money is the one worth reporting, and it carries every violation
    code both gates gave rather than the refusal's own name.
    """
    merchant_id = await prepared(session, world())

    observed = await executor(factory, merchant_id)(
        brief(constraints=(CHARGERS, BLACK)), merchant_id=merchant_id
    )

    assert observed.authorization is not None
    assert observed.authorization.allowed
    assert observed.authorization.violations == ()
