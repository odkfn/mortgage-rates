"""Pick the wanted products out of everything the page lists.

The rate page carries dozens of products across every LTV band, so selection is
by *criteria* (LTV, term, fee-saver vs standard, fixed vs tracker) rather than by
hard-coded product ids: the ids on the page can change wording without the
underlying product changing.

A rule matches a product when every criterion it states is satisfied. Criteria
left out of a rule are not checked.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .scrape import LTV_CONTEXT_RE, Product, clean_text

TERM_RE = re.compile(r"(\d{1,2})\s*(?:-|\s)?\s*(?:year|yr|y\b)", re.I)
MONTH_RE = re.compile(r"(\d{2,3})\s*(?:-|\s)?\s*month", re.I)

FEE_SAVER_RE = re.compile(r"fee[\s-]?saver", re.I)
STANDARD_RE = re.compile(r"\bstandard\b", re.I)
TRACKER_RE = re.compile(r"\b(tracker|variable|discount)\b", re.I)
FIXED_RE = re.compile(r"\bfix(?:ed)?\b", re.I)
LIFETIME_RE = re.compile(r"\b(lifetime|for the life)\b", re.I)


@dataclass
class Rule:
    """One wanted product, described by what it is rather than what it's called."""

    id: str
    label: str
    ltv: int | None = None
    term_years: int | None = None
    variant: str | None = None  # "fee saver" | "standard"
    kind: str | None = None  # "fixed" | "tracker"

    @classmethod
    def from_config(cls, raw: dict[str, Any], default_ltv: int | None) -> "Rule":
        return cls(
            id=raw["id"],
            label=raw.get("label") or raw["id"],
            ltv=raw.get("ltv", default_ltv),
            term_years=raw.get("term_years"),
            variant=(raw.get("variant") or "").lower() or None,
            kind=(raw.get("kind") or "").lower() or None,
        )


@dataclass
class Selection:
    """The outcome of matching rules against one day's scrape."""

    products: list[Product] = field(default_factory=list)
    unmatched: list[Rule] = field(default_factory=list)
    ambiguous: dict[str, list[Product]] = field(default_factory=dict)


# --------------------------------------------------------------------------
# reading attributes off a scraped product
# --------------------------------------------------------------------------


def ltv_of(product: Product) -> int | None:
    candidates = []
    if product.ltv:
        # A dedicated LTV field: a bare "70" or "70%" is unambiguous here.
        text = clean_text(product.ltv)
        match = LTV_CONTEXT_RE.search(text) or re.fullmatch(r"(\d{2,3})\s*%?", text)
        if match:
            candidates.append(match.group(1))
    # In free text only an explicitly labelled LTV counts, so that a year or a
    # fee in the product name is never mistaken for one.
    named = LTV_CONTEXT_RE.search(clean_text(product.name))
    if named:
        candidates.append(named.group(1))
    for value in candidates:
        if 10 <= int(value) <= 100:
            return int(value)
    return None


def term_of(product: Product) -> int | None:
    text = product.name
    if LIFETIME_RE.search(text):
        return None  # a lifetime tracker has no initial term
    match = TERM_RE.search(text)
    if match:
        return int(match.group(1))
    months = MONTH_RE.search(text)
    if months and int(months.group(1)) % 12 == 0:
        return int(months.group(1)) // 12
    return None


def variant_of(product: Product) -> str | None:
    """"fee saver" or "standard", from the name, falling back to the fee."""
    if FEE_SAVER_RE.search(product.name):
        return "fee saver"
    if STANDARD_RE.search(product.name) and not TRACKER_RE.search(product.name):
        return "standard"
    if product.fee is not None:
        # first direct's Fee Saver products are the no-fee ones.
        return "fee saver" if product.fee == 0 else "standard"
    return None


def kind_of(product: Product) -> str | None:
    if TRACKER_RE.search(product.name) or LIFETIME_RE.search(product.name):
        return "tracker"
    if FIXED_RE.search(product.name):
        return "fixed"
    return None


def describe(product: Product) -> str:
    """Human-readable summary of what the matcher thinks a product is."""
    parts = [
        f"{ltv_of(product)}% LTV" if ltv_of(product) else "LTV ?",
        f"{term_of(product)}yr" if term_of(product) else "term ?",
        variant_of(product) or "variant ?",
        kind_of(product) or "kind ?",
    ]
    return ", ".join(parts)


# --------------------------------------------------------------------------
# matching
# --------------------------------------------------------------------------


def matches(rule: Rule, product: Product) -> bool:
    if rule.ltv is not None and ltv_of(product) != rule.ltv:
        return False
    if rule.term_years is not None and term_of(product) != rule.term_years:
        return False
    if rule.variant is not None and variant_of(product) != rule.variant:
        return False
    if rule.kind is not None and kind_of(product) != rule.kind:
        return False
    return True


def _tie_break(rule: Rule, candidates: list[Product]) -> Product:
    """Prefer the candidate whose name says the most about what we asked for."""

    def score(product: Product) -> tuple:
        name = product.name
        explicit = sum(
            bool(pattern.search(name))
            for pattern in (TERM_RE, FEE_SAVER_RE if rule.variant == "fee saver" else STANDARD_RE)
        )
        stated_ltv = bool(product.ltv)
        return (explicit, stated_ltv, -len(name))

    return sorted(candidates, key=score, reverse=True)[0]


def apply_rules(products: list[Product], rules: list[Rule]) -> Selection:
    """Match each rule against the scrape, keeping rule order for the chart."""
    selection = Selection()
    for rule in rules:
        candidates = [p for p in products if matches(rule, p)]
        if not candidates:
            selection.unmatched.append(rule)
            continue
        chosen = candidates[0] if len(candidates) == 1 else _tie_break(rule, candidates)
        if len(candidates) > 1:
            selection.ambiguous[rule.id] = candidates
        # Re-key onto the rule's stable id so a rename on the page does not
        # start a new series in the chart.
        selection.products.append(
            Product(rule.id, rule.label, chosen.initial_rate, chosen.aprc, chosen.fee, chosen.ltv)
        )
    return selection


def rules_from_config(cfg: dict[str, Any]) -> list[Rule]:
    select_cfg = cfg.get("select") or {}
    default_ltv = select_cfg.get("ltv")
    return [Rule.from_config(raw, default_ltv) for raw in select_cfg.get("products", [])]
