"""The merchant import workflow end to end, against a real storefront on a real socket.

Everything here goes through the whole thing: the HTTP endpoint a browser calls, the network
boundary, the extraction, the record, the confirmation, and the ordinary source snapshot that
comes out. The storefront is the synthetic fixture in `importer_support`, served over loopback,
which the test process reaches only because it constructs settings that permit it.

The properties under test are the ones the phase turns on:

```text
nothing is history until confirmed    running an import writes no snapshot
confirmation is ordinary              the same intake, schema and identity as the source editor
nothing is invented                   a stock level is stated by the merchant or is zero evidence
re-import converges                   an unchanged storefront writes no second snapshot
isolation holds                       another merchant's import is an unknown one
```

The application under test is driven in this test's own event loop through an ASGI transport
rather than through a blocking test client, because the fixture storefront is an asyncio server in
that same loop: a client that blocked the loop waiting for a response would be blocking the server
that has to produce it.
"""

import asyncio
import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx2
import pytest
from conftest import CredentialIssuer, bearer
from importer_support import (
    IMPORTS,
    CannedResponse,
    MerchantFixtureServer,
    Storefront,
    import_command,
    importing_settings,
    page,
)
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import agentrank_api.importer.service as service_module
from agentrank_api.commerce.repository import MerchantRepository
from agentrank_api.config import Settings
from agentrank_api.importer.draft import SourceDraft, canonical_document
from agentrank_api.importer.models import MerchantSourceImport
from agentrank_api.main import create_app
from agentrank_api.payments.fake import FakePaymentProvider
from agentrank_api.representation.intake import MerchantSourceIntakeService
from agentrank_api.representation.models import MerchantSourceSubmission, SourceOrigin
from agentrank_api.representation.schemas import SourceDocumentInput
from agentrank_api.workspace.service import MerchantEvaluationWorkspaceService

pytestmark = pytest.mark.anyio

SOURCES = "/api/v1/sources"
FIRST = "import-request-one"
SECOND = "import-request-two"


async def api(
    settings: Settings, sessions: async_sessionmaker[AsyncSession]
) -> AsyncIterator[httpx2.AsyncClient]:
    app = create_app(importing_settings(settings), payment_provider=FakePaymentProvider())
    app.state.session_factory = sessions
    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app), base_url="http://api.test"
    ) as client:
        yield client


async def public_api(
    settings: Settings, sessions: async_sessionmaker[AsyncSession]
) -> AsyncIterator[httpx2.AsyncClient]:
    """The application as a deployment configures it: no network allowance at all."""
    app = create_app(settings, payment_provider=FakePaymentProvider())
    app.state.session_factory = sessions
    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app), base_url="http://api.test"
    ) as client:
        yield client


async def merchant(session: AsyncSession, slug: str) -> Any:
    created = await MerchantRepository(session).create(slug=slug, name="Import Shop")
    await session.commit()
    return created


def catalog_pages(server: MerchantFixtureServer) -> list[dict[str, object]]:
    """The storefront pages a merchant would name: four products and two policies."""
    return [
        page(server.url("/p/charger")),
        page(server.url("/p/cable")),
        page(server.url("/p/dock")),
        page(server.url("/p/sleeve")),
        page(server.url("/returns"), "POLICY", "returns"),
        page(server.url("/warranty"), "POLICY", "warranty"),
    ]


async def test_a_merchant_imports_their_own_pages_and_reads_what_was_found(
    settings: Settings,
    session: AsyncSession,
    factory: async_sessionmaker[AsyncSession],
    issue_credential: CredentialIssuer,
) -> None:
    """The ordinary path, asserted as counts a merchant would read on the page."""
    shop = await merchant(session, "import-basic")
    token = await issue_credential(shop.id)
    async with MerchantFixtureServer(Storefront.voltedge().routes) as server:
        async for http in api(settings, factory):
            response = await http.post(
                IMPORTS,
                headers=bearer(token),
                json=import_command(catalog_pages(server), FIRST),
            )
            assert response.status_code == 201
            body = response.json()

    assert body["summary"]["state"] == "COMPLETED"
    assert body["summary"]["page_count"] == 6
    assert body["summary"]["retrieved_count"] == 6
    assert body["summary"]["product_count"] == 4
    assert body["summary"]["policy_count"] == 2
    assert {policy["name"] for policy in body["policies"]} == {"returns", "warranty"}
    assert {product["extraction"] for product in body["products"]} == {
        "STRUCTURED_DATA",
        "PAGE_METADATA",
    }
    # Every imported product names the page it came from, and the pages are the merchant's own.
    assert all(product["source_url"].startswith(server.origin) for product in body["products"])
    # Nothing has become source history, and the read says what stands in the way.
    assert body["summary"]["source_snapshot_id"] is None
    assert body["stock_level_required"] is True
    assert body["confirmable"] is True


async def test_running_an_import_creates_no_source_snapshot(
    settings: Settings,
    session: AsyncSession,
    factory: async_sessionmaker[AsyncSession],
    issue_credential: CredentialIssuer,
) -> None:
    """The load bearing separation: retrieval is not a statement about a catalog."""
    shop = await merchant(session, "import-no-snapshot")
    token = await issue_credential(shop.id)
    async with MerchantFixtureServer(Storefront.voltedge().routes) as server:
        async for http in api(settings, factory):
            await http.post(
                IMPORTS,
                headers=bearer(token),
                json=import_command([page(server.url("/p/charger"))], FIRST),
            )
    assert await MerchantSourceIntakeService(session).current(shop.id) is None


async def test_a_page_that_cannot_be_read_is_omitted_by_name_and_the_rest_still_import(
    settings: Settings,
    session: AsyncSession,
    factory: async_sessionmaker[AsyncSession],
    issue_credential: CredentialIssuer,
) -> None:
    """One unreadable page does not fail an import, and the merchant is told which and why."""
    shop = await merchant(session, "import-omissions")
    token = await issue_credential(shop.id)
    async with MerchantFixtureServer(Storefront.voltedge().routes) as server:
        pages = [
            page(server.url("/p/charger")),
            page(server.url("/p/mystery")),
            page(server.url("/p/two-prices")),
            page(server.url("/p/injection")),
            page(server.url("/missing")),
        ]
        async for http in api(settings, factory):
            body = (
                await http.post(IMPORTS, headers=bearer(token), json=import_command(pages, FIRST))
            ).json()

    assert body["summary"]["product_count"] == 1
    reasons = {note["code"] for note in body["omissions"]}
    assert reasons == {"currency_missing", "price_conflict", "instruction_like", "http_error"}
    assert all(note["detail"] for note in body["omissions"])


async def test_the_same_request_key_fetches_the_storefront_once(
    settings: Settings,
    session: AsyncSession,
    factory: async_sessionmaker[AsyncSession],
    issue_credential: CredentialIssuer,
) -> None:
    """Pressing import twice is one import, asserted from the storefront's own request log."""
    shop = await merchant(session, "import-idempotent")
    token = await issue_credential(shop.id)
    async with MerchantFixtureServer(Storefront.voltedge().routes) as server:
        command = import_command([page(server.url("/p/charger"))], FIRST)
        async for http in api(settings, factory):
            first = await http.post(IMPORTS, headers=bearer(token), json=command)
            second = await http.post(IMPORTS, headers=bearer(token), json=command)
        assert len(server.requests) == 1
    assert first.json()["summary"]["import_id"] == second.json()["summary"]["import_id"]


async def test_confirming_an_import_writes_an_ordinary_immutable_source_snapshot(
    settings: Settings,
    session: AsyncSession,
    factory: async_sessionmaker[AsyncSession],
    issue_credential: CredentialIssuer,
) -> None:
    """What comes out is a snapshot like any other, and it records that it came from an import."""
    shop = await merchant(session, "import-confirm")
    token = await issue_credential(shop.id)
    async with MerchantFixtureServer(Storefront.voltedge().routes) as server:
        async for http in api(settings, factory):
            record = (
                await http.post(
                    IMPORTS,
                    headers=bearer(token),
                    json=import_command(catalog_pages(server), FIRST),
                )
            ).json()
            confirmed = await http.post(
                f"{IMPORTS}/{record['summary']['import_id']}/confirm",
                headers=bearer(token),
                json={"stock_level": 8},
            )
            assert confirmed.status_code == 201
            outcome = confirmed.json()
            snapshot = (
                await http.get(f"{SOURCES}/{outcome['source_snapshot_id']}", headers=bearer(token))
            ).json()

    assert outcome["created_snapshot"] is True
    assert outcome["source_label"] == "merchant-source@1"
    assert snapshot["summary"]["origin"] == "MERCHANT_IMPORT"
    assert snapshot["summary"]["product_count"] == 4
    assert snapshot["summary"]["policy_count"] == 2
    document = snapshot["document"]
    # The stock level is the merchant's, and the document says so for every variant that got it.
    sources = {
        variant["merchant_metadata"]["import_stock_level_source"]
        for product in document["products"]
        for variant in product["variants"]
    }
    assert sources == {"MERCHANT_SUPPLIED", "PAGE_OUT_OF_STOCK"}
    quantities = {
        variant["merchant_metadata"]["import_availability"]: variant["inventory_quantity"]
        for product in document["products"]
        for variant in product["variants"]
    }
    assert quantities["OUT_OF_STOCK"] == 0
    assert quantities["IN_STOCK"] == 8
    # Provenance that survives into source history is the page and the method, never a timestamp.
    metadata = document["products"][0]["merchant_metadata"]
    assert metadata["import_source_url"].startswith(server.origin)
    assert metadata["import_extraction"] in {"STRUCTURED_DATA", "PAGE_METADATA"}


async def test_an_import_is_not_confirmable_until_the_stock_level_is_stated(
    settings: Settings,
    session: AsyncSession,
    factory: async_sessionmaker[AsyncSession],
    issue_credential: CredentialIssuer,
) -> None:
    """The refusal that keeps a public "In stock" from becoming a number nobody published."""
    shop = await merchant(session, "import-stock")
    token = await issue_credential(shop.id)
    async with MerchantFixtureServer(Storefront.voltedge().routes) as server:
        async for http in api(settings, factory):
            record = (
                await http.post(
                    IMPORTS,
                    headers=bearer(token),
                    json=import_command([page(server.url("/p/charger"))], FIRST),
                )
            ).json()
            refused = await http.post(
                f"{IMPORTS}/{record['summary']['import_id']}/confirm",
                headers=bearer(token),
                json={},
            )
    assert refused.status_code == 409
    assert refused.json()["error"] == "stock_level_required"
    assert await MerchantSourceIntakeService(session).current(shop.id) is None


async def test_an_import_whose_pages_all_say_out_of_stock_needs_no_stated_number(
    settings: Settings,
    session: AsyncSession,
    factory: async_sessionmaker[AsyncSession],
    issue_credential: CredentialIssuer,
) -> None:
    shop = await merchant(session, "import-oos")
    token = await issue_credential(shop.id)
    async with MerchantFixtureServer(Storefront.voltedge().routes) as server:
        async for http in api(settings, factory):
            record = (
                await http.post(
                    IMPORTS,
                    headers=bearer(token),
                    json=import_command([page(server.url("/p/dock"))], FIRST),
                )
            ).json()
            assert record["stock_level_required"] is False
            confirmed = await http.post(
                f"{IMPORTS}/{record['summary']['import_id']}/confirm",
                headers=bearer(token),
                json={},
            )
    assert confirmed.status_code == 201
    assert confirmed.json()["created_snapshot"] is True


async def test_confirming_twice_is_one_submission_and_one_snapshot(
    settings: Settings,
    session: AsyncSession,
    factory: async_sessionmaker[AsyncSession],
    issue_credential: CredentialIssuer,
) -> None:
    """A retry after a response nobody saw learns what happened rather than writing again."""
    shop = await merchant(session, "import-confirm-twice")
    token = await issue_credential(shop.id)
    async with MerchantFixtureServer(Storefront.voltedge().routes) as server:
        async for http in api(settings, factory):
            record = (
                await http.post(
                    IMPORTS,
                    headers=bearer(token),
                    json=import_command([page(server.url("/p/charger"))], FIRST),
                )
            ).json()
            target = f"{IMPORTS}/{record['summary']['import_id']}/confirm"
            first = await http.post(target, headers=bearer(token), json={"stock_level": 3})
            second = await http.post(target, headers=bearer(token), json={"stock_level": 3})

    assert first.json()["source_snapshot_id"] == second.json()["source_snapshot_id"]
    assert first.json()["already_confirmed"] is False
    assert second.json()["already_confirmed"] is True
    submissions = (
        (
            await session.execute(
                select(MerchantSourceSubmission).where(
                    MerchantSourceSubmission.merchant_id == shop.id
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(submissions) == 1


async def test_re_importing_an_unchanged_storefront_writes_no_second_snapshot(
    settings: Settings,
    session: AsyncSession,
    factory: async_sessionmaker[AsyncSession],
    issue_credential: CredentialIssuer,
) -> None:
    """Content identity, not a retrieval timestamp, decides whether history changed.

    This is what stops an importer from producing an endless series of identical source versions,
    and it is the reason no retrieval time reaches the canonical document.
    """
    shop = await merchant(session, "import-reimport")
    token = await issue_credential(shop.id)
    async with MerchantFixtureServer(Storefront.voltedge().routes) as server:
        async for http in api(settings, factory):
            pages = catalog_pages(server)
            first = (
                await http.post(IMPORTS, headers=bearer(token), json=import_command(pages, FIRST))
            ).json()
            await http.post(
                f"{IMPORTS}/{first['summary']['import_id']}/confirm",
                headers=bearer(token),
                json={"stock_level": 8},
            )
            second = (
                await http.post(IMPORTS, headers=bearer(token), json=import_command(pages, SECOND))
            ).json()
            again = await http.post(
                f"{IMPORTS}/{second['summary']['import_id']}/confirm",
                headers=bearer(token),
                json={"stock_level": 8},
            )
            overview = (await http.get(SOURCES, headers=bearer(token))).json()

    assert again.json()["created_snapshot"] is False
    assert [entry["source_version"] for entry in overview["snapshots"]] == [1]


async def test_a_changed_storefront_becomes_a_new_version_and_leaves_the_old_one_alone(
    settings: Settings,
    session: AsyncSession,
    factory: async_sessionmaker[AsyncSession],
    issue_credential: CredentialIssuer,
) -> None:
    shop = await merchant(session, "import-changed")
    token = await issue_credential(shop.id)
    storefront = Storefront.voltedge()
    async with MerchantFixtureServer(storefront.routes) as server:
        async for http in api(settings, factory):
            pages = [page(server.url("/p/charger"))]
            first = (
                await http.post(IMPORTS, headers=bearer(token), json=import_command(pages, FIRST))
            ).json()
            confirmed = await http.post(
                f"{IMPORTS}/{first['summary']['import_id']}/confirm",
                headers=bearer(token),
                json={"stock_level": 8},
            )
            original = confirmed.json()["source_snapshot_id"]
            before = (await http.get(f"{SOURCES}/{original}", headers=bearer(token))).json()[
                "summary"
            ]["content_hash"]

            # The merchant raises their price. Everything else about the page is unchanged.
            storefront.routes[server.url("/p/charger").removeprefix(server.origin)] = _repriced()
            second = (
                await http.post(IMPORTS, headers=bearer(token), json=import_command(pages, SECOND))
            ).json()
            again = await http.post(
                f"{IMPORTS}/{second['summary']['import_id']}/confirm",
                headers=bearer(token),
                json={"stock_level": 8},
            )
            after = (await http.get(f"{SOURCES}/{original}", headers=bearer(token))).json()[
                "summary"
            ]["content_hash"]

    assert again.json()["created_snapshot"] is True
    assert again.json()["source_label"] == "merchant-source@2"
    assert again.json()["source_snapshot_id"] != original
    assert before == after


def _repriced() -> Any:
    from importer_support import html

    return html(
        """<!doctype html>
<html><head><title>VoltEdge 65W Charger</title>
<script type="application/ld+json">
{"@context":"https://schema.org","@type":"Product",
 "name":"VoltEdge 65W GaN Charger","sku":"VE-65",
 "description":"A compact 65W charger.","category":"Chargers",
 "offers":{"@type":"Offer","price":"3999.00","priceCurrency":"INR",
           "availability":"https://schema.org/InStock","sku":"VE-65-BLK"}}
</script></head>
<body><h1>VoltEdge 65W GaN Charger</h1><p>Compact and fast.</p></body></html>
"""
    )


async def test_another_merchants_import_is_an_unknown_one(
    settings: Settings,
    session: AsyncSession,
    factory: async_sessionmaker[AsyncSession],
    issue_credential: CredentialIssuer,
) -> None:
    """Reading, confirming and listing are all scoped to the credential and to nothing else."""
    owner = await merchant(session, "import-owner")
    other = await merchant(session, "import-other")
    owner_token = await issue_credential(owner.id)
    other_token = await issue_credential(other.id)
    async with MerchantFixtureServer(Storefront.voltedge().routes) as server:
        async for http in api(settings, factory):
            record = (
                await http.post(
                    IMPORTS,
                    headers=bearer(owner_token),
                    json=import_command([page(server.url("/p/charger"))], FIRST),
                )
            ).json()
            import_id = record["summary"]["import_id"]
            read = await http.get(f"{IMPORTS}/{import_id}", headers=bearer(other_token))
            confirm = await http.post(
                f"{IMPORTS}/{import_id}/confirm",
                headers=bearer(other_token),
                json={"stock_level": 4},
            )
            listed = await http.get(IMPORTS, headers=bearer(other_token))

    assert read.status_code == 404
    assert confirm.status_code == 404
    assert listed.json() == []
    assert await MerchantSourceIntakeService(session).current(other.id) is None


async def test_an_unauthenticated_caller_reaches_nothing(
    settings: Settings, factory: async_sessionmaker[AsyncSession]
) -> None:
    async for http in api(settings, factory):
        assert (await http.get(IMPORTS)).status_code == 401
        assert (await http.post(IMPORTS, json={})).status_code == 401
        assert (await http.post(f"{IMPORTS}/{uuid.uuid4()}/confirm", json={})).status_code == 401


FORBIDDEN_TARGETS = [
    "http://127.0.0.1/p",
    "http://169.254.169.254/latest/meta-data/",
    "http://10.0.0.1/p",
    "http://[::1]/p",
    "file:///etc/passwd",
    "https://user:secret@shop.example/p",
    "https://shop.example:9000/p",
]


@pytest.mark.parametrize("url", FORBIDDEN_TARGETS)
async def test_a_deployment_refuses_every_target_outside_the_public_internet(
    settings: Settings,
    session: AsyncSession,
    factory: async_sessionmaker[AsyncSession],
    issue_credential: CredentialIssuer,
    url: str,
) -> None:
    """The refusal at the real HTTP boundary, on the configuration a deployment actually has.

    Deliberately not the widened settings the rest of this file uses. This is the process a
    deployment runs, where the importer reaches globally routable addresses on port 80 or 443 and
    nothing else, and where `Settings` refuses to load any allowance that would change that.
    """
    shop = await merchant(session, f"import-refuse-{abs(hash(url)) % 10_000}")
    token = await issue_credential(shop.id)
    async for http in public_api(settings, factory):
        response = await http.post(
            IMPORTS, headers=bearer(token), json=import_command([page(url)], FIRST)
        )
    assert response.status_code == 422
    assert (await session.execute(select(MerchantSourceImport))).scalars().first() is None


async def test_a_host_name_that_resolves_inward_fails_the_page_and_fetches_nothing(
    settings: Settings,
    session: AsyncSession,
    factory: async_sessionmaker[AsyncSession],
    issue_credential: CredentialIssuer,
) -> None:
    """`localhost` is a name, so it is settled where names are settled: at the resolution.

    A name is not refused when the command is validated, because whether it resolves anywhere at
    all is not a property of the request, and a transient resolver failure should not throw away
    an import of eleven other pages. It is refused immediately before the connection that would
    have used it, and the page carries the reason.
    """
    shop = await merchant(session, "import-localhost")
    token = await issue_credential(shop.id)
    async for http in public_api(settings, factory):
        body = (
            await http.post(
                IMPORTS,
                headers=bearer(token),
                json=import_command([page("http://localhost/p")], FIRST),
            )
        ).json()
    assert body["summary"]["product_count"] == 0
    assert body["pages"][0]["retrieved"] is False
    assert body["pages"][0]["reason"] == "address_not_permitted"
    assert body["pages"][0]["byte_count"] == 0
    assert [blocker["code"] for blocker in body["blockers"]] == ["no_products"]


@pytest.mark.parametrize(
    "url",
    [
        "http://169.254.169.254/latest/meta-data/",
        "http://10.0.0.1/p",
        "http://192.168.0.5/p",
        "file:///etc/passwd",
        "https://user:secret@shop.example/p",
    ],
)
async def test_a_widened_process_still_refuses_everything_but_the_network_it_named(
    settings: Settings,
    session: AsyncSession,
    factory: async_sessionmaker[AsyncSession],
    issue_credential: CredentialIssuer,
    url: str,
) -> None:
    """A loopback allowance permits loopback. It is not a way to reach the rest of the machine."""
    shop = await merchant(session, f"import-widened-{abs(hash(url)) % 10_000}")
    token = await issue_credential(shop.id)
    async for http in api(settings, factory):
        response = await http.post(
            IMPORTS, headers=bearer(token), json=import_command([page(url)], FIRST)
        )
    assert response.status_code == 422


async def test_a_deployment_cannot_be_configured_with_an_importer_network_allowance(
    settings: Settings,
) -> None:
    """The structural block. There is no environment that is a deployment and holds one."""
    for environment in ("production", "staging"):
        with pytest.raises(ValueError, match="AGENTRANK_IMPORT_ALLOWED_NETWORKS"):
            settings.model_copy(update={"environment": environment}).model_validate(
                {
                    **settings.model_dump(),
                    "environment": environment,
                    "import_allowed_networks": "127.0.0.0/8",
                }
            )


async def test_pages_from_two_storefronts_are_not_one_import(
    settings: Settings,
    session: AsyncSession,
    factory: async_sessionmaker[AsyncSession],
    issue_credential: CredentialIssuer,
) -> None:
    """An import is a merchant importing their own store, not a list of addresses to fetch."""
    shop = await merchant(session, "import-two-origins")
    token = await issue_credential(shop.id)
    async with MerchantFixtureServer(Storefront.voltedge().routes) as server:
        async for http in api(settings, factory):
            response = await http.post(
                IMPORTS,
                headers=bearer(token),
                json=import_command(
                    [page(server.url("/p/charger")), page("https://elsewhere.example/p")], FIRST
                ),
            )
    assert response.status_code == 422
    assert "same storefront origin" in response.json()["detail"]


async def test_the_same_url_twice_in_one_import_is_refused(
    settings: Settings,
    session: AsyncSession,
    factory: async_sessionmaker[AsyncSession],
    issue_credential: CredentialIssuer,
) -> None:
    shop = await merchant(session, "import-duplicate")
    token = await issue_credential(shop.id)
    async with MerchantFixtureServer(Storefront.voltedge().routes) as server:
        async for http in api(settings, factory):
            response = await http.post(
                IMPORTS,
                headers=bearer(token),
                json=import_command(
                    [page(server.url("/p/charger")), page(server.url("/p/charger"))], FIRST
                ),
            )
    assert response.status_code == 422


@pytest.mark.parametrize(
    "body",
    [
        {"request_key": FIRST, "pages": []},
        {"request_key": FIRST, "pages": [{"url": "https://s.example/p", "kind": "POLICY"}]},
        {
            "request_key": FIRST,
            "pages": [{"url": "https://s.example/p", "kind": "PRODUCT", "name": "returns"}],
        },
        {
            "request_key": FIRST,
            "pages": [
                {"url": "https://s.example/a", "kind": "POLICY", "name": "returns"},
                {"url": "https://s.example/b", "kind": "POLICY", "name": "returns"},
            ],
        },
        {"request_key": "short", "pages": [{"url": "https://s.example/p", "kind": "PRODUCT"}]},
        {
            "request_key": FIRST,
            "pages": [{"url": "https://s.example/p", "kind": "SOMETHING"}],
        },
        {
            "request_key": FIRST,
            "pages": [{"url": "https://s.example/p", "kind": "PRODUCT", "depth": 3}],
        },
        {
            "request_key": FIRST,
            "pages": [{"url": "https://s.example/p", "kind": "PRODUCT"}],
            "max_pages": 500,
        },
    ],
)
async def test_an_import_command_this_api_does_not_accept_is_refused(
    settings: Settings,
    session: AsyncSession,
    factory: async_sessionmaker[AsyncSession],
    issue_credential: CredentialIssuer,
    body: dict[str, Any],
) -> None:
    """Including the two that are about what a browser may not decide: a depth and a page budget."""
    shop = await merchant(session, f"import-bad-{abs(hash(str(body))) % 10_000}")
    token = await issue_credential(shop.id)
    async for http in api(settings, factory):
        response = await http.post(IMPORTS, headers=bearer(token), json=body)
    assert response.status_code == 422


async def test_more_pages_than_the_bound_are_refused_before_anything_is_fetched(
    settings: Settings,
    session: AsyncSession,
    factory: async_sessionmaker[AsyncSession],
    issue_credential: CredentialIssuer,
) -> None:
    shop = await merchant(session, "import-too-many")
    token = await issue_credential(shop.id)
    async with MerchantFixtureServer(Storefront.voltedge().routes) as server:
        pages = [page(server.url(f"/p/{index}")) for index in range(20)]
        async for http in api(settings, factory):
            response = await http.post(
                IMPORTS, headers=bearer(token), json=import_command(pages, FIRST)
            )
        assert server.requests == []
    assert response.status_code == 422


async def test_retrieved_evidence_cannot_be_rewritten_after_it_has_been_read(
    settings: Settings,
    session: AsyncSession,
    factory: async_sessionmaker[AsyncSession],
    issue_credential: CredentialIssuer,
) -> None:
    """The immutability is the database's, so no process anywhere can edit what an import found."""
    shop = await merchant(session, "import-immutable")
    token = await issue_credential(shop.id)
    async with MerchantFixtureServer(Storefront.voltedge().routes) as server:
        async for http in api(settings, factory):
            record = (
                await http.post(
                    IMPORTS,
                    headers=bearer(token),
                    json=import_command([page(server.url("/p/charger"))], FIRST),
                )
            ).json()
    import_id = uuid.UUID(record["summary"]["import_id"])
    with pytest.raises(DBAPIError, match="only the confirmation"):
        await session.execute(
            text("UPDATE merchant_source_import SET draft = '{}'::jsonb WHERE id = :id"),
            {"id": import_id},
        )
    await session.rollback()
    with pytest.raises(DBAPIError, match="is not deleted"):
        await session.execute(
            text("DELETE FROM merchant_source_import WHERE id = :id"), {"id": import_id}
        )
    await session.rollback()


async def test_an_imported_snapshot_builds_the_same_evaluation_setup_any_other_one_would(
    settings: Settings,
    session: AsyncSession,
    factory: async_sessionmaker[AsyncSession],
    issue_credential: CredentialIssuer,
) -> None:
    """The whole point of producing an ordinary snapshot: everything downstream is unchanged.

    No compiler run, no representation, no second bootstrap path, and no provider call. The
    workspace service is the Phase 5C one, reached with no argument that says an import produced
    the evidence.
    """
    shop = await merchant(session, "import-downstream")
    token = await issue_credential(shop.id)
    async with MerchantFixtureServer(Storefront.voltedge().routes) as server:
        async for http in api(settings, factory):
            record = (
                await http.post(
                    IMPORTS,
                    headers=bearer(token),
                    json=import_command(catalog_pages(server), FIRST),
                )
            ).json()
            confirmed = (
                await http.post(
                    f"{IMPORTS}/{record['summary']['import_id']}/confirm",
                    headers=bearer(token),
                    json={"stock_level": 6},
                )
            ).json()

    workspaces = MerchantEvaluationWorkspaceService(session)
    preflight = await workspaces.preflight(shop.id)
    assert preflight.current_source_snapshot_id == uuid.UUID(confirmed["source_snapshot_id"])
    assert preflight.blockers == ()
    assert preflight.planned is not None
    assert preflight.planned.mission_count > 0

    outcome = await workspaces.bootstrap(
        shop.id, source_snapshot_id=uuid.UUID(confirmed["source_snapshot_id"])
    )
    assert outcome.created is True
    ready = await workspaces.preflight(shop.id)
    assert ready.current is not None
    assert ready.current.mission_count > 0


async def test_no_part_of_an_import_needs_a_model_provider(
    settings: Settings,
    session: AsyncSession,
    factory: async_sessionmaker[AsyncSession],
    issue_credential: CredentialIssuer,
) -> None:
    """Stated as an assertion rather than as a comment, on a process that holds no credential."""
    blank = importing_settings(settings).model_copy(
        update={"openai_api_key": None, "gemini_api_key": None}
    )
    assert blank.openai is None
    assert blank.gemini is None
    shop = await merchant(session, "import-provider-free")
    token = await issue_credential(shop.id)
    app = create_app(blank, payment_provider=FakePaymentProvider())
    app.state.session_factory = factory
    async with MerchantFixtureServer(Storefront.voltedge().routes) as server:
        async with httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app), base_url="http://api.test"
        ) as http:
            record = (
                await http.post(
                    IMPORTS,
                    headers=bearer(token),
                    json=import_command(catalog_pages(server), FIRST),
                )
            ).json()
            confirmed = await http.post(
                f"{IMPORTS}/{record['summary']['import_id']}/confirm",
                headers=bearer(token),
                json={"stock_level": 5},
            )
        # The storefront is the only host anything reached, and only for the named pages.
        assert {target for target, _ in server.requests} == {
            "/p/charger",
            "/p/cable",
            "/p/dock",
            "/p/sleeve",
            "/returns",
            "/warranty",
        }
    assert confirmed.status_code == 201


async def test_an_import_that_finds_no_product_states_that_rather_than_creating_a_source(
    settings: Settings,
    session: AsyncSession,
    factory: async_sessionmaker[AsyncSession],
    issue_credential: CredentialIssuer,
) -> None:
    shop = await merchant(session, "import-empty")
    token = await issue_credential(shop.id)
    async with MerchantFixtureServer(Storefront.voltedge().routes) as server:
        async for http in api(settings, factory):
            record = (
                await http.post(
                    IMPORTS,
                    headers=bearer(token),
                    json=import_command([page(server.url("/p/mystery"))], FIRST),
                )
            ).json()
            refused = await http.post(
                f"{IMPORTS}/{record['summary']['import_id']}/confirm",
                headers=bearer(token),
                json={"stock_level": 1},
            )
    assert record["confirmable"] is False
    assert [blocker["code"] for blocker in record["blockers"]] == ["no_products"]
    assert refused.status_code == 409
    assert refused.json()["error"] == "no_products"


async def test_an_import_history_row_carries_counts_and_never_the_draft(
    settings: Settings,
    session: AsyncSession,
    factory: async_sessionmaker[AsyncSession],
    issue_credential: CredentialIssuer,
) -> None:
    shop = await merchant(session, "import-history")
    token = await issue_credential(shop.id)
    async with MerchantFixtureServer(Storefront.voltedge().routes) as server:
        async for http in api(settings, factory):
            await http.post(
                IMPORTS,
                headers=bearer(token),
                json=import_command([page(server.url("/p/charger"))], FIRST),
            )
            await http.post(
                IMPORTS,
                headers=bearer(token),
                json=import_command([page(server.url("/p/cable"))], SECOND),
            )
            listed = (await http.get(IMPORTS, headers=bearer(token))).json()

    assert len(listed) == 2
    assert all("products" not in entry for entry in listed)
    assert all(entry["product_count"] == 1 for entry in listed)


async def test_an_import_that_runs_out_of_bytes_stops_and_says_which_pages_it_did_not_read(
    settings: Settings,
    session: AsyncSession,
    factory: async_sessionmaker[AsyncSession],
    issue_credential: CredentialIssuer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The whole-import byte budget, which the per page bound does not cover.

    Twelve pages each just inside the per page bound is twenty four megabytes of markup to parse
    inside one request. The budget is spent in the order the merchant listed their URLs, which is
    why pages are fetched one at a time: two imports of one list stop at the same page.
    """
    monkeypatch.setattr(service_module, "MAX_IMPORT_TOTAL_BYTES", 1)
    shop = await merchant(session, "import-byte-budget")
    token = await issue_credential(shop.id)
    async with MerchantFixtureServer(Storefront.voltedge().routes) as server:
        async for http in api(settings, factory):
            body = (
                await http.post(
                    IMPORTS,
                    headers=bearer(token),
                    json=import_command(
                        [page(server.url("/p/charger")), page(server.url("/p/cable"))], FIRST
                    ),
                )
            ).json()
        # The first page was read and the second was never requested.
        assert [target for target, _ in server.requests] == ["/p/charger"]

    assert body["summary"]["product_count"] == 1
    assert body["pages"][1]["reason"] == "import_byte_budget"
    assert body["pages"][1]["retrieved"] is False


async def test_an_import_that_runs_out_of_time_is_a_failure_rather_than_a_partial_draft(
    settings: Settings,
    session: AsyncSession,
    factory: async_sessionmaker[AsyncSession],
    issue_credential: CredentialIssuer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A deadline produces no draft at all, because half a catalog is not a catalog.

    A page level failure is a fact to report beside the pages that worked. Running out of time is
    not: which pages were reached would then depend on how slow somebody's server was that
    minute, and confirming the result would write a source snapshot missing products for a reason
    the merchant could not see.
    """
    monkeypatch.setattr(service_module, "IMPORT_DEADLINE_SECONDS", 0.4)
    shop = await merchant(session, "import-deadline")
    token = await issue_credential(shop.id)
    routes = dict(Storefront.voltedge().routes)
    routes["/p/slow"] = CannedResponse(body=b"<html></html>", delay_seconds=5.0)
    async with MerchantFixtureServer(routes) as server:
        async for http in api(settings, factory):
            body = (
                await http.post(
                    IMPORTS,
                    headers=bearer(token),
                    json=import_command(
                        [page(server.url("/p/slow")), page(server.url("/p/charger"))], FIRST
                    ),
                )
            ).json()

    assert body["summary"]["state"] == "FAILED"
    assert body["summary"]["failure_reason"] == "deadline"
    assert body["summary"]["product_count"] == 0
    assert [entry["reason"] for entry in body["pages"]] == ["import_deadline", "import_deadline"]
    assert [blocker["code"] for blocker in body["blockers"]] == ["import_failed"]


async def test_a_failed_import_cannot_be_confirmed_into_anything(
    settings: Settings,
    session: AsyncSession,
    factory: async_sessionmaker[AsyncSession],
    issue_credential: CredentialIssuer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(service_module, "IMPORT_DEADLINE_SECONDS", 0.4)
    shop = await merchant(session, "import-failed-confirm")
    token = await issue_credential(shop.id)
    routes = dict(Storefront.voltedge().routes)
    routes["/p/slow"] = CannedResponse(body=b"<html></html>", delay_seconds=5.0)
    async with MerchantFixtureServer(routes) as server:
        async for http in api(settings, factory):
            record = (
                await http.post(
                    IMPORTS,
                    headers=bearer(token),
                    json=import_command([page(server.url("/p/slow"))], FIRST),
                )
            ).json()
            refused = await http.post(
                f"{IMPORTS}/{record['summary']['import_id']}/confirm",
                headers=bearer(token),
                json={"stock_level": 1},
            )
    assert refused.status_code == 409
    assert refused.json()["error"] == "import_failed"
    assert await MerchantSourceIntakeService(session).current(shop.id) is None


async def test_two_pages_publishing_one_product_import_it_once_and_say_so(
    settings: Settings,
    session: AsyncSession,
    factory: async_sessionmaker[AsyncSession],
    issue_credential: CredentialIssuer,
) -> None:
    """A canonical URL and a variant URL for one product is an everyday storefront shape.

    Importing both would produce a document that passes the request schema and is then refused by
    the source domain, deeper down, over a uniqueness rule the merchant never saw. The second page
    is refused here instead, naming the page and the identity.
    """
    shop = await merchant(session, "import-duplicate-identity")
    token = await issue_credential(shop.id)
    routes = dict(Storefront.voltedge().routes)
    routes["/p/charger-copy"] = routes["/p/charger"]
    async with MerchantFixtureServer(routes) as server:
        async for http in api(settings, factory):
            body = (
                await http.post(
                    IMPORTS,
                    headers=bearer(token),
                    json=import_command(
                        [page(server.url("/p/charger")), page(server.url("/p/charger-copy"))],
                        FIRST,
                    ),
                )
            ).json()

    assert body["summary"]["product_count"] == 1
    assert [note["code"] for note in body["omissions"]] == ["duplicate_product"]
    assert body["omissions"][0]["source_url"].endswith("/p/charger-copy")


async def test_one_import_key_naming_different_pages_is_a_different_command(
    settings: Settings,
    session: AsyncSession,
    factory: async_sessionmaker[AsyncSession],
    issue_credential: CredentialIssuer,
) -> None:
    """Answering it with the first import's result would say an import of B read the pages of A."""
    shop = await merchant(session, "import-key-reused")
    token = await issue_credential(shop.id)
    async with MerchantFixtureServer(Storefront.voltedge().routes) as server:
        async for http in api(settings, factory):
            first = await http.post(
                IMPORTS,
                headers=bearer(token),
                json=import_command([page(server.url("/p/charger"))], FIRST),
            )
            second = await http.post(
                IMPORTS,
                headers=bearer(token),
                json=import_command([page(server.url("/p/cable"))], FIRST),
            )
    assert first.status_code == 201
    assert second.status_code == 409
    assert second.json()["error"] == "import_request_key_reused"


async def test_two_simultaneous_confirmations_produce_one_snapshot_and_no_error(
    settings: Settings,
    session: AsyncSession,
    factory: async_sessionmaker[AsyncSession],
    issue_credential: CredentialIssuer,
) -> None:
    """The merchant double clicked. Both requests succeed and one snapshot exists.

    The loser used to write over a row the immutability trigger then refused, which reached the
    merchant as "the database is unavailable" on a command that had in fact succeeded.
    """
    shop = await merchant(session, "import-confirm-race")
    token = await issue_credential(shop.id)
    async with MerchantFixtureServer(Storefront.voltedge().routes) as server:
        async for http in api(settings, factory):
            record = (
                await http.post(
                    IMPORTS,
                    headers=bearer(token),
                    json=import_command([page(server.url("/p/charger"))], FIRST),
                )
            ).json()
            target = f"{IMPORTS}/{record['summary']['import_id']}/confirm"
            both = await asyncio.gather(
                http.post(target, headers=bearer(token), json={"stock_level": 4}),
                http.post(target, headers=bearer(token), json={"stock_level": 4}),
            )

    assert [response.status_code for response in both] == [201, 201]
    assert len({response.json()["source_snapshot_id"] for response in both}) == 1
    snapshots = (
        (
            await session.execute(
                select(MerchantSourceSubmission).where(
                    MerchantSourceSubmission.merchant_id == shop.id
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(snapshots) == 1
    linked = (await session.execute(select(MerchantSourceImport))).scalars().one()
    assert linked.source_snapshot_id is not None
    assert linked.stock_level == 4


async def test_a_retry_after_a_lost_link_answers_with_the_snapshot_that_already_exists(
    settings: Settings,
    session: AsyncSession,
    factory: async_sessionmaker[AsyncSession],
    issue_credential: CredentialIssuer,
) -> None:
    """The crash between the two writes, including a retry that states a different stock level.

    The snapshot is committed by the intake and the import row is linked afterwards, so a process
    that died between them leaves a snapshot with no link. The state is reproduced by submitting
    under the key the import derives, which is exactly what the first half of a confirmation does
    and all it managed to do. The retry finds that submission, repairs the link and says the import
    was already confirmed, rather than refusing forever because the second attempt named a
    different number.
    """
    shop = await merchant(session, "import-lost-link")
    token = await issue_credential(shop.id)
    async with MerchantFixtureServer(Storefront.voltedge().routes) as server:
        async for http in api(settings, factory):
            record = (
                await http.post(
                    IMPORTS,
                    headers=bearer(token),
                    json=import_command([page(server.url("/p/charger"))], FIRST),
                )
            ).json()
            import_id = uuid.UUID(record["summary"]["import_id"])

            service = service_module.MerchantSourceImportService(session)
            stored = await service.read(shop.id, import_id)
            document = SourceDocumentInput.model_validate(
                canonical_document(SourceDraft.of(stored.draft), stock_level=3)
            )
            orphaned = await MerchantSourceIntakeService(session).submit(
                shop.id,
                request_key=service_module.submission_key(import_id),
                document=document,
                origin=SourceOrigin.MERCHANT_IMPORT,
            )
            assert (await service.read(shop.id, import_id)).confirmed_at is None
            snapshot_id = orphaned.snapshot.id

            retried = await http.post(
                f"{IMPORTS}/{import_id}/confirm",
                headers=bearer(token),
                json={"stock_level": 9},
            )

    assert retried.status_code == 201
    assert retried.json()["already_confirmed"] is True
    assert retried.json()["source_snapshot_id"] == str(snapshot_id)
    # The link was repaired by the API's own session, so this one has to be told to look again.
    session.expire_all()
    repaired = (await session.execute(select(MerchantSourceImport))).scalars().one()
    assert repaired.source_snapshot_id == snapshot_id
    # No stock level, because this attempt named 9 and the snapshot was built from 3. Recording it
    # would have the row state a figure the snapshot does not carry.
    assert repaired.stock_level is None


async def test_a_page_that_cannot_become_a_source_document_is_refused_rather_than_raised(
    settings: Settings,
    session: AsyncSession,
    factory: async_sessionmaker[AsyncSession],
    issue_credential: CredentialIssuer,
) -> None:
    """A URL that grows past what a source document holds once it has been normalized.

    Percent encoding a path expands it, so a URL inside the submitted bound can leave it, and that
    string is stored in a source document's merchant metadata. It used to pass every blocker and
    then raise out of the confirm route as a 500 the merchant could do nothing about.
    """
    shop = await merchant(session, "import-long-url")
    token = await issue_credential(shop.id)
    async for http in public_api(settings, factory):
        response = await http.post(
            IMPORTS,
            headers=bearer(token),
            json=import_command([page("https://shop.example/p/" + "\u00fc" * 370)], FIRST),
        )
    assert response.status_code == 422
    assert "normalized" in response.json()["detail"]
