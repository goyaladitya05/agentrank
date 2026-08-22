"""The deterministic benchmark runner: pinning, oracle checking, recording and closing."""

import uuid

import pytest
from benchmark_support import BLACK, CURRENCY, VALUE, fixture, mission, suite
from commerce_support import PRICE, admit, build_shop, quote
from sqlalchemy.ext.asyncio import AsyncSession

from agentrank_api.benchmark.catalog import CatalogEntry, catalog_content_hash
from agentrank_api.benchmark.definitions import AgentMissionBrief, ExpectedOutcome
from agentrank_api.benchmark.environment import BenchmarkEnvironmentService
from agentrank_api.benchmark.evaluation import evaluator_version
from agentrank_api.benchmark.execution import ExecutorIdentity
from agentrank_api.benchmark.failures import FailureReason
from agentrank_api.benchmark.lifecycle import BenchmarkRunStatus, MissionRunStatus
from agentrank_api.benchmark.observation import (
    AbstentionCode,
    ObservedAbstention,
    ObservedAuthorization,
    ObservedCheckout,
    ObservedPayment,
    ObservedResult,
    ObservedSelection,
)
from agentrank_api.benchmark.runner import BenchmarkRunService, executor_from
from agentrank_api.benchmark.suites import BenchmarkSuiteService
from agentrank_api.errors import ConflictError, NotFoundError
from agentrank_api.payments.models import OutcomeSource, PaymentAttemptStatus
from agentrank_api.payments.repository import PaymentAttemptRepository

pytestmark = pytest.mark.anyio

SLUG = "test-merchant"

# The world every orchestrated run in this file is executed against.
WORLD = fixture()


async def shop(session: AsyncSession, *, price: int = PRICE, inventory: int = 3) -> uuid.UUID:
    """A merchant whose slug is the one `benchmark_support` authors suites against."""
    built = await build_shop(session, SLUG, price=price, inventory=inventory)
    return built.merchant_id


async def registered(session: AsyncSession) -> None:
    """Mark this merchant as the benchmark world `WORLD` describes.

    `run_suite` refuses to execute against a merchant nobody registered, which is the production
    safety rule rather than a test inconvenience: the orchestrated path overwrites a catalog.
    """
    await BenchmarkEnvironmentService(session).register(WORLD)


def selection(variant_id: uuid.UUID, *, quantity: int = 1, price: int = PRICE) -> ObservedSelection:
    return ObservedSelection(
        variant_id=variant_id,
        quantity=quantity,
        unit_price_amount_minor=price,
        currency=CURRENCY,
        product_category="chargers",
        variant_attributes={"color": "black"},
    )


def purchase(
    variant_id: uuid.UUID,
    merchant_id: uuid.UUID,
    *,
    checkout_id: uuid.UUID | None = None,
    attempt_id: uuid.UUID | None = None,
    price: int = PRICE,
) -> ObservedResult:
    chosen = selection(variant_id, price=price)
    return ObservedResult(
        merchant_id=merchant_id,
        selection=chosen,
        checkout=ObservedCheckout(
            created=True,
            checkout_id=checkout_id,
            total_amount_minor=chosen.line_amount_minor,
            currency=CURRENCY,
        ),
        authorization=ObservedAuthorization(allowed=True),
        payment=ObservedPayment(
            status=PaymentAttemptStatus.SUCCEEDED, attempt_id=attempt_id or uuid.uuid7()
        ),
    )


async def published(session: AsyncSession, *missions: object) -> None:
    await BenchmarkSuiteService(session).publish(
        suite(*missions, merchant_slug=SLUG)  # type: ignore[arg-type]
    )


# Pinning.


async def test_a_run_records_what_it_was_measured_against(session: AsyncSession) -> None:
    """The suite pins the workload. Without a catalog pin the other half is unattributable."""
    merchant_id = await shop(session)
    await published(session, mission("one", budget_minor=PRICE))

    run = await BenchmarkRunService(session).start_run(
        suite_key="test-suite", suite_version=1, merchant_slug=SLUG, representation_label="baseline"
    )

    assert run.status is BenchmarkRunStatus.RUNNING
    assert run.merchant_id == merchant_id
    assert run.representation_label == "baseline"
    assert run.catalog_hash is not None and run.catalog_hash.startswith("sha256:")
    assert run.evaluator_version == evaluator_version()


async def test_two_runs_of_an_unchanged_catalog_pin_the_same_hash(session: AsyncSession) -> None:
    """Two runs, one after the other, because one merchant never has two at once.

    The first is closed before the second starts. That is not incidental to what this asserts:
    a merchant may only have one run executing, so a comparison of two runs of one catalog is
    necessarily a comparison across time, and the pin is what says the catalog did not move in
    between.
    """
    merchant_id = await shop(session)
    await published(session, mission("one", budget_minor=PRICE))
    service = BenchmarkRunService(session)

    first = await service.start_run(suite_key="test-suite", suite_version=1, merchant_slug=SLUG)
    await service.abort_run(first.id, merchant_id=merchant_id)
    second = await service.start_run(suite_key="test-suite", suite_version=1, merchant_slug=SLUG)

    assert first.catalog_hash == second.catalog_hash


async def test_a_price_change_moves_the_catalog_pin() -> None:
    """The pin is what makes a before and after comparison attributable, or visibly not."""
    entry = CatalogEntry(
        variant_id=uuid.uuid7(),
        sku="AMP-1",
        product_category="chargers",
        attributes={"color": "black"},
        price_amount_minor=400000,
        currency=CURRENCY,
        inventory_quantity=3,
    )
    dearer = CatalogEntry(
        variant_id=entry.variant_id,
        sku="AMP-2",
        product_category="chargers",
        attributes={"color": "black"},
        price_amount_minor=450000,
        currency=CURRENCY,
        inventory_quantity=3,
    )

    repriced = CatalogEntry(
        variant_id=entry.variant_id,
        sku="AMP-1",
        product_category="chargers",
        attributes={"color": "black"},
        price_amount_minor=450000,
        currency=CURRENCY,
        inventory_quantity=3,
    )

    assert catalog_content_hash([entry]) != catalog_content_hash([repriced])
    # And it does not depend on the order rows came back in.
    assert catalog_content_hash([entry, dearer]) == catalog_content_hash([dearer, entry])


async def test_a_suite_cannot_be_run_against_another_merchant(session: AsyncSession) -> None:
    await build_shop(session, "ampere-supply")
    await published(session, mission("one", budget_minor=PRICE))

    with pytest.raises(ValueError, match="was authored against merchant"):
        await BenchmarkRunService(session).start_run(
            suite_key="test-suite", suite_version=1, merchant_slug="ampere-supply"
        )


async def test_an_unpublished_suite_cannot_be_run(session: AsyncSession) -> None:
    await shop(session)

    with pytest.raises(NotFoundError, match="benchmark_suite"):
        await BenchmarkRunService(session).start_run(
            suite_key="nothing-here", suite_version=1, merchant_slug=SLUG
        )


# Recording, and the oracle check.


async def test_a_compliant_purchase_is_recorded_as_a_success(session: AsyncSession) -> None:
    built = await build_shop(session, SLUG)
    await published(session, mission("one", budget_minor=PRICE, constraints=(BLACK,)))
    checkout_id = await quote(session, built)
    attempt = await admit(session, built, checkout_id, key="bench-run-01")
    # Settled through the real payment repository, so the row this test points at is one the
    # payment kernel would recognise rather than one the test wrote by hand.
    payments = PaymentAttemptRepository(session)
    await payments.mark_in_flight(attempt)
    await payments.mark_succeeded(
        attempt, provider_reference="bench-ref-01", source=OutcomeSource.EXECUTION
    )
    await session.commit()

    service = BenchmarkRunService(session)
    run = await service.start_run(suite_key="test-suite", suite_version=1, merchant_slug=SLUG)
    result = await service.record_result(
        run.id,
        "one",
        purchase(
            built.variant_id, built.merchant_id, checkout_id=checkout_id, attempt_id=attempt.id
        ),
        merchant_id=built.merchant_id,
    )

    assert result.status is MissionRunStatus.SUCCEEDED
    assert result.primary_failure_reason is None
    assert result.selected_variant_id == built.variant_id
    assert result.selected_quantity == 1
    assert result.checkout_id == checkout_id
    assert result.payment_attempt_id == attempt.id
    # The catalog offers a qualifying variant, which is what the mission's oracle claimed.
    assert result.oracle_confirmed is True


async def test_a_stale_oracle_is_recorded_without_changing_the_result(
    session: AsyncSession,
) -> None:
    """A mission authored when something was in stock quietly becomes impossible."""
    built = await build_shop(session, SLUG, inventory=0)
    await published(session, mission("one", budget_minor=PRICE, constraints=(BLACK,)))

    service = BenchmarkRunService(session)
    run = await service.start_run(suite_key="test-suite", suite_version=1, merchant_slug=SLUG)
    result = await service.record_result(
        run.id,
        "one",
        ObservedResult(
            merchant_id=built.merchant_id,
            abstention=ObservedAbstention(code=AbstentionCode.NO_COMPLIANT_CANDIDATE),
        ),
        merchant_id=built.merchant_id,
    )

    assert result.oracle_confirmed is False
    # The executor is still marked down, because overruling the authored oracle would mean the
    # benchmark had no ground truth at all. The disagreement is what a reader discounts it by.
    assert result.status is MissionRunStatus.ABSTAINED
    assert result.primary_failure_reason is FailureReason.DISCOVERY_FAILURE


async def test_a_variant_the_merchant_does_not_sell_is_caught_by_the_catalog(
    session: AsyncSession,
) -> None:
    """Reachable without waiting for the merchant to volunteer a particular refusal."""
    built = await build_shop(session, SLUG)
    await published(session, mission("one", budget_minor=PRICE, constraints=(BLACK,)))

    service = BenchmarkRunService(session)
    run = await service.start_run(suite_key="test-suite", suite_version=1, merchant_slug=SLUG)
    result = await service.record_result(
        run.id,
        "one",
        purchase(uuid.uuid7(), built.merchant_id),
        merchant_id=built.merchant_id,
    )

    assert result.primary_failure_reason is FailureReason.INVALID_VARIANT
    assert result.unsafe_completion
    # Nothing was recorded for a variant this merchant does not have.
    assert result.selected_variant_id is None


async def test_an_unsettled_payment_reference_is_not_recorded(session: AsyncSession) -> None:
    """A recorded reference is one that was looked up, never one an executor claimed."""
    built = await build_shop(session, SLUG)
    await published(session, mission("one", budget_minor=PRICE, constraints=(BLACK,)))
    checkout_id = await quote(session, built)
    attempt = await admit(session, built, checkout_id, key="bench-run-02")
    await session.commit()

    service = BenchmarkRunService(session)
    run = await service.start_run(suite_key="test-suite", suite_version=1, merchant_slug=SLUG)
    result = await service.record_result(
        run.id,
        "one",
        purchase(
            built.variant_id, built.merchant_id, checkout_id=checkout_id, attempt_id=attempt.id
        ),
        merchant_id=built.merchant_id,
    )

    # The attempt is only ADMITTED, so the reference is left null rather than recording a
    # settlement the payment table does not agree with.
    assert result.payment_attempt_id is None
    assert result.checkout_id == checkout_id


async def test_a_mission_is_recorded_once(session: AsyncSession) -> None:
    built = await build_shop(session, SLUG)
    await published(session, mission("one", budget_minor=PRICE, constraints=(BLACK,)))
    service = BenchmarkRunService(session)
    run = await service.start_run(suite_key="test-suite", suite_version=1, merchant_slug=SLUG)
    observed = purchase(built.variant_id, built.merchant_id)
    await service.record_result(run.id, "one", observed, merchant_id=built.merchant_id)

    with pytest.raises(ConflictError, match="already been executed"):
        await service.record_result(run.id, "one", observed, merchant_id=built.merchant_id)


async def test_another_merchant_cannot_record_into_this_run(session: AsyncSession) -> None:
    built = await build_shop(session, SLUG)
    await published(session, mission("one", budget_minor=PRICE, constraints=(BLACK,)))
    service = BenchmarkRunService(session)
    run = await service.start_run(suite_key="test-suite", suite_version=1, merchant_slug=SLUG)

    with pytest.raises(NotFoundError, match="benchmark_run"):
        await service.record_result(
            run.id,
            "one",
            purchase(built.variant_id, built.merchant_id),
            merchant_id=uuid.uuid7(),
        )


# Closing a run.


async def test_a_run_with_unexecuted_missions_cannot_be_completed(
    session: AsyncSession,
) -> None:
    """A partial run presented as a complete one reports a rate over a denominator nobody chose."""
    built = await build_shop(session, SLUG)
    await published(
        session,
        mission("one", budget_minor=PRICE, constraints=(BLACK,)),
        mission("two", budget_minor=PRICE, constraints=(BLACK,)),
    )
    service = BenchmarkRunService(session)
    run = await service.start_run(suite_key="test-suite", suite_version=1, merchant_slug=SLUG)
    await service.record_result(
        run.id, "one", purchase(built.variant_id, built.merchant_id), merchant_id=built.merchant_id
    )

    with pytest.raises(ConflictError, match="missions that never executed"):
        await service.complete_run(run.id, merchant_id=built.merchant_id)


async def test_a_run_that_stopped_early_is_aborted_and_keeps_what_it_recorded(
    session: AsyncSession,
) -> None:
    built = await build_shop(session, SLUG)
    await published(
        session,
        mission("one", budget_minor=PRICE, constraints=(BLACK,)),
        mission("two", budget_minor=PRICE, constraints=(BLACK,)),
    )
    service = BenchmarkRunService(session)
    run = await service.start_run(suite_key="test-suite", suite_version=1, merchant_slug=SLUG)
    await service.record_result(
        run.id, "one", purchase(built.variant_id, built.merchant_id), merchant_id=built.merchant_id
    )

    aborted = await service.abort_run(run.id, merchant_id=built.merchant_id)
    metrics = await service.metrics(run.id, merchant_id=built.merchant_id)

    assert aborted.status is BenchmarkRunStatus.ABORTED
    assert metrics.missions_unfinished == 1
    # Two purchasable missions, one completed. The denominator is the suite's, not what ran.
    assert metrics.purchase_missions == 2
    assert metrics.task_completion_rate == pytest.approx(0.5)


async def test_a_finished_run_records_nothing_further(session: AsyncSession) -> None:
    built = await build_shop(session, SLUG)
    await published(session, mission("one", budget_minor=PRICE, constraints=(BLACK,)))
    service = BenchmarkRunService(session)
    run = await service.start_run(suite_key="test-suite", suite_version=1, merchant_slug=SLUG)
    await service.abort_run(run.id, merchant_id=built.merchant_id)

    with pytest.raises(ConflictError, match="records nothing"):
        await service.record_result(
            run.id,
            "one",
            purchase(built.variant_id, built.merchant_id),
            merchant_id=built.merchant_id,
        )


# End to end, through the executor seam.


async def test_a_whole_suite_runs_in_order_and_reports(session: AsyncSession) -> None:
    """The deterministic runner, end to end, with no LLM anywhere in it."""
    built = await build_shop(session, SLUG)
    await published(
        session,
        mission("buy-one", budget_minor=PRICE, constraints=(BLACK,)),
        mission("over-budget", budget_minor=PRICE, constraints=(BLACK,)),
        mission(
            "nothing-fits",
            budget_minor=PRICE,
            constraints=(BLACK,),
            outcome=ExpectedOutcome.NO_ACCEPTABLE_PURCHASE,
        ),
    )
    await registered(session)
    service = BenchmarkRunService(session)

    run = await service.run_suite(
        executor_from(
            {
                "buy-one": purchase(built.variant_id, built.merchant_id),
                "over-budget": purchase(built.variant_id, built.merchant_id, price=PRICE * 10),
                "nothing-fits": ObservedResult(
                    merchant_id=built.merchant_id,
                    abstention=ObservedAbstention(code=AbstentionCode.NO_COMPLIANT_CANDIDATE),
                ),
            }
        ),
        suite_key="test-suite",
        suite_version=1,
        fixture=WORLD,
        representation_label="baseline",
    )
    metrics = await service.metrics(run.id, merchant_id=built.merchant_id)

    assert run.status is BenchmarkRunStatus.COMPLETED
    assert metrics.missions_total == 3
    assert metrics.missions_succeeded == 1
    assert metrics.missions_failed == 1
    assert metrics.correct_abstentions == 1
    assert metrics.unsafe_completions == 1
    assert metrics.primary_failure_counts == {FailureReason.BUDGET_EXCEEDED: 1}
    demand = metrics.simulated_demand.single_currency()
    assert demand.potential_amount_minor == VALUE * 2
    assert demand.captured_amount_minor == VALUE
    assert demand.lost_amount_minor == VALUE
    assert demand.not_measured_amount_minor == 0


async def test_an_executor_with_no_result_for_a_mission_stops_the_run(
    session: AsyncSession,
) -> None:
    """A silently skipped mission is a run with a smaller denominator than the suite it claims."""
    built = await build_shop(session, SLUG)
    await published(
        session,
        mission("one", budget_minor=PRICE, constraints=(BLACK,)),
        mission("two", budget_minor=PRICE, constraints=(BLACK,)),
    )

    await registered(session)

    with pytest.raises(KeyError, match="two"):
        await BenchmarkRunService(session).run_suite(
            executor_from({"one": purchase(built.variant_id, built.merchant_id)}),
            suite_key="test-suite",
            suite_version=1,
            fixture=WORLD,
        )


def test_the_world_and_the_shop_helper_describe_the_same_catalog() -> None:
    """`build_shop` writes the rows and `WORLD` restores them, so the two have to agree.

    Stated rather than assumed. If either helper's naming moved, preparation would deactivate
    the variant `build_shop` created and every test below would quietly become a test about
    withdrawn variants without failing.
    """
    skus = {variant.sku for product in WORLD.products for variant in product.variants}

    assert skus == {"TEST-MERCHANT-BLACK"}
    assert {product.external_id for product in WORLD.products} == {"test-merchant-1"}
    assert WORLD.merchant_slug == SLUG


async def test_the_executor_is_handed_briefs_and_never_an_oracle(
    session: AsyncSession,
) -> None:
    """The separation, asserted at the seam an agent will actually sit behind."""
    built = await build_shop(session, SLUG)
    await published(
        session,
        mission(
            "control",
            budget_minor=PRICE,
            constraints=(BLACK,),
            outcome=ExpectedOutcome.NO_ACCEPTABLE_PURCHASE,
        ),
    )
    seen: list[AgentMissionBrief] = []

    class Recording:
        identity = ExecutorIdentity(kind="recording", version=1)

        async def __call__(
            self, brief: AgentMissionBrief, *, merchant_id: uuid.UUID
        ) -> ObservedResult:
            del merchant_id
            seen.append(brief)
            return ObservedResult(
                merchant_id=built.merchant_id,
                abstention=ObservedAbstention(code=AbstentionCode.NO_COMPLIANT_CANDIDATE),
            )

    await registered(session)

    await BenchmarkRunService(session).run_suite(
        Recording(), suite_key="test-suite", suite_version=1, fixture=WORLD
    )

    assert len(seen) == 1
    assert isinstance(seen[0], AgentMissionBrief)
    # Set equality rather than negative membership. A field added later would slip past a list of
    # names nobody thought to extend, and the one field that must never appear here is the one
    # nobody has invented yet.
    assert set(AgentMissionBrief.__dataclass_fields__) == {
        "key",
        "objective",
        "budget",
        "quantity",
        "hard_constraints",
        "preferences",
    }
