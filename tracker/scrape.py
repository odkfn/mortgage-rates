"""Fetch and parse the first direct mortgage rate list.

The page layout is not contractually stable, so parsing runs three independent
strategies and keeps the first one that yields products:

1. ``parse_embedded_json``  - rate data serialised into a <script> tag
   (``__NEXT_DATA__``, ``application/json`` blocks, ``window.X = {...}``).
2. ``parse_tables``         - a real <table> whose headers name rate columns.
3. ``parse_cards``          - a repeated block of markup each containing one
   "x.xx%" figure, optionally pinned down by CSS selectors in config.json.

Each strategy is pure ``html -> list[Product]``, so a saved copy of the page can
be replayed offline while tuning (``python -m tracker discover --html page.html``).
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass, asdict
from typing import Any, Iterable, Iterator

import requests
from bs4 import BeautifulSoup, Tag

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0 Safari/537.36"
)
REQUEST_TIMEOUT = 30

PERCENT_RE = re.compile(r"(\d{1,2}(?:\.\d{1,3})?)\s*%")
MONEY_RE = re.compile(r"£\s*([\d,]+(?:\.\d{2})?)")

# Header/key keyword sets, most specific first.
RATE_KEYS = ("initial rate", "initial interest", "interest rate", "rate")
APRC_KEYS = ("aprc", "apr", "overall cost")
# Booking fee first: it is the fee that separates a Fee Saver from a Standard
# product (£0 against £490), where the arrangement fee is £0 on both.
FEE_KEYS = ("booking fee", "product fee", "fee")
LTV_KEYS = ("loan to value", "ltv")
NAME_KEYS = ("product", "mortgage", "deal", "type", "term")

NOISE_RATE_KEYS = ("follow", "revert", "svr", "standard variable")


@dataclass
class Product:
    """One mortgage product observed on one day."""

    product_id: str
    name: str
    initial_rate: float
    aprc: float | None = None
    fee: float | None = None
    ltv: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class ParseError(RuntimeError):
    """Raised when no strategy could find any products."""


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def clean_text(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or ""))
    return re.sub(r"\s+", " ", text).strip()


def slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", clean_text(name).lower()).strip("-")
    return slug or "product"


def make_id(name: str, ltv: str | None) -> str:
    """A stable id. The LTV is folded in, since the same product name is
    normally listed once per LTV band."""
    slug = slugify(name)
    digits = re.search(r"\d{2,3}", ltv or "")
    if digits and digits.group(0) not in slug:
        slug = f"{slug}-{digits.group(0)}-ltv"
    return slug


def to_percent(value: Any) -> float | None:
    """Pull a percentage out of a string or number. ``None`` if implausible."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        number = float(value)
    else:
        match = PERCENT_RE.search(clean_text(value))
        if not match:
            return None
        number = float(match.group(1))
    # Mortgage rates live in single digits; anything else is a page artefact.
    return number if 0 < number < 25 else None


def to_money(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    text = clean_text(value)
    match = MONEY_RE.search(text)
    if match:
        return float(match.group(1).replace(",", ""))
    if re.search(r"\b(no|nil|none|free|without)\b(\s+\w+)?\s*fee|^\s*(no|nil|none|free)\s*$", text, re.I):
        return 0.0
    return None


def _matches(haystack: str, keys: Iterable[str]) -> bool:
    return any(key in haystack for key in keys)


def _dedupe(products: list[Product]) -> list[Product]:
    """Drop products scraped twice; suffix ids that genuinely collide."""
    out: list[Product] = []
    counts: dict[str, int] = {}
    for product in products:
        base = product.product_id
        if any(p.product_id == base and p.initial_rate == product.initial_rate for p in out):
            continue  # the same product picked up twice
        counts[base] = counts.get(base, 0) + 1
        if counts[base] > 1:
            product.product_id = f"{base}-{counts[base]}"
        out.append(product)
    return out


# --------------------------------------------------------------------------
# strategy 1: embedded JSON
# --------------------------------------------------------------------------


def _iter_json_blobs(soup: BeautifulSoup) -> Iterator[Any]:
    for script in soup.find_all("script"):
        raw = script.string or script.get_text() or ""
        raw = raw.strip()
        if not raw or "%" not in raw and "rate" not in raw.lower():
            continue
        candidates = [raw]
        # window.__DATA__ = {...};  /  var x = {...}
        assignment = re.search(r"=\s*(\{.*\}|\[.*\])\s*;?\s*$", raw, re.S)
        if assignment:
            candidates.append(assignment.group(1))
        for candidate in candidates:
            try:
                yield json.loads(candidate)
                break
            except (ValueError, RecursionError):
                continue


def _walk(node: Any) -> Iterator[dict]:
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk(value)
    elif isinstance(node, list):
        for value in node:
            yield from _walk(value)


def _product_from_mapping(mapping: dict) -> Product | None:
    rate = aprc = fee = None
    ltv = name = None
    for key, value in mapping.items():
        if not isinstance(value, (str, int, float)) or isinstance(value, bool):
            continue
        lkey = clean_text(key).lower()
        if _matches(lkey, NOISE_RATE_KEYS):
            continue
        if rate is None and _matches(lkey, RATE_KEYS) and not _matches(lkey, APRC_KEYS):
            rate = to_percent(value)
        elif aprc is None and _matches(lkey, APRC_KEYS):
            aprc = to_percent(value)
        elif fee is None and _matches(lkey, FEE_KEYS):
            fee = to_money(value)
        elif ltv is None and _matches(lkey, LTV_KEYS):
            ltv = clean_text(value) or None
        elif name is None and _matches(lkey, NAME_KEYS):
            name = clean_text(value) or None
    if rate is None:
        return None
    label = name or " ".join(filter(None, [ltv, "mortgage"]))
    return Product(make_id(label, ltv), label, rate, aprc, fee, ltv)


def parse_embedded_json(html: str) -> list[Product]:
    soup = BeautifulSoup(html, "html.parser")
    products: list[Product] = []
    for blob in _iter_json_blobs(soup):
        for mapping in _walk(blob):
            product = _product_from_mapping(mapping)
            if product:
                products.append(product)
        if products:
            break
    return _dedupe(products)


# --------------------------------------------------------------------------
# strategy 2: tables
# --------------------------------------------------------------------------


# Markup that carries no value of its own: explanatory tooltips, the label
# repeated for the mobile layout, screen-reader text and sort buttons.
CELL_NOISE_SELECTORS = (
    ".tooltip-container, .tooltip__content, [role=tooltip], .mobile-th-text, "
    ".visuallyhidden, .visually-hidden, .sr-only, .screen-reader-text, "
    "[hidden], button, script, style"
)
TOOLTIP_RE = re.compile(r"\bmore info\b", re.I)


def cell_text(cell: Tag) -> str:
    """The text of a table cell as a sighted reader sees it.

    Rate tables repeat the column's own label inside every cell and hang a
    "More info" explanation off it, and the product cell carries a
    screen-reader span restating the LTV and the rate. Left in, they mislead
    every value parser: the hidden span alone makes a product name look like a
    percentage, and a stray "product" in an explanation makes the wrong column
    look like the product column.
    """
    clone = BeautifulSoup(str(cell), "html.parser")
    for junk in clone.select(CELL_NOISE_SELECTORS):
        junk.decompose()
    # Fallback for pages that mark tooltips up some other way.
    return clean_text(TOOLTIP_RE.split(clone.get_text(" "), maxsplit=1)[0])


def header_text(cell: Tag) -> str:
    """The name of a column, preferring an explicit one over the rendered text."""
    return clean_text(cell.get("data-col-title") or cell_text(cell))


def _header_index(headers: list[str], keys: Iterable[str]) -> int | None:
    for key in keys:
        for index, header in enumerate(headers):
            if key in header and not _matches(header, NOISE_RATE_KEYS):
                return index
    return None


def _context_ltv(node: Tag) -> str | None:
    """Find an LTV that applies to ``node`` from a heading or caption above it.

    Rate pages commonly group products under "70% LTV" headings instead of
    repeating the LTV on every row.
    """
    current: Tag | None = node
    for _ in range(6):
        if current is None:
            break
        for sibling in current.find_previous_siblings(limit=8):
            match = LTV_CONTEXT_RE.search(clean_text(sibling.get_text(" ")))
            if match:
                return f"{match.group(1)}%"
        current = current.parent
    return None


def parse_tables(html: str) -> list[Product]:
    soup = BeautifulSoup(html, "html.parser")
    products: list[Product] = []

    # Scan every table: a rate page often uses one table per LTV band.
    for table in soup.find_all("table"):
        header_cells = table.find_all("th")
        headers = [header_text(cell).lower() for cell in header_cells]
        rate_col = _header_index(headers, RATE_KEYS)
        if rate_col is None:
            continue
        aprc_col = _header_index(headers, APRC_KEYS)
        fee_col = _header_index(headers, FEE_KEYS)
        ltv_col = _header_index(headers, LTV_KEYS)
        name_col = _header_index(headers, NAME_KEYS)
        section_ltv = _context_ltv(table) if ltv_col is None else None

        for row in table.find_all("tr"):
            cells = row.find_all(["td", "th"])
            if len(cells) <= rate_col:
                continue
            values = [cell_text(cell) for cell in cells]
            rate = to_percent(values[rate_col])
            if rate is None:
                continue  # header row, or a row without a usable rate

            def at(col: int | None) -> str | None:
                return values[col] if col is not None and col < len(values) else None

            ltv = at(ltv_col) or section_ltv
            name = at(name_col)
            if not name or to_percent(name) is not None:
                # No name column - build one from the row's descriptive cells.
                descriptive = [
                    v for i, v in enumerate(values)
                    if i not in {rate_col, aprc_col, fee_col} and v and not PERCENT_RE.search(v)
                ]
                name = " ".join(descriptive[:2]) or f"{ltv or ''} mortgage".strip()
            products.append(
                Product(make_id(name, ltv), name, rate, to_percent(at(aprc_col)), to_money(at(fee_col)), ltv)
            )

    return _dedupe(products)


# --------------------------------------------------------------------------
# strategy 3: repeated cards
# --------------------------------------------------------------------------


HEADINGS = ["h1", "h2", "h3", "h4", "h5", "h6", "strong", "dt"]
LTV_CONTEXT_RE = re.compile(r"(\d{2,3})\s*%\s*(?:ltv|loan[- ]to[- ]value)", re.I)


def _pick_rate(text: str) -> float | None:
    """Choose the headline rate from a block that may also quote an LTV."""
    ltv_figures = set(LTV_CONTEXT_RE.findall(text))
    candidates = [m for m in PERCENT_RE.findall(text) if m not in ltv_figures]
    # A rate is quoted with decimals ("4.09%"); an LTV usually is not ("60%").
    decimal = [c for c in candidates if "." in c]
    for value in decimal + candidates:
        rate = to_percent(value + "%")
        if rate is not None:
            return rate
    return None


def _is_card(element: Tag) -> bool:
    text = clean_text(element.get_text(" "))
    if len(text) > 400 or not PERCENT_RE.search(text):
        return False
    return element.find(HEADINGS) is not None


def _card_candidates(soup: BeautifulSoup) -> list[Tag]:
    """Innermost blocks that pair a heading with a percentage figure."""
    cards = [e for e in soup.find_all(True) if _is_card(e)]
    # Drop any block that wraps another candidate - keep the leaves.
    return [c for c in cards if not any(other is not c and other in c.descendants for other in cards)]


def parse_cards(html: str, selectors: dict[str, str | None] | None = None) -> list[Product]:
    soup = BeautifulSoup(html, "html.parser")
    selectors = selectors or {}

    if selectors.get("card"):
        cards = soup.select(selectors["card"])
    else:
        cards = _card_candidates(soup)

    products: list[Product] = []
    for card in cards:
        text = clean_text(card.get_text(" "))
        if selectors.get("rate"):
            found = card.select_one(selectors["rate"])
            rate = to_percent(found.get_text()) if found else None
        else:
            rate = _pick_rate(text)
        if rate is None:
            continue

        name = None
        if selectors.get("name"):
            found = card.select_one(selectors["name"])
            name = clean_text(found.get_text()) if found else None
        if not name:
            heading = card.find(HEADINGS)
            name = clean_text(heading.get_text()) if heading else None
        if not name:
            # Fall back to the card's text with the numbers stripped out.
            name = clean_text(PERCENT_RE.sub("", MONEY_RE.sub("", text)))[:60]
        if not name:
            continue

        ltv_match = LTV_CONTEXT_RE.search(text)
        ltv = f"{ltv_match.group(1)}%" if ltv_match else _context_ltv(card)
        products.append(Product(make_id(name, ltv), name, rate, None, to_money(text), ltv))
    return _dedupe(products)


# --------------------------------------------------------------------------
# public API
# --------------------------------------------------------------------------

STRATEGIES = ("embedded_json", "tables", "cards")


def parse_products(html: str, selectors: dict[str, str | None] | None = None) -> tuple[list[Product], str]:
    """Return ``(products, strategy_name)`` from the first strategy that works."""
    runners = {
        "embedded_json": lambda: parse_embedded_json(html),
        "tables": lambda: parse_tables(html),
        "cards": lambda: parse_cards(html, selectors),
    }
    for name in STRATEGIES:
        try:
            products = runners[name]()
        except Exception as exc:  # a broken strategy must not sink the others
            print(f"  strategy {name!r} raised {type(exc).__name__}: {exc}")
            continue
        if products:
            return products, name
    raise ParseError(
        "No mortgage products found. The page layout has probably changed - save "
        "the HTML and replay it with `python -m tracker discover --html page.html`."
    )


def fetch_html(url: str, render: bool = False, form: dict[str, Any] | None = None) -> str:
    """Fetch the page, optionally rendering it in a headless browser first."""
    if render or form:
        return _fetch_rendered(url, form)
    response = requests.get(
        url,
        timeout=REQUEST_TIMEOUT,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "en-GB,en;q=0.9",
        },
    )
    response.raise_for_status()
    return response.text


def _fetch_rendered(url: str, form: dict[str, Any] | None = None) -> str:
    """Render the page in a headless browser, optionally submitting a filter.

    first direct lists no rates until the "Find a rate" filter is applied, and
    it submits as a POST, so there is no query string that reaches the results
    and no way to do this without a browser.
    """
    from playwright.sync_api import sync_playwright

    choices = (form or {}).get("select") or {}
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        try:
            page = browser.new_page(user_agent=USER_AGENT, locale="en-GB")
            page.goto(url, wait_until="networkidle", timeout=60_000)
            if not choices:
                return page.content()

            # A cookie banner overlays the form and swallows the clicks.
            for selector in (form or {}).get("dismiss") or []:
                element = page.query_selector(selector)
                if element:
                    try:
                        element.click(timeout=5_000)
                    except Exception:
                        pass  # already gone, or harmless where it is

            for selector, value in choices.items():
                page.select_option(selector, value)
            submit = (form or {}).get("submit")
            if submit:
                page.click(submit)
            page.wait_for_load_state("networkidle", timeout=60_000)
            return page.content()
        finally:
            browser.close()


def scrape(
    url: str,
    selectors: dict[str, str | None] | None = None,
    form: dict[str, Any] | None = None,
) -> tuple[list[Product], str]:
    """Fetch and parse, falling back to a rendered browser fetch if needed."""
    if (form or {}).get("select"):
        # The rates only exist after the filter is submitted, so a plain fetch
        # cannot succeed and is not worth the request.
        html = fetch_html(url, form=form)
        products, strategy = parse_products(html, selectors)
        return products, f"{strategy} (filtered)"
    try:
        html = fetch_html(url)
        return parse_products(html, selectors)
    except (requests.RequestException, ParseError) as exc:
        print(f"Plain fetch/parse failed ({type(exc).__name__}: {exc}); retrying with a browser.")
        html = fetch_html(url, render=True)
        products, strategy = parse_products(html, selectors)
        return products, f"{strategy} (rendered)"
