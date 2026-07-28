"""Configuration loading.

Config lives in ``tracker/config.json``. Keys beginning with ``//`` are comments
and are ignored.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = Path(__file__).resolve().parent / "config.json"

DATA_PATH = REPO_ROOT / "data" / "rates.csv"
CHART_PATH = REPO_ROOT / "docs" / "index.html"

DEFAULTS: dict[str, Any] = {
    "url": "https://mortgages.firstdirect.com/mortgage-rates/list-rates",
    "track_products": [],
    "select": {"ltv": None, "products": []},
    "alert": {"drop_threshold_pp": 0.01, "alert_on_record_low": True},
    "selectors": {"card": None, "name": None, "rate": None},
}


def _strip_comments(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: _strip_comments(v) for k, v in obj.items() if not k.startswith("//")}
    if isinstance(obj, list):
        return [_strip_comments(v) for v in obj]
    return obj


def load(path: Path | None = None) -> dict[str, Any]:
    """Return the config, merged over ``DEFAULTS`` one level deep."""
    path = path or CONFIG_PATH
    raw: dict[str, Any] = {}
    if path.exists():
        raw = _strip_comments(json.loads(path.read_text()))

    merged = dict(DEFAULTS)
    for key, value in raw.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = {**merged[key], **value}
        else:
            merged[key] = value
    return merged
