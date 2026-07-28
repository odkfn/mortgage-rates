"""Reconstruct history from the Internet Archive.

The live page only ever shows today's rates, so the chart normally starts empty
and builds forward. The Wayback Machine has archived that page over the years,
and each snapshot is a rate table from a known date - so the ordinary parser can
read them and fill in the past.

Expect gaps. Snapshots are captured irregularly (sometimes several a week,
sometimes nothing for months) and older ones may predate a page redesign the
parser cannot read. The chart breaks its lines across gaps rather than inventing
a straight run between them.

This is a one-off: run it once, keep whatever it finds, and let the daily job
take over. It never overwrites a reading the daily job recorded.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import requests

from . import select, store
from .scrape import USER_AGENT, ParseError, Product, parse_products

CDX_URL = "http://web.archive.org/cdx/search/cdx"
SNAPSHOT_URL = "https://web.archive.org/web/{timestamp}id_/{original}"

# The archive is a free service run on donations - do not hammer it.
POLITE_DELAY_SECONDS = 1.5
REQUEST_TIMEOUT = 90


@dataclass
class Snapshot:
    timestamp: str  # YYYYMMDDhhmmss
    original: str

    @property
    def date(self) -> str:
        return datetime.strptime(self.timestamp[:8], "%Y%m%d").strftime("%Y-%m-%d")


@dataclass
class Result:
    dates_added: list[str]
    snapshots_seen: int
    snapshots_parsed: int
    skipped_existing: int
    failures: list[tuple[str, str]]


def parse_cdx(payload: list[list[str]]) -> list[Snapshot]:
    """Turn a CDX JSON response into snapshots. The first row is the header."""
    if not payload:
        return []
    header, *rows = payload
    try:
        ts_col = header.index("timestamp")
        url_col = header.index("original")
    except ValueError:
        return []
    return [
        Snapshot(row[ts_col], row[url_col])
        for row in rows
        if len(row) > max(ts_col, url_col)
    ]


def list_snapshots(url: str, from_year: int | None = None, to_year: int | None = None,
                   session: requests.Session | None = None) -> list[Snapshot]:
    """One snapshot per day the page was archived, oldest first."""
    params = {
        "url": url,
        "output": "json",
        "fl": "timestamp,original",
        "filter": "statuscode:200",
        "collapse": "timestamp:8",  # at most one capture per day
    }
    if from_year:
        params["from"] = str(from_year)
    if to_year:
        params["to"] = str(to_year)

    getter = session or requests
    response = getter.get(CDX_URL, params=params, timeout=REQUEST_TIMEOUT,
                          headers={"User-Agent": USER_AGENT})
    response.raise_for_status()
    return parse_cdx(response.json())


def fetch_snapshot(snapshot: Snapshot, session: requests.Session | None = None) -> str:
    """Fetch the archived page as originally served ('id_' strips the archive's
    own toolbar injection, so the markup is what the parser expects)."""
    getter = session or requests
    response = getter.get(
        SNAPSHOT_URL.format(timestamp=snapshot.timestamp, original=snapshot.original),
        timeout=REQUEST_TIMEOUT,
        headers={"User-Agent": USER_AGENT},
    )
    response.raise_for_status()
    return response.text


def products_from_snapshot(html: str, cfg: dict) -> list[Product]:
    """Parse and narrow one archived page with the ordinary daily pipeline."""
    products, _strategy = parse_products(html, cfg.get("selectors"))
    rules = select.rules_from_config(cfg)
    if not rules:
        return products
    return select.apply_rules(products, rules).products


def backfill(cfg: dict, path: Path, from_year: int | None = None,
             to_year: int | None = None, limit: int | None = None,
             overwrite: bool = False, dry_run: bool = False,
             delay: float = POLITE_DELAY_SECONDS) -> Result:
    session = requests.Session()
    snapshots = list_snapshots(cfg["url"], from_year, to_year, session)
    print(f"The archive has {len(snapshots)} day(s) with a snapshot of this page.")

    existing = {o.date for o in store.read(path)}
    if limit:
        # Prefer the most recent snapshots - old page designs parse worst.
        snapshots = snapshots[-limit:]

    result = Result([], len(snapshots), 0, 0, [])
    collected: list[store.Observation] = []

    for index, snapshot in enumerate(snapshots, start=1):
        if snapshot.date in existing and not overwrite:
            result.skipped_existing += 1
            continue
        try:
            html = fetch_snapshot(snapshot, session)
            products = products_from_snapshot(html, cfg)
        except (requests.RequestException, ParseError, ValueError) as exc:
            result.failures.append((snapshot.date, f"{type(exc).__name__}: {exc}"))
            print(f"  [{index}/{len(snapshots)}] {snapshot.date}  failed: {type(exc).__name__}")
            time.sleep(delay)
            continue

        if not products:
            result.failures.append((snapshot.date, "parsed, but none of the wanted products matched"))
            print(f"  [{index}/{len(snapshots)}] {snapshot.date}  no wanted products")
        else:
            result.snapshots_parsed += 1
            result.dates_added.append(snapshot.date)
            rates = ", ".join(f"{p.initial_rate:g}%" for p in products)
            print(f"  [{index}/{len(snapshots)}] {snapshot.date}  {len(products)} product(s): {rates}")
            collected.extend(
                store.Observation(snapshot.date, p.product_id, p.name, p.initial_rate,
                                  p.aprc, p.fee, p.ltv)
                for p in products
            )
        time.sleep(delay)

    if collected and not dry_run:
        history = [o for o in store.read(path) if o.date not in set(result.dates_added)]
        history.extend(collected)
        store.write(path, history)

    return result


def summarise(result: Result) -> str:
    lines = [
        "",
        f"Snapshots considered:      {result.snapshots_seen}",
        f"Days recovered:            {result.snapshots_parsed}",
        f"Skipped (already stored):  {result.skipped_existing}",
        f"Failed / unreadable:       {len(result.failures)}",
    ]
    if result.dates_added:
        lines.append(f"Range recovered:           {result.dates_added[0]} to {result.dates_added[-1]}")
    if result.failures:
        lines.append("")
        lines.append("Unreadable snapshots (usually an older page design):")
        for date, reason in result.failures[:15]:
            lines.append(f"  {date}  {reason}")
        if len(result.failures) > 15:
            lines.append(f"  ... and {len(result.failures) - 15} more")
    return "\n".join(lines)
