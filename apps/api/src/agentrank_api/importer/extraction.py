"""Deterministic extraction of merchant facts from one page that was already read.

Two paths and no third. A page either publishes schema.org product data, or it publishes the
Open Graph and product metadata tags that shopping surfaces already consume, or AgentRank does not
import it. Both paths read machine readable data the merchant authored and published on purpose.
Neither reads running prose, and there is no CSS selector anywhere in this repository.

That is a real restriction and it is the right one. The alternative is a per merchant selector
table, which is bespoke repository code for every storefront, or a general "find the price in the
page" heuristic, which cannot tell a product's price from a cross sell's price, a struck through
price, a bundle price or a free shipping threshold. Either way a number nobody can explain ends up
in a merchant's source history. A page whose price AgentRank cannot read is reported as one, and
the merchant writes that product themselves, which they could always do.

Precedence is stated rather than emergent. Structured data wins, because it is the merchant
saying what the thing is rather than how it looks. Metadata tags are consulted only when the page
publishes no product node at all, or publishes one carrying no offer at all. They are never
consulted to replace a figure that structured data published and this module refused: refusing an
ambiguous price and then quietly using a different one from elsewhere on the page is the same
mistake as guessing, with an extra step.

What a page says is data throughout. `@type` decides nothing about control flow beyond which of
two extractors runs, no value is executed, no URL found in a document is fetched, and every string
that survives into a draft has been matched against a shape or bounded to a length. The one shape
this repository has already decided is never legitimate merchant prose, text that addresses
whatever reads it next, is refused here so that the merchant is told which page it was on, rather
than at the source intake where it would surface as an unexplained rejection of the whole import.
"""

import hashlib
import json
import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from agentrank_api.importer.amounts import RefusedAmountError, minor_units, normalize_currency
from agentrank_api.importer.draft import (
    AvailabilityEvidence,
    DraftPolicy,
    DraftProduct,
    DraftVariant,
    ExtractionMethod,
    Finding,
    Omission,
    bounded_policy_body,
)
from agentrank_api.importer.reading import PageReading, collapse
from agentrank_api.representation.definitions import instruction_like
from agentrank_api.representation.schemas import (
    IDENTIFIER_PATTERN,
    MAX_CATEGORY_LENGTH,
    MAX_DESCRIPTION_LENGTH,
    MAX_INVENTORY_QUANTITY,
    MAX_LABEL_LENGTH,
    MAX_TITLE_LENGTH,
    MAX_VARIANTS_PER_PRODUCT,
)

# schema.org types this module recognizes, lowercased and with any namespace prefix removed.
PRODUCT_TYPES = frozenset({"product", "productgroup", "productmodel"})
AGGREGATE_OFFER_TYPE = "aggregateoffer"
BREADCRUMB_TYPE = "breadcrumblist"

# How far into a structured data document nodes are collected from. Only the places a merchant
# says "this document is about this": the top level, a `@graph`, and a `mainEntity` pointer.
# Walking every key instead would collect the related products, the reviews and the breadcrumb
# entries as though the page were about them.
MAX_STRUCTURED_DEPTH = 6

# Availability tokens, as schema.org publishes them and as the Open Graph product tags spell them.
# Anything not listed is `UNKNOWN`, which is an answer rather than a gap: it says the merchant
# published something AgentRank does not recognize as a stock statement.
IN_STOCK_TOKENS = frozenset(
    {"instock", "in stock", "instoreonly", "onlineonly", "limitedavailability", "in_stock"}
)
OUT_OF_STOCK_TOKENS = frozenset(
    {"outofstock", "out of stock", "soldout", "sold out", "discontinued", "out_of_stock", "oos"}
)

# What a page's own metadata calls a price, in the order a page carrying several should be read.
# The `product:` namespace is the more specific statement and wins over the generic Open Graph one.
PRICE_AMOUNT_TAGS = ("product:price:amount", "og:price:amount")
PRICE_CURRENCY_TAGS = ("product:price:currency", "og:price:currency")
AVAILABILITY_TAGS = ("product:availability", "og:availability", "availability")
IDENTIFIER_TAGS = ("product:retailer_item_id", "product:sku", "product:mpn", "og:sku")
TITLE_TAGS = ("og:title", "twitter:title")
DESCRIPTION_TAGS = ("og:description", "description", "twitter:description")
CATEGORY_TAGS = ("product:category", "article:section")

MAX_AVAILABILITY_TEXT = 120

_IDENTIFIER = re.compile(IDENTIFIER_PATTERN)
_DISALLOWED = re.compile(r"[^A-Za-z0-9_-]+")
_LEADING = re.compile(r"^[^A-Za-z0-9]+")

MAX_IDENTIFIER_LENGTH = 64


@dataclass(frozen=True, slots=True)
class ProductExtraction:
    """What one product page produced: a product, or the stated reason there is not one."""

    product: DraftProduct | None
    omission: Omission | None
    findings: tuple[Finding, ...] = ()


@dataclass(frozen=True, slots=True)
class PolicyExtraction:
    """What one policy page produced."""

    policy: DraftPolicy | None
    omission: Omission | None
    findings: tuple[Finding, ...] = ()


class IdentityCollisionError(Exception):
    """Two different merchant strings that would become one identifier.

    Raised rather than resolved, because every way of resolving one is a silent rename of somebody
    else's product. It reaches the caller as an omission naming the page.
    """


class Identifiers:
    """Stable, unique, pattern valid identities for the things one import found.

    An external identifier and a SKU are addresses. A compiler candidate cites
    `products[X].variants[Y].sku`, a source field address parses on dots and brackets, and the
    source schema restricts both to letters, digits, hyphens and underscores for exactly that
    reason. A merchant's own identifier is frequently none of those things.

    The rule is a pure function of the merchant's string, and that is the property that took two
    attempts to get right. An identifier that is derived from the string *and* from what has
    already been assigned depends on the order pages were listed in, which means the same
    storefront imported with its URLs in a different order produces a different canonical document
    and therefore a spurious new source snapshot, with SKUs swapped between real variants.

    So:

    ```text
    the string is already an identifier    it is used as it is, and nothing was lost
    it is not                              a bounded slug plus a digest of the whole string
    ```

    A string that is already a valid identifier cannot collide with another such string without
    being equal to it. A string that is not always carries its own digest. What is left is the
    astronomically unlikely case of a merchant literally using another string's slug and digest as
    a SKU, and that raises rather than renaming anything.

    Two namespaces, because a source document requires product identifiers unique among products
    and SKUs unique among variants, and those are separate questions. Sharing one namespace would
    give a product and its only variant different names for no reason.
    """

    def __init__(self) -> None:
        self._products: dict[str, str] = {}
        self._skus: dict[str, str] = {}

    def product(self, raw: str) -> str:
        """The external identifier for one merchant product string."""
        return _assign(self._products, raw)

    def sku(self, raw: str) -> str:
        """The SKU for one merchant variant string."""
        return _assign(self._skus, raw)


def _assign(taken: dict[str, str], raw: str) -> str:
    """One identifier, decided by the string alone, refused if it is somebody else's."""
    candidate = raw if _IDENTIFIER.fullmatch(raw) else _derived(raw)
    settled = taken.get(candidate)
    if settled is not None and settled != raw:
        raise IdentityCollisionError(candidate)
    taken[candidate] = raw
    return candidate


def _derived(raw: str) -> str:
    """A bounded slug of a merchant string plus a digest of the whole of it.

    The digest is what makes this injective in practice. The slug alone is lossy: a merchant with
    `Blue / Large` and `Blue - Large` would get one identifier for two variants, and a source
    document would lose one of them to its uniqueness check.
    """
    suffix = f"-{_digest(raw)}"
    base = _slug(raw)[: MAX_IDENTIFIER_LENGTH - len(suffix)]
    return f"{base}{suffix}" if base else f"item{suffix}"


def _slug(raw: str) -> str:
    """A merchant string reduced to the identifier alphabet, deterministically."""
    replaced = _DISALLOWED.sub("-", raw.strip())
    trimmed = _LEADING.sub("", replaced).strip("-_")
    collapsed = re.sub(r"-{2,}", "-", trimmed)[:MAX_IDENTIFIER_LENGTH].strip("-_")
    return collapsed if _IDENTIFIER.fullmatch(collapsed) else ""


def _digest(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]


def structured_nodes(reading: PageReading) -> tuple[list[dict[str, Any]], int]:
    """Every schema.org node this page published, and how many blocks could not be parsed.

    Parsed with `parse_float=Decimal`, which is the whole reason this parses structured data here
    rather than anywhere else. A price of 4999.10 read as a float has already lost exactness
    before any money rule can look at it, and no later care recovers it.
    """
    nodes: list[dict[str, Any]] = []
    malformed = 0
    for block in reading.structured_blocks:
        text = block.strip()
        if not text:
            continue
        try:
            value = json.loads(text, parse_float=Decimal)
        except ValueError, RecursionError:
            malformed += 1
            continue
        _collect(value, nodes, 0)
    return nodes, malformed


def _collect(value: Any, out: list[dict[str, Any]], depth: int) -> None:
    if depth > MAX_STRUCTURED_DEPTH or len(out) > 200:
        return
    if isinstance(value, list):
        for item in value:
            _collect(item, out, depth + 1)
        return
    if not isinstance(value, dict):
        return
    out.append(value)
    graph = value.get("@graph")
    if graph is not None:
        _collect(graph, out, depth + 1)
    main = value.get("mainEntity")
    if main is not None:
        _collect(main, out, depth + 1)


def _types(node: dict[str, Any]) -> set[str]:
    """The schema.org types one node claims, lowercased and without a namespace prefix."""
    raw = node.get("@type")
    values = raw if isinstance(raw, list) else [raw]
    found: set[str] = set()
    for value in values:
        if isinstance(value, str):
            found.add(value.strip().rsplit("/", 1)[-1].rsplit(":", 1)[-1].lower())
    return found


def extract_product(
    reading: PageReading, *, source_url: str, identifiers: Identifiers
) -> ProductExtraction:
    """One product page, as a draft product or as a stated reason it is not one."""
    nodes, malformed = structured_nodes(reading)
    findings: list[Finding] = []
    if malformed:
        findings.append(
            Finding(
                source_url,
                "structured_data_malformed",
                f"{malformed} structured data block(s) on this page could not be parsed",
            )
        )
    products = _product_nodes(nodes)
    if len(products) > 1:
        return ProductExtraction(
            None,
            Omission(
                source_url,
                "several_products",
                "this page publishes more than one product, so AgentRank cannot tell which one"
                " it is about",
            ),
            tuple(findings),
        )
    try:
        if products:
            structured = _from_structured(products[0], reading, source_url, identifiers, nodes)
            if structured is not None:
                return ProductExtraction(
                    structured.product, structured.omission, tuple(findings) + structured.findings
                )
        outcome = _from_metadata(reading, source_url, identifiers, nodes)
    except IdentityCollisionError as collision:
        # Two different merchant strings that would become one address. Renaming one of them would
        # be this importer deciding that somebody else's product is now called something else.
        return ProductExtraction(
            None,
            Omission(
                source_url,
                "identifier_collision",
                "an identifier on this page would collide with one already imported, and"
                " AgentRank will not rename either of them",
                str(collision),
            ),
            tuple(findings),
        )
    return ProductExtraction(outcome.product, outcome.omission, tuple(findings) + outcome.findings)


def _product_nodes(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The product nodes a page published, with a node reachable twice counted once.

    A `@graph` commonly names one node from several places, and a page that says one thing twice
    has published one product. Identity is the node's `@id` when it has one, because that is what
    a repeated reference shares, and the node itself otherwise.
    """
    found: list[dict[str, Any]] = []
    seen: set[str] = set()
    for node in nodes:
        if not (PRODUCT_TYPES & _types(node)):
            continue
        identity = node.get("@id")
        key = str(identity) if isinstance(identity, str) and identity.strip() else _shape(node)
        if key in seen:
            continue
        seen.add(key)
        found.append(node)
    return found


def _shape(node: dict[str, Any]) -> str:
    try:
        return json.dumps(node, sort_keys=True, default=str)[:4096]
    except TypeError, ValueError:  # pragma: no cover - default=str handles the known cases
        return repr(node)[:4096]


def _from_structured(
    node: dict[str, Any],
    reading: PageReading,
    source_url: str,
    identifiers: Identifiers,
    nodes: list[dict[str, Any]],
) -> ProductExtraction | None:
    """A product from schema.org data, or None when the node carries no offer at all.

    None means "this path had nothing to say", which lets the metadata path run. It is
    deliberately not what a refused price returns: that comes back as an omission, because a page
    whose published price AgentRank would not read is not a page to go looking for another number
    on.
    """
    entries = _variant_entries(node)
    if entries is None:
        return None
    title = _text(node.get("name")) or _fallback_title(reading)
    if title is None:
        return ProductExtraction(
            None,
            Omission(source_url, "title_missing", "this page publishes no product title"),
            (),
        )
    unsafe = _instruction_like_field(node, title, reading)
    if unsafe is not None:
        return ProductExtraction(None, Omission(source_url, "instruction_like", unsafe), ())

    findings: list[Finding] = []
    variants: list[DraftVariant] = []
    prices: dict[str, tuple[int, str]] = {}
    for index, (owner, offers) in enumerate(entries):
        built = _variant(owner, offers, node, index, source_url, identifiers)
        if isinstance(built, Omission):
            return ProductExtraction(None, built, tuple(findings))
        settled = prices.get(built.sku)
        if settled is not None and settled != (built.price_amount_minor, built.currency):
            return ProductExtraction(
                None,
                Omission(
                    source_url,
                    "price_conflict",
                    "this page publishes two different prices for one SKU",
                    built.sku,
                ),
                tuple(findings),
            )
        if settled is not None:
            # Two entries under one SKU. Dropping the second silently would publish a catalog
            # that quietly disagrees with the merchant's own page, which is the same rule a
            # refused variant follows: the product is not imported and the reason is stated.
            return ProductExtraction(
                None,
                Omission(
                    source_url,
                    "variant_ambiguous",
                    "this page publishes two variants under one SKU",
                    built.sku,
                ),
                tuple(findings),
            )
        prices[built.sku] = (built.price_amount_minor, built.currency)
        variants.append(built)
    if not variants:
        return None
    if len(variants) > MAX_VARIANTS_PER_PRODUCT:
        findings.append(
            Finding(
                source_url,
                "variants_truncated",
                f"this page publishes {len(variants)} variants and AgentRank imported the first"
                f" {MAX_VARIANTS_PER_PRODUCT}",
            )
        )
        variants = variants[:MAX_VARIANTS_PER_PRODUCT]

    conflict = _metadata_price_conflict(reading, variants, source_url)
    if conflict is not None:
        return ProductExtraction(None, conflict, tuple(findings))

    external = _external_identity(node, source_url, identifiers)
    description, truncated = _bounded(_text(node.get("description")), MAX_DESCRIPTION_LENGTH)
    if truncated:
        findings.append(
            Finding(source_url, "description_truncated", "the description was cut to fit a source")
        )
    return ProductExtraction(
        DraftProduct(
            external_id=external,
            title=title[:MAX_TITLE_LENGTH],
            description=description,
            category=_category(node, nodes, title),
            source_url=source_url,
            extraction=ExtractionMethod.STRUCTURED_DATA,
            variants=tuple(variants),
        ),
        None,
        tuple(findings),
    )


def _variant_entries(
    node: dict[str, Any],
) -> list[tuple[dict[str, Any], list[dict[str, Any]]]] | None:
    """Which nodes are this product's variants, and which offers belong to each.

    Two published shapes, taken in the order they are specific. `hasVariant` is a merchant saying
    "these are the versions of this product", which is the strongest statement available. A list
    of offers under one product is the older shape and means the same thing when the offers carry
    distinct SKUs.

    None when the product publishes no offer anywhere, which is absence rather than refusal.
    """
    variants = node.get("hasVariant")
    if isinstance(variants, list):
        entries = [
            (item, _offers(item)) for item in variants if isinstance(item, dict) and _offers(item)
        ]
        if entries:
            return entries
    offers = _offers(node)
    if not offers:
        return None
    return [(node, [offer]) for offer in offers]


def _offers(node: dict[str, Any]) -> list[dict[str, Any]]:
    """One node's offers, with an aggregate that contains real offers replaced by them.

    An `AggregateOffer` naming a low and a high price is a summary rather than an offer, and it is
    left in place here so that the money rules can refuse it by name when the two disagree.
    """
    raw = node.get("offers")
    candidates = raw if isinstance(raw, list) else [raw]
    found: list[dict[str, Any]] = []
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        if AGGREGATE_OFFER_TYPE in _types(candidate):
            nested = candidate.get("offers")
            inner = nested if isinstance(nested, list) else [nested]
            real = [item for item in inner if isinstance(item, dict)]
            found.extend(real if real else [candidate])
            continue
        found.append(candidate)
    return found


def _variant(
    owner: dict[str, Any],
    offers: list[dict[str, Any]],
    product: dict[str, Any],
    index: int,
    source_url: str,
    identifiers: Identifiers,
) -> DraftVariant | Omission:
    """One variant, or the reason this whole product cannot be imported.

    A refusal here fails the product rather than dropping the variant. Importing three of a
    product's four variants and saying nothing about the fourth would publish a catalog that
    quietly disagrees with the merchant's own page, and an evaluation run against it would be
    measuring a product that does not exist.
    """
    if len(offers) != 1:
        return Omission(
            source_url,
            "variant_ambiguous",
            "this page publishes several offers for one variant and AgentRank cannot tell which"
            " one is the price",
        )
    offer = offers[0]
    try:
        currency = normalize_currency(offer.get("priceCurrency") or product.get("priceCurrency"))
        amount = _offered_amount(offer, currency)
    except RefusedAmountError as refused:
        return Omission(source_url, refused.reason, refused.detail)
    if amount is None:
        return Omission(
            source_url,
            "price_conflict",
            "this page publishes a price range rather than a price",
        )

    # The offer's own identifier first. An offer that names a SKU is naming the thing being
    # sold, while the product node's SKU is the family it belongs to, and a page publishing both
    # has said which is which. A variant node reached through `hasVariant` carries its SKU on
    # itself and falls through to the second clause.
    raw_sku = (
        _text(offer.get("sku"))
        or _text(owner.get("sku"))
        or _text(offer.get("mpn"))
        or _text(owner.get("mpn"))
        or _text(offer.get("gtin13"))
        or _text(owner.get("gtin13"))
    )
    if raw_sku is None:
        # Nothing positional. The second entry in an array is not "the medium one" unless the page
        # says so, and a SKU derived from an index means the same identifier names a different
        # variant the moment a storefront regenerates its structured data in another order. What is
        # used instead is whatever the merchant published to tell this variant from its siblings.
        distinguishing = _variant_label(owner, product)
        if owner is not product and distinguishing is not None:
            raw_sku = f"{source_url}#{distinguishing}"
        elif owner is product and index == 0:
            raw_sku = _text(product.get("sku")) or f"{source_url}#only"
        else:
            return Omission(
                source_url,
                "variant_ambiguous",
                "this page publishes variants with nothing to tell them apart, so AgentRank"
                " cannot give them stable identities",
            )
    availability, text = _availability(offer, owner)
    quantity = _inventory_level(offer, owner)
    if quantity is not None:
        implied = (
            AvailabilityEvidence.OUT_OF_STOCK if quantity == 0 else AvailabilityEvidence.IN_STOCK
        )
        # Two published facts about one thing that contradict each other. Picking either would
        # be deciding which of a merchant's own statements to believe, which is the decision this
        # importer exists without.
        #
        # An unreadable token counts as a disagreement, not as an absence. A page publishing
        # `PreOrder` beside a count of five has said something about availability that AgentRank
        # cannot represent, and reading the count alone would turn a state it does not model into
        # `IN_STOCK`. A page that published no availability token at all is different: there is
        # nothing to disagree with, and the count says the whole thing on its own.
        unreadable = availability is AvailabilityEvidence.UNKNOWN and text is not None
        if unreadable or (
            availability is not AvailabilityEvidence.UNKNOWN and implied is not availability
        ):
            return Omission(
                source_url,
                "availability_conflict",
                "this page publishes an availability and an inventory level that disagree",
            )
    label = _variant_label(owner, product)
    for value in (label, text):
        if value is not None and instruction_like(value):
            return Omission(
                source_url,
                "instruction_like",
                "this page addresses whatever reads it, so it is not imported",
            )
    return DraftVariant(
        sku=identifiers.sku(raw_sku),
        label=None if label is None else label[:MAX_LABEL_LENGTH],
        price_amount_minor=amount,
        currency=currency,
        availability=(
            availability
            if quantity is None
            else (
                AvailabilityEvidence.OUT_OF_STOCK
                if quantity == 0
                else AvailabilityEvidence.IN_STOCK
            )
        ),
        availability_text=text,
        inventory_quantity=quantity,
    )


def _offered_amount(offer: dict[str, Any], currency: str) -> int | None:
    """One offer's price in minor units, or None when it is a range rather than a price.

    An `AggregateOffer` is a summary and its two figures are compared as amounts rather than as
    spellings. `10` and `10.00` are one price written twice, and refusing that pair as a
    disagreement would exclude a product over a formatting choice; `10` and `20` are two prices and
    there is no basis for picking between them.
    """
    if AGGREGATE_OFFER_TYPE not in _types(offer):
        return minor_units(offer.get("price"), currency)
    low, high = offer.get("lowPrice"), offer.get("highPrice")
    if low is None and high is None:
        return minor_units(offer.get("price"), currency)
    if low is None or high is None:
        # Half a range is not a price. The page states a bound and leaves the other end open.
        raise RefusedAmountError(
            "price_missing", "the page publishes one end of a price range and not a price"
        )
    lowest = minor_units(low, currency)
    return lowest if lowest == minor_units(high, currency) else None


def _variant_label(owner: dict[str, Any], product: dict[str, Any]) -> str | None:
    """What distinguishes one variant, taken from what the merchant published and not inferred.

    A variant's own name when it has one and it differs from the product's, otherwise the option
    values the merchant stated. Position in a list is never used: the second entry in an array is
    not "the medium one" unless the page says so, and a label invented from ordering would be a
    semantic claim dressed as a string.
    """
    if owner is not product:
        name = _text(owner.get("name"))
        if name is not None and name != _text(product.get("name")):
            return name
    for key in ("color", "size", "variesBy", "material", "pattern"):
        value = owner.get(key)
        if isinstance(value, str) and value.strip():
            return collapse(value)
        if isinstance(value, list):
            parts = [collapse(item) for item in value if isinstance(item, str) and item.strip()]
            if parts:
                return " / ".join(parts)
    return None


def _availability(
    offer: dict[str, Any], owner: dict[str, Any]
) -> tuple[AvailabilityEvidence, str | None]:
    """What the page said about stock, and the exact token it said it with.

    The token is kept because the state is a reading of it, and a merchant looking at a draft
    should be able to see the word their page published rather than only AgentRank's summary of
    it. It never becomes a quantity. See `agentrank_api.importer.draft.canonical_document`.

    Being kept is exactly why the caller runs it through the instruction-like guard. It is stored
    in the variant's merchant metadata, which is a source document field, and every other imported
    string that reaches one is checked. Missing it here would leave a draft that carries no
    blocker and that the source schema then refuses at confirmation time, which is the one place
    a merchant can do nothing about it.
    """
    raw = offer.get("availability")
    if raw is None:
        raw = owner.get("availability")
    if raw is None:
        return AvailabilityEvidence.UNKNOWN, None
    text = _text(raw)
    if text is None:
        return AvailabilityEvidence.UNKNOWN, None
    token = text.rsplit("/", 1)[-1].strip().lower()
    bounded = text[:MAX_AVAILABILITY_TEXT]
    if token in IN_STOCK_TOKENS:
        return AvailabilityEvidence.IN_STOCK, bounded
    if token in OUT_OF_STOCK_TOKENS:
        return AvailabilityEvidence.OUT_OF_STOCK, bounded
    return AvailabilityEvidence.UNKNOWN, bounded


def _inventory_level(offer: dict[str, Any], owner: dict[str, Any]) -> int | None:
    """An exact stock count, on the rare page that publishes one, and None on every other.

    schema.org publishes this as a `QuantitativeValue`, and only a whole non-negative `value` is
    read. A fraction of a unit, a range, a string and a negative are all left as None rather than
    interpreted: this is stock, the state beside it already says whether there is any, and a
    number that had to be massaged into shape is not a number a merchant published.

    Read the same way a price is, from the offer first and the product it belongs to second, so
    a variant that states its own count is not given its family's. An offer that publishes the
    key at all answers for itself, readably or not: falling through to the family on a value this
    reader cannot use would give the variant a count its own page contradicted.
    """
    for holder in (offer, owner):
        if "inventoryLevel" not in holder:
            continue
        raw = holder["inventoryLevel"]
        if isinstance(raw, dict):
            raw = raw.get("value")
        if isinstance(raw, bool) or not isinstance(raw, int):
            return None
        return raw if 0 <= raw <= MAX_INVENTORY_QUANTITY else None
    return None


def _metadata_price_conflict(
    reading: PageReading, variants: list[DraftVariant], source_url: str
) -> Omission | None:
    """Whether the page's own metadata contradicts the single price its structured data published.

    Only for a product with one variant. A multi variant product's metadata price is conventionally
    the lowest of them, so comparing would manufacture a conflict out of two statements that agree.
    """
    if len(variants) != 1:
        return None
    published = reading.meta(*PRICE_AMOUNT_TAGS)
    declared = reading.meta(*PRICE_CURRENCY_TAGS)
    if published is None or declared is None:
        return None
    try:
        currency = normalize_currency(declared)
        amount = minor_units(published, currency)
    except RefusedAmountError:
        # Metadata this module would not read as a price cannot contradict one. Saying otherwise
        # would let an unreadable tag veto a price the merchant published properly.
        return None
    if (amount, currency) == (variants[0].price_amount_minor, variants[0].currency):
        return None
    return Omission(
        source_url,
        "price_conflict",
        "this page publishes two different prices for this product, one in its structured data"
        " and one in its page metadata",
        variants[0].sku,
    )


def _from_metadata(
    reading: PageReading,
    source_url: str,
    identifiers: Identifiers,
    nodes: list[dict[str, Any]],
) -> ProductExtraction:
    """A product from the page's own metadata tags, for a page publishing no product node."""
    # Only the two tags that mean "price". A Twitter card's `data1` field is a free form value
    # whose meaning is stated by its own `label1`, so reading it as a price turns a review count,
    # a wattage or a delivery estimate into money. It was read here once and should not have been.
    published = reading.meta(*PRICE_AMOUNT_TAGS)
    declared = reading.meta(*PRICE_CURRENCY_TAGS)
    if published is None:
        return ProductExtraction(
            None,
            Omission(
                source_url,
                "price_missing",
                "this page publishes no machine readable price, so AgentRank cannot import it"
                " without guessing one",
            ),
            (),
        )
    try:
        currency = normalize_currency(declared)
        amount = minor_units(published, currency)
    except RefusedAmountError as refused:
        return ProductExtraction(None, Omission(source_url, refused.reason, refused.detail), ())

    title = reading.meta(*TITLE_TAGS) or _fallback_title(reading)
    if title is None:
        return ProductExtraction(
            None,
            Omission(source_url, "title_missing", "this page publishes no product title"),
            (),
        )
    description, truncated = _bounded(reading.meta(*DESCRIPTION_TAGS), MAX_DESCRIPTION_LENGTH)
    for value in (title, description):
        if value is not None and instruction_like(value):
            return ProductExtraction(
                None,
                Omission(
                    source_url,
                    "instruction_like",
                    "this page addresses whatever reads it, so it is not imported",
                ),
                (),
            )
    raw_sku = reading.meta(*IDENTIFIER_TAGS) or source_url
    availability, text = _metadata_availability(reading)
    if text is not None and instruction_like(text):
        return ProductExtraction(
            None,
            Omission(
                source_url,
                "instruction_like",
                "this page addresses whatever reads it, so it is not imported",
            ),
            (),
        )
    findings = (
        (Finding(source_url, "description_truncated", "the description was cut to fit a source"),)
        if truncated
        else ()
    )
    return ProductExtraction(
        DraftProduct(
            external_id=identifiers.product(raw_sku),
            title=title[:MAX_TITLE_LENGTH],
            description=description,
            category=_category({}, nodes, title) or _bounded_category(reading.meta(*CATEGORY_TAGS)),
            source_url=source_url,
            extraction=ExtractionMethod.PAGE_METADATA,
            variants=(
                DraftVariant(
                    sku=identifiers.sku(raw_sku),
                    label=None,
                    price_amount_minor=amount,
                    currency=currency,
                    availability=availability,
                    availability_text=text,
                ),
            ),
        ),
        None,
        findings,
    )


def _metadata_availability(reading: PageReading) -> tuple[AvailabilityEvidence, str | None]:
    published = reading.meta(*AVAILABILITY_TAGS)
    if published is None:
        return AvailabilityEvidence.UNKNOWN, None
    token = published.rsplit("/", 1)[-1].strip().lower()
    bounded = published[:MAX_AVAILABILITY_TEXT]
    if token in IN_STOCK_TOKENS:
        return AvailabilityEvidence.IN_STOCK, bounded
    if token in OUT_OF_STOCK_TOKENS:
        return AvailabilityEvidence.OUT_OF_STOCK, bounded
    return AvailabilityEvidence.UNKNOWN, bounded


def _external_identity(node: dict[str, Any], source_url: str, identifiers: Identifiers) -> str:
    """What to call this product, from the merchant's own identifier when they published one."""
    raw = (
        _text(node.get("sku"))
        or _text(node.get("productID"))
        or _text(node.get("mpn"))
        or _text(node.get("@id"))
        or source_url
    )
    return identifiers.product(raw)


def _category(node: dict[str, Any], nodes: list[dict[str, Any]], title: str) -> str | None:
    """The category the page published, from the product node or from its breadcrumb trail.

    The breadcrumb is read only as a fallback and only for the last entry that is not the product
    itself, which is the entry a breadcrumb exists to state. Nothing else about the trail is
    interpreted and no depth is assumed.
    """
    published = node.get("category")
    if isinstance(published, str) and published.strip():
        return _bounded_category(published)
    if isinstance(published, list):
        for item in published:
            if isinstance(item, str) and item.strip():
                return _bounded_category(item)
    if isinstance(published, dict):
        name = _text(published.get("name"))
        if name:
            return _bounded_category(name)
    return _breadcrumb_category(nodes, title)


def _breadcrumb_category(nodes: list[dict[str, Any]], title: str) -> str | None:
    for candidate in nodes:
        if BREADCRUMB_TYPE not in _types(candidate):
            continue
        elements = candidate.get("itemListElement")
        if not isinstance(elements, list):
            continue
        names: list[str] = []
        for element in elements:
            if not isinstance(element, dict):
                continue
            name = _text(element.get("name"))
            if name is None:
                item = element.get("item")
                if isinstance(item, dict):
                    name = _text(item.get("name"))
            if name is not None and name != title:
                names.append(name)
        if names:
            return _bounded_category(names[-1])
    return None


def _bounded_category(value: str | None) -> str | None:
    if value is None:
        return None
    collapsed = collapse(value)
    if not collapsed or instruction_like(collapsed):
        return None
    return collapsed[:MAX_CATEGORY_LENGTH]


def _instruction_like_field(node: dict[str, Any], title: str, reading: PageReading) -> str | None:
    """Whether anything this product would import addresses whatever reads it next."""
    for value in (title, _text(node.get("description")), reading.meta(*DESCRIPTION_TAGS)):
        if value is not None and instruction_like(value):
            return "this page addresses whatever reads it, so it is not imported"
    return None


def _fallback_title(reading: PageReading) -> str | None:
    """The page's own name for itself, from the two places every HTML document has one.

    The first `h1` and then `<title>`, and nothing else. Any heading would do at first glance and
    is wrong: `headings` collects h1, h2 and h3 in document order, so a page whose first heading is
    `Related products` would take that as a product title. An `h1` is the document's own name for
    itself in a way an `h2` is not.
    """
    for candidate in (reading.heading, reading.title):
        if candidate is not None and candidate.strip():
            return collapse(candidate)
    return None


def _bounded(value: str | None, limit: int) -> tuple[str | None, bool]:
    if value is None:
        return None, False
    collapsed = collapse(value)
    if not collapsed:
        return None, False
    if len(collapsed) <= limit:
        return collapsed, False
    return collapsed[:limit].rstrip(), True


def _text(value: Any) -> str | None:
    """One published value as a bounded single line string, or None if it is not text.

    A schema.org value is frequently a nested `Thing` rather than a string, and a page that puts
    an object where a name belongs has not published a name this module will flatten into one.
    """
    if isinstance(value, str):
        collapsed = collapse(value)
        return collapsed or None
    if isinstance(value, int | Decimal) and not isinstance(value, bool):
        return str(value)
    return None


def extract_policy(reading: PageReading, *, source_url: str, name: str) -> PolicyExtraction:
    """One policy page as bounded merchant prose.

    The whole visible text rather than a section of it, because deciding which paragraph of a
    returns page is "the policy" is exactly the kind of judgement this importer does not make.
    What the text means is the compiler's question, and it reads the source document rather than
    the page.
    """
    text = reading.text.strip()
    if not text:
        return PolicyExtraction(
            None,
            Omission(source_url, "policy_empty", "this page has no readable text", name),
            (),
        )
    if instruction_like(text):
        return PolicyExtraction(
            None,
            Omission(
                source_url,
                "instruction_like",
                "this policy page addresses whatever reads it, so it is not imported",
                name,
            ),
            (),
        )
    body, truncated = bounded_policy_body(text)
    findings = (
        (
            Finding(
                source_url,
                "policy_truncated",
                "this policy page is longer than a source document holds and was cut",
                name,
            ),
        )
        if truncated
        else ()
    )
    return PolicyExtraction(
        DraftPolicy(name=name, body=body, source_url=source_url, truncated=truncated),
        None,
        findings,
    )
