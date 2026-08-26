"""Reading one merchant page into the few bounded things AgentRank is willing to look at.

A storefront page is markup written for a browser. This turns one into a small, flat record: the
structured data blocks the merchant published, the metadata tags they published, the title, and
the visible text with everything that is not text removed. Nothing here decides what any of it
means. That is `extraction`, which sees this record and never sees markup.

The separation is the point. Parsing untrusted markup and deciding commerce facts are two
different risks, and a module that did both would be a module where a parser detail could become
a price. Here a parser detail can at most produce a wrong string, which the next layer then
refuses to turn into a fact because it does not match a shape.

Three things this deliberately is not:

```text
not a selector engine    no merchant specific CSS path, no "the price is the third span"
not a renderer           no script runs, no style is applied, no layout is computed
not a link follower      hrefs are collected to be reported and never to be fetched
```

The visible-text pass exists for policy pages, where the merchant's own prose is the evidence.
It is not used to find a price. Distinguishing a product's price from a cross-sell's price, a
struck-through price and a shipping threshold in running text is not something a deterministic
reader can do, and a reader that guessed would put a wrong number into a merchant's source
history without anybody being able to see why.

Everything accumulated here is bounded while it accumulates rather than truncated afterwards, so
a page designed to be expensive to read costs the bound and not the page.
"""

import re
from dataclasses import dataclass
from html.parser import HTMLParser

# Elements whose content is not merchant prose. Their text is dropped entirely rather than
# collected and filtered later: a stylesheet or a script body is never evidence about a product,
# and the surest way for it never to become one is for it never to enter the text at all.
# `head` is deliberately not among them. Suppression now covers metadata and headings as well as
# text, and every tag this module reads is in the head, so suppressing it would make the reader
# see nothing. It contributes no text either way: its script and style children are suppressed by
# name, and its title is captured rather than appended.
NON_TEXT_ELEMENTS = frozenset(
    {"script", "style", "template", "noscript", "svg", "canvas", "iframe"}
)

# Elements that separate words. Without this, "Free returns</p><p>within 30 days" reads as one
# run-together word and a bounded excerpt of it is unreadable.
BREAKING_ELEMENTS = frozenset(
    {
        "p",
        "br",
        "div",
        "li",
        "tr",
        "td",
        "th",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "section",
        "article",
        "header",
        "footer",
        "nav",
        "ul",
        "ol",
        "dl",
        "dt",
        "dd",
        "table",
        "blockquote",
        "pre",
    }
)

STRUCTURED_DATA_TYPE = "application/ld+json"

# How much of a page this reader will hold. Each bound is reached by an ordinary page only if the
# page is extraordinary, and each is what a page built to be expensive costs instead of the page.
MAX_TEXT_CHARACTERS = 200_000
MAX_STRUCTURED_BLOCKS = 24
MAX_STRUCTURED_BLOCK_CHARACTERS = 256 * 1024
MAX_METADATA_ENTRIES = 200
MAX_METADATA_VALUE_CHARACTERS = 2000
MAX_LINKS = 500
MAX_HEADINGS = 32
MAX_HEADING_CHARACTERS = 500

_WHITESPACE = re.compile(r"\s+")

_HEADING_ELEMENTS = frozenset({"h1", "h2", "h3"})


def collapse(text: str) -> str:
    """One string with every run of whitespace reduced to a single space, and trimmed.

    Shared rather than repeated because two callers comparing strings that went through
    different whitespace rules would find differences that are not differences, and one of those
    callers decides whether a re-import says anything new.
    """
    return _WHITESPACE.sub(" ", text).strip()


@dataclass(frozen=True, slots=True)
class PageReading:
    """Everything AgentRank looked at on one merchant page.

    Flat and small on purpose. A caller can render every field of this beside the URL it came
    from, which is what makes an import inspectable rather than something a merchant is asked to
    trust.
    """

    title: str | None
    heading: str | None
    headings: tuple[str, ...]
    metadata: dict[str, str]
    structured_blocks: tuple[str, ...]
    text: str
    links: tuple[str, ...]
    truncated: bool

    def meta(self, *names: str) -> str | None:
        """The first of several metadata names that this page actually published.

        Ordered rather than merged, because the caller's order is a precedence decision:
        `product:price:amount` and `og:price:amount` mean the same thing and a page carrying both
        should be read the way the caller said, not the way a dictionary happened to iterate.
        """
        for name in names:
            value = self.metadata.get(name)
            if value is not None and value.strip():
                return value.strip()
        return None


class _Reader(HTMLParser):
    """The one pass over a page's markup.

    `convert_charrefs` is on, so entities arrive as the characters they name and no caller has to
    decode `&amp;` a second time. Errors are not raised: `html.parser` is lenient by design, and a
    storefront with unclosed tags is an ordinary storefront rather than an import to refuse.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title: str | None = None
        self.heading: str | None = None
        self.headings: list[str] = []
        self.metadata: dict[str, str] = {}
        self.structured_blocks: list[str] = []
        self.links: list[str] = []
        self.truncated = False
        self._text: list[str] = []
        self._length = 0
        self._suppressed: list[str] = []
        self._capturing: str | None = None
        self._captured: list[str] = []
        self._captured_length = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        name = tag.lower()
        attributes = {key.lower(): (value or "") for key, value in attrs}
        if name == "script":
            # Only a structured data block is captured. Every other script is suppressed with
            # the rest of the non-text elements, so no script body reaches the text.
            if attributes.get("type", "").strip().lower() == STRUCTURED_DATA_TYPE:
                self._begin_capture("script")
            else:
                self._suppressed.append(name)
            return
        if name in NON_TEXT_ELEMENTS:
            self._suppressed.append(name)
            return
        # Everything below is inert while suppressed. A `<template>`, a `<noscript>` or an `<svg>`
        # is markup a browser does not render, so a `<meta>` or an `<h1>` inside one is not
        # something the page published: it is a placeholder, a fallback or an icon label. Reading
        # them was a real defect, because metadata is first wins and a template's placeholder
        # price therefore beat the merchant's real one.
        if self._suppressed:
            return
        if name == "title":
            self._begin_capture("title")
            return
        if name in _HEADING_ELEMENTS:
            self._begin_capture(name)
            return
        if name == "meta":
            self._record_meta(attributes)
            return
        if name == "a":
            self._record_link(attributes)
        if name in BREAKING_ELEMENTS:
            self._append(" ")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        name = tag.lower()
        attributes = {key.lower(): (value or "") for key, value in attrs}
        if self._suppressed:
            return
        if name == "meta":
            self._record_meta(attributes)
        elif name == "a":
            self._record_link(attributes)
        elif name in BREAKING_ELEMENTS:
            self._append(" ")

    def handle_endtag(self, tag: str) -> None:
        name = tag.lower()
        if self._capturing == name:
            self._end_capture(name)
            return
        if self._suppressed and self._suppressed[-1] == name:
            self._suppressed.pop()
            return
        if name in NON_TEXT_ELEMENTS and name in self._suppressed:
            # An unclosed inner element left the stack out of step. Unwinding to this tag is what
            # a browser does and keeps a stray `</div>` inside a `<script>` from ending
            # suppression early, which would put a script body into the text.
            while self._suppressed and self._suppressed.pop() != name:
                continue
            return
        if name in BREAKING_ELEMENTS:
            self._append(" ")

    def handle_data(self, data: str) -> None:
        if self._capturing is not None:
            self._capture(data)
            return
        if self._suppressed:
            return
        self._append(data)

    def _begin_capture(self, name: str) -> None:
        # A nested capture cannot happen in valid markup and can in a malformed page. The outer
        # one wins, because abandoning it would lose the text already collected for it.
        if self._capturing is None:
            self._capturing = name
            self._captured = []
            self._captured_length = 0

    def _capture(self, data: str) -> None:
        # A running count rather than a sum over the accumulated pieces. `handle_data` is called
        # once per chunk the parser produces, so re-summing would be quadratic in the number of
        # chunks, which is a property of the page rather than of its size.
        if self._captured_length > MAX_STRUCTURED_BLOCK_CHARACTERS:
            self.truncated = True
            return
        self._captured.append(data)
        self._captured_length += len(data)

    def _end_capture(self, name: str) -> None:
        content = "".join(self._captured)
        self._capturing = None
        self._captured = []
        self._captured_length = 0
        if name == "script":
            if len(self.structured_blocks) < MAX_STRUCTURED_BLOCKS:
                self.structured_blocks.append(content)
            else:
                self.truncated = True
            return
        collapsed = collapse(content)
        if not collapsed:
            return
        if name == "title":
            if self.title is None:
                self.title = collapsed[:MAX_HEADING_CHARACTERS]
            return
        if name == "h1" and self.heading is None:
            self.heading = collapsed[:MAX_HEADING_CHARACTERS]
        if len(self.headings) < MAX_HEADINGS:
            self.headings.append(collapsed[:MAX_HEADING_CHARACTERS])
        # A heading is part of the page's prose as well as being a heading, so it goes into the
        # text too. A policy page whose entire content is headings should not read as empty.
        self._append(f" {collapsed} ")

    def _record_meta(self, attributes: dict[str, str]) -> None:
        name = attributes.get("name") or attributes.get("property") or attributes.get("itemprop")
        content = attributes.get("content")
        if not name or content is None:
            return
        key = collapse(name).lower()
        if not key or len(self.metadata) >= MAX_METADATA_ENTRIES:
            return
        # First wins. A page repeating one tag is stating it once as far as this is concerned,
        # and taking the last would make the value depend on how far the reader got.
        self.metadata.setdefault(key, collapse(content)[:MAX_METADATA_VALUE_CHARACTERS])

    def _record_link(self, attributes: dict[str, str]) -> None:
        href = attributes.get("href", "").strip()
        if href and len(self.links) < MAX_LINKS and href not in self.links:
            self.links.append(href[:MAX_METADATA_VALUE_CHARACTERS])

    def _append(self, data: str) -> None:
        if self._length >= MAX_TEXT_CHARACTERS:
            self.truncated = True
            return
        self._text.append(data)
        self._length += len(data)

    def text(self) -> str:
        return collapse("".join(self._text))


def read_page(markup: str) -> PageReading:
    """One merchant page, as the bounded record everything downstream works from."""
    reader = _Reader()
    reader.feed(markup)
    reader.close()
    return PageReading(
        title=reader.title,
        heading=reader.heading,
        headings=tuple(reader.headings),
        metadata=dict(reader.metadata),
        structured_blocks=tuple(reader.structured_blocks),
        text=reader.text(),
        links=tuple(reader.links),
        truncated=reader.truncated,
    )
