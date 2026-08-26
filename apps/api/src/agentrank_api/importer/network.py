"""The only place this application fetches a URL a merchant chose.

Everything else AgentRank talks to is an address this repository decided: a payment provider, a
model provider, its own loopback commerce endpoint. This module is different in kind. The address
comes from a request body, so the request body can aim it, and a server that fetches an attacker
chosen address is the server side request forgery boundary. It is written as one narrow mechanism
rather than spread across a service so that there is exactly one answer to "what may AgentRank
connect to", and so that answer can be tested without a service, a session or a merchant.

Four separate decisions, in order, and none of them trusts the one before it:

```text
the URL         a shape this repository will consider at all
the addresses   every address the name resolves to, all of them, before any connection
the connection  made to a validated address literal and never to the name again
the response    bounded in bytes, in type and in time, and read as data
```

The third is the half that is usually missing. Validating a hostname and then handing the
hostname to an HTTP client leaves the client to resolve it a second time, and a name that answered
with a public address for the check can answer with 127.0.0.1 for the connection. That is DNS
rebinding, and no amount of care about the first two decisions prevents it. So the connection is
made to the address that was validated: the request URL carries the address literal, the `Host`
header carries the original name so the merchant's server routes it correctly, and
`sni_hostname` carries the original name so TLS is negotiated and the certificate verified
against the name rather than against the number. There is no second resolution to poison.

Redirects are followed here rather than by the client, for the same reason. A client following
redirects applies none of this to hop two, and a public URL that answers `302 Location:
http://169.254.169.254/` is the shortest server side request forgery in existence. Every hop is a
new target that goes through all four decisions from the beginning.

What this module will not do is as much of its definition as what it will. It sends `GET`, it
sends no body, it presents no credential, it stores no cookie, it runs no script, and it never
looks at a response to decide what to fetch next. A merchant page is data. The only thing that
decides what is fetched is the list of URLs the merchant supplied.

Refusals are stated as reason codes rather than prose, because a refusal is something a console
has to render as an actionable sentence beside the URL that caused it, and prose from a network
stack is not that. Nothing an upstream said reaches one, and nothing but the origin and a bounded
path reaches a log.
"""

import asyncio
import hashlib
import ipaddress
import socket
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from types import TracebackType
from typing import Self

import httpx2

# What a merchant may aim this at. HTTPS is the shape a storefront actually has; plain HTTP is
# permitted because a small storefront that serves its policy pages over HTTP is an ordinary
# thing rather than an attack, because every hop is validated whether or not it is encrypted, and
# because the content being fetched is public by definition. It buys no access that HTTPS does
# not: the address policy below is the boundary, and it does not consult the scheme.
PERMITTED_SCHEMES = frozenset({"http", "https"})

# The ports a public storefront answers on. Restricting these is not about the HTTP protocol; it
# is about what a permitted URL can be used to learn. An unrestricted port turns one fetch into a
# port scanner aimed at whatever the address policy does permit, and the difference between a
# refused connection, a timeout and a 200 is enough to map a host.
PERMITTED_PORTS = frozenset({80, 443})

DEFAULT_PORTS = {"http": 80, "https": 443}

# A URL long enough for any storefront and short enough to store beside every page of an import.
MAX_URL_LENGTH = 2048

# What this application will read from one page. Generous for a storefront product page, which is
# tens of kilobytes of markup, and far below anything that would make parsing it expensive.
#
# Counted over decoded bytes rather than over what arrived on the wire, which is what makes it a
# bound at all. A gzip stream of one megabyte expanding to a gigabyte is a small response by every
# measure taken before decompression, and the parser downstream would see the gigabyte.
MAX_RESPONSE_BYTES = 2 * 1024 * 1024

# Redirect hops. Storefronts redirect: http to https, a bare host to www, a legacy product path to
# a current one. Three is room for all of that at once and is nowhere near a loop.
MAX_REDIRECTS = 3

# What a page is allowed to take. The connect bound is separate because a host that is not
# answering at all should be reported quickly rather than occupying the whole page budget, and the
# total is enforced around the entire hop chain so that redirects cannot multiply it.
CONNECT_TIMEOUT_SECONDS = 5.0
PAGE_TIMEOUT_SECONDS = 15.0

# What a merchant page is. A storefront that answers a product URL with a PDF, an image or a JSON
# API document is not refused because those are suspicious; it is refused because this module's
# only consumer is an HTML extractor and handing it something else would produce a draft from
# bytes nobody read as a document.
PERMITTED_CONTENT_TYPES = frozenset({"text/html", "application/xhtml+xml"})

# Where the identity of a retrieved document is taken. Named because the boundary matters and is
# argued in `RetrievedDocument.content_hash`.
CONTENT_DIGEST_ALGORITHM = "sha256"


class RefusedTargetError(Exception):
    """A URL this application will not fetch, decided without connecting to anything.

    Separate from a retrieval that was attempted and failed, because the two are different facts
    about a merchant's request. This one says the target is outside what AgentRank fetches at
    all, and no amount of retrying changes it. It carries a stable reason code and a sentence
    this repository wrote.
    """

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(detail)
        self.reason = reason
        self.detail = detail


@dataclass(frozen=True, slots=True)
class AddressPolicy:
    """Which IP addresses this deployment may connect to when it fetches a merchant page.

    The default answer is "globally routable addresses only", and it is expressed as a predicate
    over a resolved address rather than as a list of forbidden names. A forbidden-name list is
    the version of this that does not work: `localhost` is one spelling of 127.0.0.1 out of
    infinitely many, and a name the attacker controls can resolve to anything at all.

    `permitted_networks` exists for one situation, which is a test or a development machine
    fetching a synthetic merchant fixture served on loopback. It is refused by `Settings` in any
    environment that is not development, CI or test, so a deployment cannot hold one. Empty here
    rather than optional, so the safe policy is the one you get by constructing this with no
    arguments.

    The port rule travels with the address rule because the two answer one question. Restricting
    ports on the public internet is what stops one permitted fetch from becoming a port scanner
    aimed at whatever else is out there. A policy that has already been widened to a private
    network is a development or test process reaching a fixture on an ephemeral port, where a port
    restriction buys nothing that the address restriction has not already decided, so `any_port`
    travels with `permitted_networks` and never appears without one.
    """

    permitted_networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...] = ()
    any_port: bool = False

    def __post_init__(self) -> None:
        if self.any_port and not self.permitted_networks:
            raise ValueError("a policy may not permit any port without permitting a network")

    def permits(self, address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
        """Whether one resolved address may be connected to."""
        if any(address in network for network in self.permitted_networks):
            return True
        return _globally_routable(_unwrapped(address))

    def permits_port(self, port: int) -> bool:
        """Whether a merchant page may be fetched from this port."""
        return self.any_port or port in PERMITTED_PORTS


PUBLIC_ONLY = AddressPolicy()


def _unwrapped(
    address: ipaddress.IPv4Address | ipaddress.IPv6Address,
) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    """The IPv4 address hiding inside an IPv6 one, when there is one.

    `::ffff:127.0.0.1`, `2002:7f00:1::` and a Teredo address all carry an IPv4 address in their
    bits, and a stack asked to connect to one may reach the IPv4 address it names. Python's own
    classification already answers correctly for every case this repository could find, so this
    is not a fix for a hole that was measured. It is here because the classification of the outer
    address answering correctly is a property of the standard library's constant tables, and the
    property this module needs is about what a socket will do.
    """
    if isinstance(address, ipaddress.IPv6Address):
        if address.ipv4_mapped is not None:
            return address.ipv4_mapped
        if address.sixtofour is not None:
            return address.sixtofour
        if address.teredo is not None:
            return address.teredo[1]
    return address


def _globally_routable(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Whether one address is a public internet address and nothing else.

    Spelled out rather than left to `is_global` alone, and that is not redundancy. Multicast
    addresses report `is_global` as true, because they are globally scoped in the sense that
    property means, and a multicast destination is not a merchant storefront. Every other clause
    is implied by `is_global` today and is written down because this predicate is the security
    boundary and should read as the list of things it excludes.
    """
    return (
        address.is_global
        and not address.is_multicast
        and not address.is_reserved
        and not address.is_loopback
        and not address.is_link_local
        and not address.is_private
        and not address.is_unspecified
    )


@dataclass(frozen=True, slots=True)
class FetchLimits:
    """What one page retrieval may cost, in bytes and in seconds.

    Server authoritative by construction: there is no request field anywhere that carries one of
    these, and the console cannot raise one. A merchant whose page is larger than this is told
    which bound refused it.
    """

    max_response_bytes: int = MAX_RESPONSE_BYTES
    max_redirects: int = MAX_REDIRECTS
    connect_timeout_seconds: float = CONNECT_TIMEOUT_SECONDS
    page_timeout_seconds: float = PAGE_TIMEOUT_SECONDS


@dataclass(frozen=True, slots=True)
class ImportTarget:
    """One URL that passed every check that can be made without connecting to anything.

    The host is carried as a field rather than read back off the URL, and that is not tidiness.
    `httpx2.URL.host` is a lazy property that IDNA *decodes* on every access, so reading it back
    returns Unicode for an internationalized name, which then fails to encode as a `Host` header,
    and raises outright for a name whose punycode is malformed. Both are page or request content,
    so both were an unhandled server error. What is stored here is the ASCII form that actually
    goes on the wire.
    """

    url: httpx2.URL
    host: str

    @property
    def port(self) -> int:
        port = self.url.port
        return DEFAULT_PORTS[self.url.scheme] if port is None else port

    @property
    def origin(self) -> str:
        """Scheme, host and port, as the one string that decides whether two URLs are one site."""
        return f"{self.url.scheme}://{self.host}:{self.port}"

    @property
    def text(self) -> str:
        return str(self.url)


@dataclass(frozen=True, slots=True)
class RetrievedDocument:
    """One merchant page, as bytes that were read and never as something that was executed.

    `body` is what arrived after transfer decoding and content decoding, which is the boundary
    the size bound is enforced at and the boundary the digest is taken at. Anything later is
    interpretation: which characters those bytes spell depends on a charset, and which elements
    they describe depends on a parser, and neither should be able to change what two retrievals
    of an unchanged page hash to.
    """

    target: ImportTarget
    final_url: str
    status_code: int
    content_type: str
    charset: str | None
    body: bytes
    redirects: tuple[str, ...]

    @property
    def byte_count(self) -> int:
        return len(self.body)

    @property
    def content_hash(self) -> str:
        digest = hashlib.sha256(self.body).hexdigest()
        return f"{CONTENT_DIGEST_ALGORITHM}:{digest}"

    def text(self) -> str:
        """The document as characters, decoded by what the response said it was.

        `replace` rather than a refusal. A byte sequence that is not valid in the declared
        charset is a merchant page with a mistake in it, which is ordinary, and refusing the
        whole import over one malformed byte would be refusing to read a page a browser renders.
        What the replacement character can never do is become a fact: every extracted value is
        matched against a shape before it is used.
        """
        return self.body.decode(self.charset or "utf-8", errors="replace")


@dataclass(frozen=True, slots=True)
class RetrievalFailure:
    """One page that was attempted and did not produce a document.

    A value rather than an exception because an import fetches several pages and one that failed
    is a fact to report beside the ones that did not, not a reason to abandon the rest. The
    reason is a code this repository chose; nothing an upstream said is in it.
    """

    target: ImportTarget
    reason: str
    detail: str
    status_code: int | None = None


def validate_target(raw: str, *, policy: AddressPolicy = PUBLIC_ONLY) -> ImportTarget:
    """One URL, checked for every property that does not require a connection.

    `policy` is read only to decide whether a URL whose host is already an address literal is
    permitted, which is the one check here that is about an address rather than about a shape.
    Resolving a name is `resolve_addresses`, which happens per hop and immediately before the
    connection that uses it.
    """
    if len(raw) > MAX_URL_LENGTH:
        raise RefusedTargetError("url_too_long", "that URL is longer than AgentRank will fetch")
    if raw.strip() != raw or not raw.strip():
        raise RefusedTargetError("url_malformed", "that URL is blank or has surrounding spaces")
    try:
        url = httpx2.URL(raw)
    except Exception as error:
        raise RefusedTargetError("url_malformed", "that is not a URL AgentRank can read") from error

    if url.scheme not in PERMITTED_SCHEMES:
        raise RefusedTargetError(
            "scheme_not_permitted", "a merchant page must be an http or https URL"
        )
    # A credential in a URL is refused rather than stripped. Stripping it would fetch something
    # the caller did not ask for, and the caller asking for it at all is either an attempt to
    # authenticate to somebody else's host or a secret about to be written to a database.
    if url.username or url.password:
        raise RefusedTargetError(
            "credential_in_url", "a merchant page URL may not carry a username or a password"
        )
    try:
        port = url.port
    except (ValueError, TypeError) as error:
        raise RefusedTargetError("url_malformed", "that URL has an unreadable port") from error
    if port is not None and not policy.permits_port(port):
        raise RefusedTargetError(
            "port_not_permitted", "a merchant page must be served on port 80 or 443"
        )

    host = _canonical_host(url)
    literal = _address_literal(host)
    if literal is not None and not policy.permits(literal):
        raise RefusedTargetError(
            "address_not_permitted", "that address is not a public internet address"
        )
    # The fragment is dropped rather than carried. It is never sent to a server, so keeping one
    # would mean storing a piece of a URL that had no effect on what was fetched, and comparing
    # two imports would report a difference that was not one.
    return ImportTarget(url=url.copy_with(host=host, fragment=None), host=host)


def _canonical_host(url: httpx2.URL) -> str:
    """One host, in the single ASCII spelling everything downstream compares and connects against.

    Taken from `raw_host`, which is the form the URL parser already produced for the wire. Doing
    the conversion here instead was a real defect twice over. It used the standard library's
    `idna` codec, which is IDNA 2003 with nameprep, while the parser uses the `idna` package,
    which is IDNA 2008; the two disagree, and `fa\u00df.de` and `fass.de` are the canonical
    disagreement and are separately registrable domains. A merchant asking for one would have had
    the other fetched, and the import record would have said so. It also read `URL.host`, which
    decodes back to Unicode and raises for a malformed punycode label.

    Two normalizations remain, each a way for two spellings of one host to look like two hosts.
    Case, because DNS is case insensitive and a same-origin check is not. And the root label,
    because `example.com.` and `example.com` are one name to a resolver.
    """
    raw = url.raw_host
    if not raw:
        raise RefusedTargetError("url_malformed", "that URL names no host")
    try:
        lowered = raw.decode("ascii").lower().rstrip(".")
    except UnicodeDecodeError as error:  # pragma: no cover - the parser produces ASCII
        raise RefusedTargetError(
            "url_malformed", "that URL names a host AgentRank cannot read"
        ) from error
    if not lowered:
        raise RefusedTargetError("url_malformed", "that URL names no host")
    return lowered


def _address_literal(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """The address a host already is, when it is one rather than a name to look up."""
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        return None


async def resolve_addresses(
    target: ImportTarget, *, policy: AddressPolicy = PUBLIC_ONLY
) -> tuple[str, ...]:
    """Every address this host resolves to, refused unless the policy permits all of them.

    All of them, and that is the load bearing word. Picking the permitted ones and connecting to
    those would make a name that answers with one public address and one loopback address into a
    permitted target, and which of the two a connection reaches is then a race. A name that
    resolves partly inside is not a merchant storefront, and the whole target is refused.

    Deterministic in what it refuses and deliberately not in what it returns beyond the first
    entry: `getaddrinfo` decides the order, the caller connects to the first, and the rest exist
    so that the check covers what a different resolution round could have chosen.
    """
    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(target.host, target.port, type=socket.SOCK_STREAM)
    except socket.gaierror as error:
        raise RefusedTargetError(
            "host_not_resolvable", "that host name does not resolve"
        ) from error
    addresses: list[str] = []
    for info in infos:
        literal = str(info[4][0])
        if literal not in addresses:
            addresses.append(literal)
    if not addresses:
        raise RefusedTargetError("host_not_resolvable", "that host name does not resolve")
    for literal in addresses:
        if not policy.permits(ipaddress.ip_address(literal)):
            raise RefusedTargetError(
                "address_not_permitted", "that host resolves to an address that is not public"
            )
    return tuple(addresses)


class MerchantPageFetcher:
    """Bounded public GET retrieval of merchant pages, one connection policy for all of them.

    Holds one client for the life of an import so that several pages on one storefront reuse a
    connection, and constructs it with redirects disabled, because redirects are this module's
    job and a client that followed one would follow it without any of the checks here.

    Nothing about a response influences what is fetched next except a `Location` header, which is
    treated as a URL to validate from scratch rather than as an instruction to obey.
    """

    def __init__(
        self,
        *,
        policy: AddressPolicy = PUBLIC_ONLY,
        limits: FetchLimits | None = None,
        client: httpx2.AsyncClient | None = None,
    ) -> None:
        self._policy = policy
        self._limits = limits or FetchLimits()
        self._owns_client = client is None
        self._client = client or httpx2.AsyncClient(
            follow_redirects=False,
            timeout=httpx2.Timeout(
                self._limits.page_timeout_seconds,
                connect=self._limits.connect_timeout_seconds,
            ),
            # No connection is kept alive, and that is a security decision rather than a
            # politeness one. The pool keys a connection on the origin it was opened for, and
            # this module rewrites every request's host to the validated address literal, so the
            # key is an address. Two host names behind one address, which is what shared hosting
            # and every CDN are, would therefore share one TLS connection, and `sni_hostname` is
            # read only when a connection is established. The second name's certificate would
            # never be checked. One connection per page costs a handshake and removes that
            # entirely.
            limits=httpx2.Limits(max_connections=1, max_keepalive_connections=0),
            headers={"User-Agent": USER_AGENT, "Accept": ACCEPT},
        )

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def fetch(self, target: ImportTarget) -> RetrievedDocument | RetrievalFailure:
        """One merchant page, or the named reason there is not one.

        The whole hop chain is inside one deadline. Bounding each hop separately would let a
        chain of redirects that each answer just inside their own bound add up to a request that
        never returns, and the merchant is waiting on this synchronously.
        """
        try:
            async with asyncio.timeout(self._limits.page_timeout_seconds):
                return await self._chain(target)
        except TimeoutError:
            return RetrievalFailure(target, "timeout", "that page did not answer in time")
        except RefusedTargetError as refused:
            return RetrievalFailure(target, refused.reason, refused.detail)
        except httpx2.HTTPError:
            # Every transport failure is one fact about the merchant's site: it did not answer.
            # A connection refused, a TLS handshake that failed and a body that ended early are
            # not distinctions a merchant can act on differently, and the exception text is a
            # library's prose that would end up in a stored import record.
            return RetrievalFailure(target, "unreachable", "that page could not be retrieved")
        except UnicodeError, httpx2.InvalidURL:
            # A `Location` header this repository never asked anybody to parse. The client builds
            # a redirect request on every redirect response in order to expose it, whether or not
            # it is configured to follow one, and building it reads the target's host, which IDNA
            # decodes and raises on a malformed punycode label. So an attacker chosen page could
            # raise out of the whole boundary, discarding an in-flight import of a dozen pages
            # with nothing written and nothing the merchant could see. It is a refusal like any
            # other unreadable redirect.
            return self._unreadable_redirect(target)

    async def _chain(self, target: ImportTarget) -> RetrievedDocument | RetrievalFailure:
        """Follow up to the permitted number of redirects, validating every hop as a new target."""
        redirects: list[str] = []
        current = target
        for _ in range(self._limits.max_redirects + 1):
            outcome = await self._once(current)
            if isinstance(outcome, RetrievedDocument):
                # Reported against the URL the merchant supplied rather than the one that finally
                # answered, so that a page reads as the page they asked for. Where it ended up is
                # `final_url` and how it got there is `redirects`, both of which are provenance.
                return RetrievedDocument(
                    target=target,
                    final_url=outcome.final_url,
                    status_code=outcome.status_code,
                    content_type=outcome.content_type,
                    charset=outcome.charset,
                    body=outcome.body,
                    redirects=tuple(redirects),
                )
            if isinstance(outcome, RetrievalFailure):
                return RetrievalFailure(target, outcome.reason, outcome.detail, outcome.status_code)
            redirects.append(outcome.text)
            current = outcome
        return RetrievalFailure(
            target, "too_many_redirects", "that page redirected more times than AgentRank follows"
        )

    async def _once(
        self, target: ImportTarget
    ) -> RetrievedDocument | RetrievalFailure | ImportTarget:
        """One hop. A document, a named failure, or the next target to validate and try."""
        addresses = await resolve_addresses(target, policy=self._policy)
        request = self._client.build_request(
            "GET",
            target.url.copy_with(host=addresses[0]),
            headers={"Host": _host_header(target)},
            # The name, not the number. TLS is negotiated for the merchant's host name and the
            # certificate is verified against it, while the socket goes to the address that was
            # checked. Without this a pinned connection would present the address literal as its
            # server name and every HTTPS fetch would fail certificate verification, which is the
            # failure mode that tempts people to disable verification instead.
            extensions={"sni_hostname": target.host},
        )
        response = await self._client.send(request, stream=True)
        try:
            if response.is_redirect:
                return self._next_hop(target, response)
            if response.status_code >= 400:
                return RetrievalFailure(
                    target,
                    "http_error",
                    "that page answered with an error status",
                    response.status_code,
                )
            content_type, charset = _content_type(response)
            if content_type not in PERMITTED_CONTENT_TYPES:
                return RetrievalFailure(
                    target,
                    "content_type_not_permitted",
                    "that URL did not answer with an HTML page",
                    response.status_code,
                )
            declared = _declared_length(response)
            if declared is not None and declared > self._limits.max_response_bytes:
                return RetrievalFailure(
                    target,
                    "response_too_large",
                    "that page is larger than AgentRank will read",
                    response.status_code,
                )
            body = await self._read(response)
            if body is None:
                return RetrievalFailure(
                    target,
                    "response_too_large",
                    "that page is larger than AgentRank will read",
                    response.status_code,
                )
            return RetrievedDocument(
                target=target,
                final_url=target.text,
                status_code=response.status_code,
                content_type=content_type,
                charset=charset,
                body=body,
                redirects=(),
            )
        finally:
            await response.aclose()

    @staticmethod
    def _unreadable_redirect(target: ImportTarget) -> RetrievalFailure:
        return RetrievalFailure(
            target,
            "redirect_malformed",
            "that page redirected to something AgentRank cannot read",
        )

    def _next_hop(self, target: ImportTarget, response: httpx2.Response) -> ImportTarget:
        """Where a redirect points, as a target that has proved nothing yet.

        Resolved against the URL that was actually requested rather than against the connection's
        address literal, so a relative `Location` lands on the merchant's host. Validated by the
        same function the merchant's own URL went through, so a redirect cannot reach anything a
        submitted URL could not.
        """
        location = response.headers.get("location", "")
        if not location.strip():
            raise RefusedTargetError("redirect_malformed", "that page redirected to nowhere")
        try:
            destination = target.url.join(location)
        except Exception as error:
            raise RefusedTargetError(
                "redirect_malformed", "that page redirected to something AgentRank cannot read"
            ) from error
        return validate_target(str(destination), policy=self._policy)

    async def _read(self, response: httpx2.Response) -> bytes | None:
        """The body, or None if it exceeded the bound while it was arriving.

        Accumulated with a running count and abandoned the moment it passes, so a response with
        no declared length and a response whose declared length lied are refused by the same
        check. The count is over decoded bytes, which is what makes it a bound on a compressed
        body that expands.
        """
        chunks: list[bytes] = []
        total = 0
        async for chunk in response.aiter_bytes():
            total += len(chunk)
            if total > self._limits.max_response_bytes:
                return None
            chunks.append(chunk)
        return b"".join(chunks)


# What AgentRank says it is. A merchant looking at their own access log should be able to see that
# this was AgentRank importing their pages, and a site that would rather not be fetched has a name
# to refuse. No version, because the version of this application is not a merchant's business and
# would be one more thing to keep in step.
USER_AGENT = "AgentRankImporter/1 (+merchant source import; contact your AgentRank operator)"

ACCEPT = "text/html,application/xhtml+xml"


def _host_header(target: ImportTarget) -> str:
    """The `Host` a merchant's server should see, which is its name and not the address dialled.

    The default port is left out. `example.com` and `example.com:443` are the same host to a
    correct server and are different strings to a virtual host configuration, and a fetch that
    works in a browser and fails here because of a port suffix is a bug nobody would find.
    """
    port = target.url.port
    if port is None or port == DEFAULT_PORTS[target.url.scheme]:
        return target.host
    return f"{target.host}:{port}"


def _content_type(response: httpx2.Response) -> tuple[str, str | None]:
    """The media type and the charset a response declared, separated and lowercased."""
    raw = response.headers.get("content-type", "")
    parts = raw.split(";")
    media = parts[0].strip().lower()
    charset: str | None = None
    for parameter in parts[1:]:
        name, _, value = parameter.partition("=")
        if name.strip().lower() == "charset":
            candidate = value.strip().strip('"').lower()
            if candidate:
                charset = candidate
    return media, _known_charset(charset)


def _known_charset(charset: str | None) -> str | None:
    """A charset Python can decode with, or None so that the caller falls back to UTF-8.

    A merchant page declaring a charset nobody has heard of is a merchant page with a typo in it,
    and the answer to one is to read it as UTF-8 with replacement rather than to refuse the
    import. Checked here rather than at decode time so that the stored record says which charset
    was actually used.
    """
    if charset is None:
        return None
    try:
        "".encode(charset)
    except LookupError:
        return None
    return charset


def _declared_length(response: httpx2.Response) -> int | None:
    """What the response said its body would be, when it said anything readable."""
    raw = response.headers.get("content-length")
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


@dataclass(frozen=True, slots=True)
class RetrievalLogLine:
    """What one retrieval may be written down as, outside the import record itself.

    A merchant's URL carries their own business in its path and its query, and an import fetches
    pages a merchant chose while they are logged in. So the log line is the origin, a bounded
    path, and numbers. The query string is dropped rather than truncated, because half a query
    string is still the merchant's data and is additionally useless.
    """

    origin: str
    path: str
    status_code: int | None
    byte_count: int

    @classmethod
    def of(
        cls, target: ImportTarget, *, status_code: int | None = None, byte_count: int = 0
    ) -> Self:
        path = target.url.path or "/"
        return cls(
            origin=target.origin,
            path=path[:120],
            status_code=status_code,
            byte_count=byte_count,
        )

    def __str__(self) -> str:
        return f"{self.origin}{self.path} status={self.status_code} bytes={self.byte_count}"


@dataclass(frozen=True, slots=True)
class PermittedNetworks:
    """A parsed list of extra networks one non-deployment environment may reach.

    Its own type so that the parsing failure has one sentence, and so that `Settings` holds
    something already validated rather than a string every reader has to re-interpret.
    """

    networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...] = field(default=())

    @classmethod
    def parse(cls, raw: str | Iterable[str]) -> Self:
        entries: Sequence[str]
        entries = raw.split(",") if isinstance(raw, str) else list(raw)
        networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
        for entry in entries:
            text = entry.strip()
            if not text:
                continue
            try:
                networks.append(ipaddress.ip_network(text, strict=False))
            except ValueError as error:
                raise ValueError(f"{text!r} is not a network in CIDR notation") from error
        return cls(networks=tuple(networks))

    def policy(self) -> AddressPolicy:
        """The widened policy these networks describe, ports included.

        `any_port` rather than a second variable, because the situation this exists for is a
        fixture on an ephemeral port and a deployment can hold neither half.
        """
        return AddressPolicy(permitted_networks=self.networks, any_port=bool(self.networks))
