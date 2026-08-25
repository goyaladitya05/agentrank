"""What the deterministic half of a workspace bootstrap is allowed to read.

Every claim this phase makes about its own honesty reduces to one thing: the world and the
workload are derived from frozen merchant evidence and from nothing downstream of it. A comment
saying so is a comment, and a grep somebody runs once is a grep. This asserts it against the
module graph, so an import that quietly makes a benchmark depend on the compiler's output, on a
previous run's failures or on a model is a failing test rather than a methodology defect nobody
notices.

The boundary asserted is the direct import list of the two modules that decide what a merchant's
world holds and what the benchmark asks of them, and the transitive closure is deliberately not
asserted. That is a limitation worth stating rather than hiding: `benchmark.catalog` carries both
the catalog pin and the ground-truth predicate, so it legitimately reaches the evaluator's
vocabulary, and the evaluator in turn imports a payment status enum from a models module. The
closure therefore reaches SQLAlchemy through a chain in which nothing reads a row, and a test
over it would fail for a reason that says nothing about this package.

What is asserted instead is exact and is the thing that would actually go wrong: neither module
names a producer or reader of results, neither names a session, a clock or a random source, and
nothing anywhere in the package names a model provider.
"""

import ast
import pathlib

PACKAGE = pathlib.Path("apps/api/src/agentrank_api")
WORKSPACE = PACKAGE / "workspace"

# Modules that produce, interpret or read a benchmark result, and everything that reaches a
# model. A generated workload naming any of them would be a workload derived from its own
# subject, which is the one defect this phase cannot recover from after the fact.
DOWNSTREAM = (
    "agentrank_api.compiler",
    "agentrank_api.diagnostics",
    "agentrank_api.benchmark.runner",
    "agentrank_api.benchmark.evaluation",
    "agentrank_api.benchmark.report",
    "agentrank_api.benchmark.metrics",
    "agentrank_api.benchmark.observation",
    "agentrank_api.benchmark.experiment",
    "agentrank_api.benchmark.dispatch",
    "agentrank_api.benchmark.llm",
    "agentrank_api.benchmark.buyer",
    "agentrank_api.benchmark.isolation",
    "agentrank_api.benchmark.agent_trace",
)

# The two modules that decide what a merchant's world holds and what the benchmark asks of them.
DETERMINISTIC = ("projection.py", "generation.py")

# Everything that could make one bootstrap differ from another of the same evidence, or could
# make one spend something.
IMPURE = ("sqlalchemy", "datetime", "random", "time", "secrets", "os", "httpx", "requests")

# Whatever a model provider is reached through, wherever it is reached from.
PROVIDERS = ("agentrank_api.benchmark.llm", "openai", "google", "genai", "anthropic")


def imports(path: pathlib.Path) -> set[str]:
    """Every module one file imports, by the name it names them with."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    named: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            named.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            named.add(node.module)
    return named


def names(module: str, prefix: str) -> bool:
    """Whether one imported name is that module or something inside it."""
    return module == prefix or module.startswith(f"{prefix}.")


def test_the_generator_names_nothing_that_produces_or_reads_a_result() -> None:
    """No compiler output, no diagnostics, no previous run, no trace, no model.

    A mission's expected outcome is the answer key an executor is marked against. Deriving one
    from anything on this list would be deriving it from the thing under test.
    """
    for module in DETERMINISTIC:
        named = imports(WORKSPACE / module)
        offending = sorted(
            name for name in named for forbidden in DOWNSTREAM if names(name, forbidden)
        )
        assert not offending, f"{module} imports {offending}"


def test_the_generator_names_the_predicate_a_run_recomputes_ground_truth_with() -> None:
    """The positive half, and the reason the list above can be as short as it is.

    Ground truth is computed by `benchmark.catalog.satisfies`, which is the same predicate a run
    recomputes a mission's outcome with while it executes. A second implementation could disagree
    with the one a run checks it against, and the disagreement would be reported as a fact about
    the merchant.
    """
    assert "agentrank_api.benchmark.catalog" in imports(WORKSPACE / "generation.py")


def test_the_generator_names_no_session_no_clock_and_no_random_source() -> None:
    """Pure, so the same evidence produces the same benchmark in every process and database.

    A clock would make two bootstraps of one snapshot differ, a session would let a generated
    mission depend on whatever else was in the database, and a random source would make a
    merchant's benchmark unreproducible while looking identical in every report.
    """
    for module in DETERMINISTIC:
        named = imports(WORKSPACE / module)
        offending = sorted(name for name in named for impure in IMPURE if names(name, impure))
        assert not offending, f"{module} imports {offending}"


def test_no_part_of_the_bootstrap_names_a_model_provider() -> None:
    """Building a setup is deterministic and spends nothing, asserted rather than described."""
    for path in sorted(WORKSPACE.glob("*.py")):
        named = imports(path)
        offending = sorted(
            name for name in named for provider in PROVIDERS if names(name, provider)
        )
        assert not offending, f"{path.name} imports {offending}"


def test_the_projection_reads_the_merchants_own_evidence_and_nothing_else() -> None:
    """One input to a generated world, stated as an import.

    `definitions` is the frozen source document itself. `schemas` is here for two integer bounds
    and nothing else: the intake already refuses a document larger than an evaluation catalog can
    hold, and importing those numbers rather than restating them is what stops the two from
    disagreeing. A third name in this list would be a second input to a merchant's benchmark, and
    that is a decision somebody has to make here rather than one that arrives with a helper.
    """
    named = imports(WORKSPACE / "projection.py")
    sources = sorted(name for name in named if name.startswith("agentrank_api.representation"))
    assert sources == [
        "agentrank_api.representation.definitions",
        "agentrank_api.representation.schemas",
    ]
