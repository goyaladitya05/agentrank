"""Publishing and reading benchmark suite definitions.

One workflow, and it is the one that turns an authored definition into the historical record
a run can point at: validate it, give it a content identity, and refuse to let an existing
version mean something new.

Publishing is convergent rather than idempotent by accident. Publishing the same definition
twice returns the suite that already exists and writes nothing, exactly as seeding the
development catalog does. Publishing a *different* definition under an existing key and
version is refused, and that refusal is the whole reproducibility guarantee: a historical run
names a suite row, the row cannot be updated, and a new row cannot take its identity.

There is no update and no delete here, and no HTTP route anywhere. Suites are global templates
published by an operator, and until a buyer agent or a console needs to read them over the
wire there is nothing for an endpoint to serve. See docs/architecture.md.
"""

from sqlalchemy.ext.asyncio import AsyncSession

from agentrank_api.benchmark.definitions import BenchmarkSuiteDefinition
from agentrank_api.benchmark.identity import suite_content_hash
from agentrank_api.benchmark.models import BenchmarkSuite
from agentrank_api.benchmark.repository import BenchmarkSuiteRepository
from agentrank_api.conflicts import translated_conflicts
from agentrank_api.errors import ConflictError, NotFoundError

SUITE_RESOURCE = "benchmark_suite"

# The refusal `conflicts.py` produces when two publishes of one brand new version race. Named
# here because this service is what turns losing that race back into an ordinary answer.
ALREADY_PUBLISHED = "suite_already_published"


class BenchmarkSuiteService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._suites = BenchmarkSuiteRepository(session)

    async def publish(self, definition: BenchmarkSuiteDefinition) -> BenchmarkSuite:
        """Publish one suite definition, or return the identical one already published.

        Three outcomes and no fourth:

        - nothing exists under this key and version, so the definition is written
        - something exists and its content hash matches, so it is returned untouched
        - something exists and its content hash differs, so the call is refused

        The third is what stops a fixture edit from rewriting history. The suite rows cannot
        be updated, so the only way an existing version could come to mean something else is
        by being replaced, and there is no code path here that replaces one.

        The read answers first and answers better, naming the version. The unique constraint on
        key and version answers when two publishes race, and the loser then re-reads and takes
        the same three way decision the winner did. That is what makes the convergence claim
        above true under concurrency as well as in sequence: two processes publishing the same
        definition both get the suite, and two publishing different ones both get told which
        digest is already there.
        """
        content_hash = suite_content_hash(definition)
        existing = await self._suites.get(definition.key, definition.version)
        if existing is not None:
            _require_same_content(existing, content_hash)
            return existing

        try:
            async with translated_conflicts(self._session, identifier=definition.label):
                suite = await self._suites.create(definition)
        except ConflictError as conflict:
            if conflict.reason != ALREADY_PUBLISHED:
                raise
            return await self._published_by_the_winner(definition, content_hash)

        await self._session.commit()
        return suite

    async def _published_by_the_winner(
        self, definition: BenchmarkSuiteDefinition, content_hash: str
    ) -> BenchmarkSuite:
        """Resolve a lost publish race by reading what the winner wrote.

        The transaction was already rolled back by the conflict translation, so this read sees
        the committed row. A version that is somehow still absent means the constraint fired
        for a reason nobody can explain, and that is a bug rather than a refusal.
        """
        existing = await self._suites.get(definition.key, definition.version)
        if existing is None:
            raise ConflictError(
                ALREADY_PUBLISHED,
                f"benchmark suite {definition.label} could not be published or read back",
                resource=SUITE_RESOURCE,
                identifier=definition.label,
            )
        _require_same_content(existing, content_hash)
        return existing

    async def get(self, key: str, version: int) -> BenchmarkSuite:
        """One published suite, raising rather than returning None.

        Absence is not an empty workload. A run against a suite nobody published would have no
        missions and would report a perfect score over nothing at all, which is the most
        flattering possible way for this benchmark to be wrong.
        """
        suite = await self._suites.get(key, version)
        if suite is None:
            raise NotFoundError(SUITE_RESOURCE, f"{key}@{version}")
        return suite

    async def versions(self, key: str) -> list[BenchmarkSuite]:
        """Every published version of one suite key, oldest first."""
        return await self._suites.list_versions(key)


def _require_same_content(existing: BenchmarkSuite, content_hash: str) -> None:
    """Refuse a definition that would give an existing version new meaning.

    The message names both digests on purpose. An author who edited a fixture and forgot to
    bump the version needs to see that the content changed, not merely that something is
    already there.
    """
    if existing.definition_hash == content_hash:
        return
    raise ConflictError(
        "suite_definition_changed",
        f"benchmark suite {existing.label} is already published with content"
        f" {existing.definition_hash} and this definition is {content_hash}."
        " Publish a new version rather than changing what an existing one measured",
        resource=SUITE_RESOURCE,
        identifier=existing.label,
    )
