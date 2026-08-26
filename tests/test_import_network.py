"""The merchant import network boundary, tested as a mechanism rather than as a policy.

This is the one place in AgentRank that connects to an address a request body chose. Every test
here is about something that would be a server side request forgery if it were missing, and each
one exercises the real function a request goes through rather than a regular expression over a
string.

Three properties get the most scrutiny, because they are the three that are usually absent:

```text
all addresses     a name resolving partly inside is refused, not partly used
the connection    the socket goes to the address that was checked, not to the name again
every hop         a redirect is a new target and goes through every check from the start
```

Nothing here reaches the internet. The permitted-target cases are served by a real HTTP fixture on
loopback, reachable only because those tests construct a policy that permits loopback; the refusal
cases use the default policy, which does not.
"""

import asyncio
import ipaddress
import socket
from collections.abc import Iterator
from typing import Any

import pytest
from importer_support import (
    JSON_LD_PRODUCT,
    CannedResponse,
    MerchantFixtureServer,
    html,
    redirect,
)

from agentrank_api.importer.network import (
    PUBLIC_ONLY,
    AddressPolicy,
    FetchLimits,
    MerchantPageFetcher,
    PermittedNetworks,
    RefusedTargetError,
    RetrievalFailure,
    RetrievalLogLine,
    RetrievedDocument,
    resolve_addresses,
    validate_target,
)

pytestmark = pytest.mark.anyio

LOOPBACK = PermittedNetworks.parse("127.0.0.0/8,::1/128").policy()


@pytest.fixture
def resolves(monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, list[str]]]:
    """Point chosen host names at chosen addresses, without touching a real resolver.

    Patched at `socket.getaddrinfo`, which is what the event loop's own resolver calls, so the
    code under test takes exactly the path it takes in production and only the answer changes.
    """
    table: dict[str, list[str]] = {}
    original = socket.getaddrinfo

    def fake(host: Any, port: Any, *args: Any, **kwargs: Any) -> list[Any]:
        answers = table.get(str(host))
        if answers is None:
            return original(host, port, *args, **kwargs)
        if not answers:
            raise socket.gaierror("no answer")
        found = []
        for literal in answers:
            address = ipaddress.ip_address(literal)
            family = socket.AF_INET6 if address.version == 6 else socket.AF_INET
            endpoint = (
                (literal, int(port or 0), 0, 0)
                if address.version == 6
                else (literal, int(port or 0))
            )
            found.append((family, socket.SOCK_STREAM, 6, "", endpoint))
        return found

    monkeypatch.setattr(socket, "getaddrinfo", fake)
    yield table


FORBIDDEN_LITERALS = [
    "http://127.0.0.1/",
    "http://127.0.0.2/",
    "https://[::1]/",
    "http://10.0.0.1/",
    "http://192.168.1.1/",
    "http://172.16.0.1/",
    "http://169.254.169.254/latest/meta-data/",
    "http://[fd00:ec2::254]/",
    "http://[fe80::1]/",
    "http://0.0.0.0/",
    "http://100.100.100.200/",
    "http://[::ffff:127.0.0.1]/",
    "http://224.0.0.1/",
    "http://255.255.255.255/",
]


@pytest.mark.parametrize("url", FORBIDDEN_LITERALS)
async def test_an_address_literal_outside_the_public_internet_is_refused(url: str) -> None:
    """Every shape of internal address, including the ones that are not obviously internal.

    The metadata address and the multicast address are the two worth naming. The first is the
    canonical server side request forgery target and is only excluded because link-local is;
    the second reports itself as globally scoped and is excluded by a clause that exists for it.
    """
    with pytest.raises(RefusedTargetError) as refused:
        validate_target(url)
    assert refused.value.reason == "address_not_permitted"


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "ftp://example.com/x",
        "gopher://example.com/x",
        "data:text/html,<b>x</b>",
        "javascript:alert(1)",
        "http+unix://%2Fvar%2Frun%2Fdocker.sock/info",
    ],
)
async def test_a_scheme_other_than_http_is_refused(url: str) -> None:
    """A URL that is not an ordinary web page request, including a Unix socket spelling."""
    with pytest.raises(RefusedTargetError) as refused:
        validate_target(url)
    assert refused.value.reason in {"scheme_not_permitted", "url_malformed"}


@pytest.mark.parametrize(
    "url",
    [
        "https://user:password@shop.example/p",
        "https://user@shop.example/p",
        "https://:secret@shop.example/p",
    ],
)
async def test_a_credential_in_a_url_is_refused_rather_than_stripped(url: str) -> None:
    """Stripping one would fetch something the caller did not ask for and store a secret."""
    with pytest.raises(RefusedTargetError) as refused:
        validate_target(url)
    assert refused.value.reason == "credential_in_url"


@pytest.mark.parametrize("url", ["https://shop.example:8080/p", "http://shop.example:22/p"])
async def test_a_port_a_storefront_does_not_answer_on_is_refused(url: str) -> None:
    """An unrestricted port turns one permitted fetch into a port scanner."""
    with pytest.raises(RefusedTargetError) as refused:
        validate_target(url)
    assert refused.value.reason == "port_not_permitted"


@pytest.mark.parametrize(
    "url",
    [
        "http://xn--p1ai..example/",
        "http://xn--p1ai." + "a" * 250 + ".example/",
        "https://xn--fiqs8s.icom.museum/p",
    ],
)
async def test_an_internationalized_host_is_read_without_escaping(url: str) -> None:
    """Every one of these used to raise out of the boundary as an unhandled server error.

    `httpx2.URL.host` is a lazy property that IDNA decodes on access: it raises for a malformed
    punycode label and returns Unicode for a valid one, and the Unicode then fails to encode as a
    `Host` header. Both are reachable from a request body and from a redirect a merchant page
    chose, so both were a way for a page to 500 the endpoint. The ASCII form goes on the wire and
    is what is stored.
    """
    target = validate_target(url)
    assert target.host.isascii()
    assert target.host == target.host.lower()


async def test_a_host_is_never_replaced_by_a_different_registrable_domain() -> None:
    """Two hosts IDNA 2003 folds together are separate registrations, and stay separate here.

    Converting the host here with the standard library codec did exactly that, so a merchant
    asking for one would have had the other fetched and the import record would have said so. The
    host now comes from the parser's own wire form, which is the one the connection uses.
    """
    assert validate_target("http://xn--fa-hia.de/").host == "xn--fa-hia.de"
    assert validate_target("https://SHOP.Example./p").host == "shop.example"


async def test_a_url_longer_than_the_bound_is_refused_before_it_is_parsed() -> None:
    with pytest.raises(RefusedTargetError) as refused:
        validate_target("https://shop.example/" + "a" * 4000)
    assert refused.value.reason == "url_too_long"


async def test_a_trailing_root_label_is_the_same_host(resolves: dict[str, list[str]]) -> None:
    """`shop.example.` and `shop.example` are one name, so they cannot be two origins."""
    assert validate_target("https://shop.example./p").host == "shop.example"
    assert validate_target("https://SHOP.Example/p").origin == "https://shop.example:443"


async def test_a_host_that_resolves_to_a_forbidden_address_is_refused(
    resolves: dict[str, list[str]],
) -> None:
    """The name says nothing. What it resolves to decides, and it is checked before connecting."""
    resolves["storefront.example"] = ["127.0.0.1"]
    with pytest.raises(RefusedTargetError) as refused:
        await resolve_addresses(validate_target("https://storefront.example/p"))
    assert refused.value.reason == "address_not_permitted"


async def test_a_host_that_resolves_both_inside_and_outside_is_refused_entirely(
    resolves: dict[str, list[str]],
) -> None:
    """The rebinding shape. Using the permitted answer would make which one is reached a race."""
    resolves["storefront.example"] = ["93.184.216.34", "127.0.0.1"]
    with pytest.raises(RefusedTargetError) as refused:
        await resolve_addresses(validate_target("https://storefront.example/p"))
    assert refused.value.reason == "address_not_permitted"


async def test_a_host_that_resolves_to_nothing_is_refused(
    resolves: dict[str, list[str]],
) -> None:
    resolves["storefront.example"] = []
    with pytest.raises(RefusedTargetError) as refused:
        await resolve_addresses(validate_target("https://storefront.example/p"))
    assert refused.value.reason == "host_not_resolvable"


async def test_the_connection_goes_to_the_validated_address_and_names_the_host(
    resolves: dict[str, list[str]],
) -> None:
    """The pinning property, asserted from the server's side.

    A name is resolved once, checked, and connected to as an address literal. The merchant's
    server still sees its own name in `Host`, which is both what makes virtual hosting work and
    what proves the request was not simply rewritten to an address.
    """
    async with MerchantFixtureServer({"/p": html(JSON_LD_PRODUCT)}) as server:
        resolves["storefront.example"] = ["127.0.0.1"]
        target = validate_target(f"http://storefront.example:{server.port}/p", policy=LOOPBACK)
        async with MerchantPageFetcher(
            policy=LOOPBACK, limits=FetchLimits(max_response_bytes=1 << 20)
        ) as fetcher:
            outcome = await fetcher.fetch(target)
    assert isinstance(outcome, RetrievedDocument)
    assert server.header("host") == f"storefront.example:{server.port}"


async def test_a_redirect_into_forbidden_address_space_is_refused() -> None:
    """The shortest server side request forgery there is: a public URL that redirects inward."""
    async with MerchantFixtureServer(
        {"/p": redirect("http://169.254.169.254/latest/meta-data/")}
    ) as server:
        outcome = await _fetch_from(server, "/p")
    assert isinstance(outcome, RetrievalFailure)
    assert outcome.reason == "address_not_permitted"


async def test_a_redirect_to_another_scheme_is_refused() -> None:
    async with MerchantFixtureServer({"/p": redirect("file:///etc/passwd")}) as server:
        outcome = await _fetch_from(server, "/p")
    assert isinstance(outcome, RetrievalFailure)
    assert outcome.reason == "scheme_not_permitted"


async def test_a_redirect_carrying_a_credential_is_refused() -> None:
    async with MerchantFixtureServer(
        {"/p": redirect("https://user:secret@elsewhere.example/x")}
    ) as server:
        outcome = await _fetch_from(server, "/p")
    assert isinstance(outcome, RetrievalFailure)
    assert outcome.reason == "credential_in_url"


async def test_a_relative_redirect_is_followed_within_the_bound() -> None:
    """Storefronts redirect legitimately, and a relative target resolves against the request."""
    async with MerchantFixtureServer(
        {"/p": redirect("/final"), "/final": html(JSON_LD_PRODUCT)}
    ) as server:
        outcome = await _fetch_from(server, "/p")
    assert isinstance(outcome, RetrievedDocument)
    assert outcome.redirects and outcome.redirects[0].endswith("/final")
    assert outcome.final_url.endswith("/final")


async def test_more_redirects_than_the_bound_are_refused() -> None:
    routes: dict[str, Any] = {f"/hop{index}": redirect(f"/hop{index + 1}") for index in range(8)}
    async with MerchantFixtureServer(routes) as server:
        outcome = await _fetch_from(server, "/hop0", limits=FetchLimits(max_redirects=3))
    assert isinstance(outcome, RetrievalFailure)
    assert outcome.reason == "too_many_redirects"


async def test_a_redirect_to_a_host_that_cannot_be_read_is_a_refusal_rather_than_an_error() -> None:
    """A merchant page choosing a `Location` must never be able to escape the boundary.

    A malformed punycode label in a redirect target used to raise out of `fetch` and discard the
    entire in-flight import, twelve pages and all, with nothing written and nothing the merchant
    could see.
    """
    async with MerchantFixtureServer({"/p": redirect("http://xn--p1ai..example/")}) as server:
        outcome = await _fetch_from(server, "/p")
    assert isinstance(outcome, RetrievalFailure)
    assert outcome.reason == "redirect_malformed"


async def test_a_redirect_to_nowhere_is_refused() -> None:
    async with MerchantFixtureServer(
        {"/p": CannedResponse(status=302, content_type=None)}
    ) as server:
        outcome = await _fetch_from(server, "/p")
    assert isinstance(outcome, RetrievalFailure)
    assert outcome.reason == "redirect_malformed"


async def test_a_body_larger_than_the_bound_is_refused_while_it_arrives() -> None:
    """A response that declares no length at all is bounded by the running count and nothing else.

    This is the case the declared-length check cannot cover. A response framed by the connection
    closing, which is ordinary HTTP/1.1, has no length to check, so the only thing standing between
    a merchant page and an unbounded read is counting the bytes as they arrive.
    """
    async with MerchantFixtureServer(
        {"/p": CannedResponse(body=b"a" * 200_000, omit_length=True)}
    ) as server:
        outcome = await _fetch_from(server, "/p", limits=FetchLimits(max_response_bytes=1024))
    assert isinstance(outcome, RetrievalFailure)
    assert outcome.reason == "response_too_large"


async def test_a_declared_length_over_the_bound_is_refused_before_the_body_is_read() -> None:
    async with MerchantFixtureServer(
        {"/p": CannedResponse(body=b"a" * 4096, declared_length=99_000_000)}
    ) as server:
        outcome = await _fetch_from(server, "/p", limits=FetchLimits(max_response_bytes=1024))
    assert isinstance(outcome, RetrievalFailure)
    assert outcome.reason == "response_too_large"


async def test_a_compressed_body_that_expands_past_the_bound_is_refused() -> None:
    """The bound is over decoded bytes, which is the only place it is a bound at all.

    Ten megabytes of one repeated character compresses to a few kilobytes. Every measurement taken
    before decompression says this is a small response.
    """
    async with MerchantFixtureServer(
        {"/p": CannedResponse(body=b"a" * (10 * 1024 * 1024), gzip_body=True)}
    ) as server:
        assert len(server._routes["/p"].wire()) < 64 * 1024  # type: ignore[union-attr]
        outcome = await _fetch_from(server, "/p", limits=FetchLimits(max_response_bytes=1 << 20))
    assert isinstance(outcome, RetrievalFailure)
    assert outcome.reason == "response_too_large"


@pytest.mark.parametrize(
    "content_type",
    ["application/json", "application/pdf", "image/png", "text/plain", None, "not/a type; ="],
)
async def test_a_response_that_is_not_an_html_page_is_refused(content_type: str | None) -> None:
    async with MerchantFixtureServer(
        {"/p": CannedResponse(content_type=content_type, body=b"{}")}
    ) as server:
        outcome = await _fetch_from(server, "/p")
    assert isinstance(outcome, RetrievalFailure)
    assert outcome.reason == "content_type_not_permitted"


async def test_a_server_that_does_not_answer_in_time_is_reported_rather_than_waited_on() -> None:
    async with MerchantFixtureServer({"/p": CannedResponse(delay_seconds=5.0)}) as server:
        outcome = await _fetch_from(server, "/p", limits=FetchLimits(page_timeout_seconds=0.4))
    assert isinstance(outcome, RetrievalFailure)
    assert outcome.reason == "timeout"


@pytest.mark.parametrize("status", [401, 403, 404, 429, 500, 503])
async def test_a_refusal_from_the_merchant_is_respected_rather_than_worked_around(
    status: int,
) -> None:
    """AgentRank does not retry past an access control, a rate limit or an error."""
    async with MerchantFixtureServer(
        {"/p": CannedResponse(status=status, body=b"<html></html>")}
    ) as server:
        outcome = await _fetch_from(server, "/p")
    assert isinstance(outcome, RetrievalFailure)
    assert outcome.reason == "http_error"
    assert outcome.status_code == status


async def test_a_connection_nothing_is_listening_on_is_one_named_failure(
    unused_port: int,
) -> None:
    target = validate_target(f"http://127.0.0.1:{unused_port}/p", policy=LOOPBACK)
    async with MerchantPageFetcher(policy=LOOPBACK) as fetcher:
        outcome = await fetcher.fetch(target)
    assert isinstance(outcome, RetrievalFailure)
    assert outcome.reason == "unreachable"


async def test_the_retrieved_document_identity_is_the_bytes_that_arrived() -> None:
    """Two retrievals of one unchanged page have one identity, whatever the transport did.

    The same body served compressed and uncompressed is the same document, because the digest is
    taken after content decoding and before anything interprets it.
    """
    body = JSON_LD_PRODUCT.encode("utf-8")
    async with MerchantFixtureServer(
        {"/plain": CannedResponse(body=body), "/gzip": CannedResponse(body=body, gzip_body=True)}
    ) as server:
        plain = await _fetch_from(server, "/plain")
        packed = await _fetch_from(server, "/gzip")
    assert isinstance(plain, RetrievedDocument)
    assert isinstance(packed, RetrievedDocument)
    assert plain.content_hash == packed.content_hash
    assert plain.content_hash.startswith("sha256:")


async def test_a_log_line_carries_an_origin_and_a_bounded_path_and_no_query() -> None:
    """A merchant's URL carries their business in its query, and a log is not the place for it."""
    target = validate_target("https://shop.example/orders/secret?token=abc123&email=a@b.example")
    line = RetrievalLogLine.of(target, status_code=200, byte_count=42)
    rendered = str(line)
    assert "token" not in rendered
    assert "abc123" not in rendered
    assert "a@b.example" not in rendered
    assert rendered.startswith("https://shop.example:443/orders/secret")


async def test_the_default_policy_permits_a_public_address_and_nothing_else() -> None:
    assert PUBLIC_ONLY.permits(ipaddress.ip_address("93.184.216.34"))
    assert PUBLIC_ONLY.permits(ipaddress.ip_address("2606:4700::1111"))
    assert not PUBLIC_ONLY.permits(ipaddress.ip_address("127.0.0.1"))
    assert not AddressPolicy().permits(ipaddress.ip_address("10.0.0.1"))


async def test_a_permitted_network_widens_the_policy_only_where_it_says() -> None:
    policy = PermittedNetworks.parse("127.0.0.0/8").policy()
    assert policy.permits(ipaddress.ip_address("127.0.0.1"))
    assert not policy.permits(ipaddress.ip_address("10.0.0.1"))
    assert not policy.permits(ipaddress.ip_address("169.254.169.254"))


async def test_the_public_policy_restricts_ports_and_a_widened_one_does_not() -> None:
    """A port restriction is what stops a permitted fetch becoming a port scanner.

    It is lifted only for a policy that has already been widened to a private network, which is a
    development or test process reaching a fixture and which `Settings` refuses to construct in a
    deployment at all.
    """
    assert PUBLIC_ONLY.permits_port(443)
    assert not PUBLIC_ONLY.permits_port(8080)
    assert PermittedNetworks.parse("127.0.0.0/8").policy().permits_port(8080)
    assert not PermittedNetworks.parse("").policy().permits_port(8080)


async def test_a_policy_cannot_lift_the_port_rule_without_widening_an_address() -> None:
    with pytest.raises(ValueError, match="any port"):
        AddressPolicy(any_port=True)


async def test_a_permitted_network_list_that_is_not_cidr_is_refused() -> None:
    with pytest.raises(ValueError, match="CIDR"):
        PermittedNetworks.parse("not-a-network")


async def _fetch_from(
    server: MerchantFixtureServer, path: str, *, limits: FetchLimits | None = None
) -> RetrievedDocument | RetrievalFailure:
    """Fetch one fixture path through the whole real boundary, on a policy that permits loopback."""
    target = validate_target(server.url(path), policy=LOOPBACK)
    async with MerchantPageFetcher(policy=LOOPBACK, limits=limits or FetchLimits()) as fetcher:
        return await fetcher.fetch(target)


async def test_asyncio_timeout_is_the_bound_on_a_whole_hop_chain() -> None:
    """A chain of redirects that each answer just inside the per hop bound still ends."""
    routes: dict[str, Any] = {
        f"/slow{index}": CannedResponse(
            status=302,
            content_type=None,
            headers=(("Location", f"/slow{index + 1}"),),
            delay_seconds=0.3,
        )
        for index in range(4)
    }
    async with MerchantFixtureServer(routes) as server:
        started = asyncio.get_running_loop().time()
        outcome = await _fetch_from(
            server, "/slow0", limits=FetchLimits(page_timeout_seconds=0.5, max_redirects=8)
        )
        elapsed = asyncio.get_running_loop().time() - started
    assert isinstance(outcome, RetrievalFailure)
    assert outcome.reason == "timeout"
    assert elapsed < 2.0
