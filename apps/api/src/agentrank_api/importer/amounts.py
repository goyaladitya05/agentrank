"""Turning a published price into AgentRank money, or refusing to.

Money is the field where a plausible guess is most expensive. A wrong title is visible to anybody
reading the draft; a price that is a hundred times too small because a currency has no minor unit,
or a number read as 1.234 when the merchant wrote one thousand two hundred and thirty four, is a
number that looks completely ordinary in a review and then decides what an evaluation measures.

So every rule here refuses rather than resolves:

```text
no currency          a bare number is not an amount and does not become one
unknown currency     a code with no published minor unit is not convertible, so it is not converted
grouped digits       "1,234.56" is one thousand in one place and one point two in another
extra decimals       0.005 of a currency with two of them is a number this system cannot hold
several prices       two figures that disagree are not a price to pick between
```

There is exactly one inference in this module and it is stated rather than hidden: two currency
symbols, the rupee sign and the euro sign, each denote exactly one currency in the world, so a
page whose declared currency field holds one has published a currency. Every other symbol,
including the dollar sign and the pound sign, denotes several and is refused. Nothing here ever
consults a locale, an address, a domain suffix or a merchant profile. If the page does not say it,
AgentRank does not know it.

Amounts are `Decimal` from the moment they leave the JSON parser, which is why the parser is
called with `parse_float=Decimal`. A float would already have lost the question this module exists
to answer by the time any of these rules ran.
"""

import re
from decimal import (
    Decimal,
    DecimalException,
    Inexact,
    InvalidOperation,
    Overflow,
    Rounded,
    localcontext,
)

from agentrank_api.money import CURRENCY_PATTERN

# The minor unit exponent of every currency this importer will convert. A table rather than a
# format check, because converting a published price to minor units is arithmetic that needs the
# exponent, and there is no way to derive one from a three letter code.
#
# Deliberately not a claim about which currencies exist. `agentrank_api.money` is right that
# AgentRank does not decide that, and a source document may carry any ISO 4217 code. This is
# narrower: it is the set of codes this importer can convert a decimal figure into minor units
# for. A merchant publishing anything else is told that, and can write the source document
# themselves with the minor units they already know.
ZERO_DECIMAL = frozenset(
    {
        "BIF",
        "CLP",
        "DJF",
        "GNF",
        "ISK",
        "JPY",
        "KMF",
        "KRW",
        "PYG",
        "RWF",
        "UGX",
        "UYI",
        "VND",
        "VUV",
        "XAF",
        "XOF",
        "XPF",
    }
)

THREE_DECIMAL = frozenset({"BHD", "IQD", "JOD", "KWD", "LYD", "OMR", "TND"})

TWO_DECIMAL = frozenset(
    {
        "AED",
        "ARS",
        "AUD",
        "BDT",
        "BGN",
        "BRL",
        "CAD",
        "CHF",
        "CNY",
        "COP",
        "CZK",
        "DKK",
        "EGP",
        "EUR",
        "GBP",
        "HKD",
        "HRK",
        "HUF",
        "IDR",
        "ILS",
        "INR",
        "KES",
        "LKR",
        "MAD",
        "MXN",
        "MYR",
        "NGN",
        "NOK",
        "NZD",
        "PEN",
        "PHP",
        "PKR",
        "PLN",
        "QAR",
        "RON",
        "RSD",
        "RUB",
        "SAR",
        "SEK",
        "SGD",
        "THB",
        "TRY",
        "TWD",
        "TZS",
        "UAH",
        "USD",
        "VES",
        "ZAR",
    }
)

# The only two currency symbols that name one currency each. Applied to a field where the merchant
# said "this is the currency" and never to running text.
UNAMBIGUOUS_SYMBOLS = {"₹": "INR", "€": "EUR"}

# A published price, as schema.org asks for one: digits, optionally a decimal point, nothing else.
# In particular no grouping separator, because which of the comma and the point groups and which
# one divides is a locale question and a page that needs one answered has not published a number.
_PLAIN_DECIMAL = re.compile(r"^\d{1,15}(\.\d{1,8})?$")

_CURRENCY_CODE = re.compile(CURRENCY_PATTERN)

MAX_PRICE_AMOUNT_MINOR = 10**12

# Enough significant digits for any figure `_PLAIN_DECIMAL` admits, shifted by any exponent this
# module knows. Fifteen digits plus eight decimals plus three places of shift is twenty six.
_ARITHMETIC_PRECISION = 40


class RefusedAmountError(Exception):
    """A published figure this module will not turn into money, and the reason.

    Carries a reason code the console renders beside the product it excluded, so a merchant reads
    "the price is not a plain number" rather than a stack trace or, worse, a price.
    """

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(detail)
        self.reason = reason
        self.detail = detail


def exponent(currency: str) -> int | None:
    """How many minor units one unit of this currency divides into, or None if unknown."""
    if currency in ZERO_DECIMAL:
        return 0
    if currency in THREE_DECIMAL:
        return 3
    if currency in TWO_DECIMAL:
        return 2
    return None


def normalize_currency(published: object) -> str:
    """The ISO 4217 code a page published, refused unless it published one.

    Case is normalized because a page writing `inr` has published INR, and nothing else is.
    """
    if published is None:
        raise RefusedAmountError(
            "currency_missing", "the page publishes no currency for this price"
        )
    text = str(published).strip()
    if not text:
        raise RefusedAmountError(
            "currency_missing", "the page publishes no currency for this price"
        )
    symbol = UNAMBIGUOUS_SYMBOLS.get(text)
    if symbol is not None:
        return symbol
    upper = text.upper()
    if _CURRENCY_CODE.fullmatch(upper) is None:
        raise RefusedAmountError(
            "currency_ambiguous",
            "the page states a currency AgentRank cannot read as an ISO 4217 code",
        )
    if exponent(upper) is None:
        raise RefusedAmountError(
            "currency_unsupported",
            "AgentRank does not know how many minor units that currency divides into",
        )
    return upper


def minor_units(published: object, currency: str) -> int:
    """One published figure as an integer count of minor units of one known currency.

    Exact by construction, and the arithmetic is done in a context that refuses to be inexact
    rather than in the default one. `Decimal.scaleb` under the default context rounds at
    twenty eight significant digits and raises on overflow, so a figure with more precision than
    that would be quietly rounded into a whole number and imported, and a figure with a huge
    exponent would raise a decimal error nothing catches. Both are page content, so both have to
    be refusals rather than surprises.
    """
    places = exponent(currency)
    if places is None:  # pragma: no cover - normalize_currency refuses first
        raise RefusedAmountError("currency_unsupported", "that currency has no known minor unit")
    figure = _decimal(published)
    if figure < 0:
        raise RefusedAmountError("price_negative", "the page publishes a negative price")
    try:
        with localcontext() as context:
            context.prec = _ARITHMETIC_PRECISION
            context.traps[Inexact] = True
            context.traps[Rounded] = True
            context.traps[Overflow] = True
            shifted = figure.scaleb(places)
    except DecimalException as error:
        raise RefusedAmountError(
            "price_malformed", "the page publishes a price AgentRank cannot convert exactly"
        ) from error
    if shifted != shifted.to_integral_value():
        raise RefusedAmountError(
            "price_precision",
            "the price states more decimal places than that currency has minor units",
        )
    amount = int(shifted)
    if amount > MAX_PRICE_AMOUNT_MINOR:
        raise RefusedAmountError("price_too_large", "the price is larger than AgentRank will store")
    return amount


def _decimal(published: object) -> Decimal:
    """A published price as an exact decimal, refused unless it is a plain number.

    A JSON number reaches this as `int` or, because the structured data parser is constructed with
    `parse_float=Decimal`, as `Decimal`. A JSON string reaches it as text and has to look like a
    number with nothing else in it. Everything a merchant writes around a figure, a symbol, a
    thousands separator, a range, a word, makes the figure something this refuses to read.
    """
    if isinstance(published, bool):
        raise RefusedAmountError("price_malformed", "the page publishes no readable price")
    if isinstance(published, int):
        return Decimal(published)
    if isinstance(published, Decimal):
        return published
    if isinstance(published, float):  # pragma: no cover - the parser never produces one
        raise RefusedAmountError("price_malformed", "the page publishes no readable price")
    text = str(published).strip() if published is not None else ""
    if not text:
        raise RefusedAmountError("price_missing", "the page publishes no price for this product")
    if _PLAIN_DECIMAL.fullmatch(text) is None:
        raise RefusedAmountError(
            "price_malformed",
            "the price is not a plain number, so AgentRank cannot read it without guessing",
        )
    try:
        return Decimal(text)
    except InvalidOperation as error:  # pragma: no cover - the pattern already refused these
        raise RefusedAmountError(
            "price_malformed", "the page publishes no readable price"
        ) from error
