"""Append-only CSV history of observed rates.

One row per product per day. Re-running on a day that already has data
overwrites that day's rows, so the workflow is safe to re-run.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path

from .scrape import Product

FIELDS = ["date", "product_id", "name", "initial_rate", "aprc", "fee", "ltv"]


@dataclass
class Observation:
    date: str
    product_id: str
    name: str
    initial_rate: float
    aprc: float | None = None
    fee: float | None = None
    ltv: str | None = None


def _parse_float(value: str) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def read(path: Path) -> list[Observation]:
    if not path.exists():
        return []
    with path.open(newline="") as handle:
        rows = list(csv.DictReader(handle))
    observations = []
    for row in rows:
        rate = _parse_float(row.get("initial_rate", ""))
        if rate is None:
            continue
        observations.append(
            Observation(
                date=row["date"],
                product_id=row["product_id"],
                name=row.get("name") or row["product_id"],
                initial_rate=rate,
                aprc=_parse_float(row.get("aprc", "")),
                fee=_parse_float(row.get("fee", "")),
                ltv=row.get("ltv") or None,
            )
        )
    return observations


def write(path: Path, observations: list[Observation]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    ordered = sorted(observations, key=lambda o: (o.date, o.product_id))
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        for observation in ordered:
            writer.writerow(
                {
                    "date": observation.date,
                    "product_id": observation.product_id,
                    "name": observation.name,
                    "initial_rate": f"{observation.initial_rate:g}",
                    "aprc": "" if observation.aprc is None else f"{observation.aprc:g}",
                    "fee": "" if observation.fee is None else f"{observation.fee:g}",
                    "ltv": observation.ltv or "",
                }
            )


def upsert(path: Path, date: str, products: list[Product]) -> list[Observation]:
    """Replace ``date``'s rows with ``products`` and persist. Returns full history."""
    history = [o for o in read(path) if o.date != date]
    history.extend(
        Observation(date, p.product_id, p.name, p.initial_rate, p.aprc, p.fee, p.ltv)
        for p in products
    )
    write(path, history)
    return sorted(history, key=lambda o: (o.date, o.product_id))


def latest_two(history: list[Observation], product_id: str) -> tuple[Observation | None, Observation | None]:
    """Return the (previous, current) observations for one product."""
    series = sorted((o for o in history if o.product_id == product_id), key=lambda o: o.date)
    if not series:
        return None, None
    if len(series) == 1:
        return None, series[-1]
    return series[-2], series[-1]
