"""Tests for the parser, the store and the alert logic.

Three of the fixtures are plausible shapes of the same six products, written by
hand. ``live_filtered_table.html`` is different: it is a real capture of the
rates table the live page serves once its filter is submitted, committed by the
capture workflow. When the two disagree, the live one is right.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from bs4 import BeautifulSoup

from tracker import backfill, chart, notify, select, store
from tracker import config as config_mod
from tracker.scrape import (
    ParseError,
    Product,
    cell_text,
    header_text,
    parse_cards,
    parse_embedded_json,
    parse_products,
    parse_tables,
    slugify,
    to_money,
    to_percent,
)

FIXTURES = Path(__file__).parent / "fixtures"
EXPECTED_RATES = [4.09, 3.89, 4.14, 3.99, 4.44, 4.69]


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text()


# --------------------------------------------------------------------- parsing


@pytest.mark.parametrize(
    "name,parser",
    [
        ("table.html", parse_tables),
        ("cards.html", parse_cards),
        ("embedded.html", parse_embedded_json),
    ],
)
def test_each_strategy_finds_all_six(name, parser):
    products = parser(fixture(name))
    assert [p.initial_rate for p in products] == EXPECTED_RATES
    assert products[0].name.startswith("2 Year Fixed Fee Saver")


@pytest.mark.parametrize("name,strategy", [
    ("table.html", "tables"),
    ("cards.html", "cards"),
    ("embedded.html", "embedded_json"),
])
def test_parse_products_picks_the_right_strategy(name, strategy):
    products, used = parse_products(fixture(name))
    assert used == strategy
    assert len(products) == 6


def test_table_captures_fee_aprc_and_ltv():
    products = parse_tables(fixture("table.html"))
    assert products[0].fee == 0
    assert products[1].fee == 490
    assert products[0].aprc == 6.4
    assert products[0].ltv == "60%"
    assert products[4].fee == 0  # "No fee"


def test_standard_variable_rate_is_not_treated_as_a_product():
    products = parse_tables(fixture("table.html"))
    assert all(p.initial_rate != 6.99 for p in products)


def test_product_ids_are_stable_slugs_including_the_ltv():
    ids = [p.product_id for p in parse_tables(fixture("table.html"))]
    assert ids[0] == "2-year-fixed-fee-saver-60-ltv"
    assert len(set(ids)) == 6


def test_duplicate_products_are_collapsed_and_clashes_suffixed():
    from tracker.scrape import _dedupe

    same = Product("a", "A", 4.0)
    dup = Product("a", "A", 4.0)
    clash = Product("a", "A", 5.0)
    assert [p.product_id for p in _dedupe([same, dup, clash])] == ["a", "a-2"]


def test_empty_page_raises():
    with pytest.raises(ParseError):
        parse_products("<html><body><p>Nothing here</p></body></html>")


@pytest.mark.parametrize("raw,expected", [
    ("4.09%", 4.09), ("4.09 %", 4.09), (3.75, 3.75), ("from 4.5%", 4.5),
    ("", None), ("no rate", None), ("99.9%", None), ("0%", None),
])
def test_to_percent(raw, expected):
    assert to_percent(raw) == expected


@pytest.mark.parametrize("raw,expected", [
    ("£490", 490.0), ("£1,499", 1499.0), ("No fee", 0.0), ("free", 0.0),
    (0, 0.0), ("", None), ("nonsense", None),
])
def test_to_money(raw, expected):
    assert to_money(raw) == expected


def test_slugify_never_returns_empty():
    assert slugify("!!!") == "product"
    assert slugify("5 Year Fixed  Standard") == "5-year-fixed-standard"


# ------------------------------------------------------------------- selection


def test_full_page_yields_every_product_across_every_ltv_band():
    products, strategy = parse_products(fixture("full_page.html"))
    assert strategy == "tables"
    assert len(products) == 19
    assert {select.ltv_of(p) for p in products} == {60, 70, 85}


def test_ltv_is_inherited_from_the_section_heading():
    products = parse_tables(fixture("full_page.html"))
    seventies = [p for p in products if select.ltv_of(p) == 70]
    assert len(seventies) == 8
    assert all(p.ltv == "70%" for p in seventies)


def test_the_configured_rules_pick_exactly_the_wanted_six():
    """The requirement: 70% LTV, 2/3/5 year, fee saver and standard."""
    products = parse_tables(fixture("full_page.html"))
    rules = select.rules_from_config(config_mod.load())
    result = select.apply_rules(products, rules)

    assert result.unmatched == []
    assert result.ambiguous == {}
    # Display order: standard 2/3/5 year, then fee saver 2/3/5 year.
    assert [(p.product_id, p.initial_rate) for p in result.products] == [
        ("2yr-standard-70", 3.99),
        ("3yr-standard-70", 4.04),
        ("5yr-standard-70", 4.09),
        ("2yr-fee-saver-70", 4.19),
        ("3yr-fee-saver-70", 4.24),
        ("5yr-fee-saver-70", 4.29),
    ]


def test_chart_series_follow_the_configured_display_order():
    """The legend, table columns and colours all key off this order."""
    from tracker.__main__ import _tracked_ids

    cfg = config_mod.load()
    assert _tracked_ids(cfg) == [
        "2yr-standard-70", "3yr-standard-70", "5yr-standard-70",
        "2yr-fee-saver-70", "3yr-fee-saver-70", "5yr-fee-saver-70",
    ]

    products = parse_tables(fixture("full_page.html"))
    selected = select.apply_rules(products, select.rules_from_config(cfg)).products
    history = [
        store.Observation("2026-07-27", p.product_id, p.name, p.initial_rate, p.aprc, p.fee, p.ltv)
        for p in selected
    ]
    # Storage sorts rows by id, so the payload must re-impose the display order.
    payload = chart.build_payload(history, _tracked_ids(cfg))
    assert [p["id"] for p in payload["products"]] == _tracked_ids(cfg)


def test_selection_excludes_other_ltvs_trackers_and_the_svr():
    products = parse_tables(fixture("full_page.html"))
    result = select.apply_rules(products, select.rules_from_config(config_mod.load()))
    rates = {p.initial_rate for p in result.products}
    assert 3.99 in rates  # the 70% 2yr standard
    assert 3.79 not in rates  # 60% band
    assert 4.49 not in rates  # 85% band
    assert 4.54 not in rates and 4.79 not in rates  # trackers
    assert 6.99 not in rates  # standard variable rate


def test_a_missing_product_is_reported_not_silently_dropped():
    products = parse_tables(fixture("full_page.html"))
    rules = [
        select.Rule("3yr-fee-saver-85", "3 Year Fee Saver", ltv=85, term_years=3, variant="fee saver"),
    ]
    result = select.apply_rules(products, rules)
    assert result.products == []
    assert [r.id for r in result.unmatched] == ["3yr-fee-saver-85"]


def test_rule_id_survives_the_page_renaming_a_product():
    """A renamed product must continue the same chart series, not start a new one."""
    renamed = Product("whatever", "2 Year Fixed - Fee Saver option", 4.19, ltv="70%")
    rules = select.rules_from_config(config_mod.load())
    result = select.apply_rules([renamed], [r for r in rules if r.id == "2yr-fee-saver-70"])
    assert result.products[0].product_id == "2yr-fee-saver-70"
    assert result.products[0].name == "2 Year Fixed Fee Saver"


@pytest.mark.parametrize("name,term", [
    ("2 Year Fixed Fee Saver", 2), ("3-Year Fixed", 3), ("5 yr fixed", 5),
    ("24 Month Fixed", 2), ("Lifetime Tracker", None), ("Offset Mortgage", None),
])
def test_term_detection(name, term):
    assert select.term_of(Product("x", name, 4.0)) == term


@pytest.mark.parametrize("name,fee,variant", [
    ("2 Year Fixed Fee Saver", None, "fee saver"),
    ("2 Year Fixed FeeSaver", None, "fee saver"),
    ("2 Year Fixed Standard", None, "standard"),
    ("2 Year Fixed", 0.0, "fee saver"),      # inferred from a zero fee
    ("2 Year Fixed", 490.0, "standard"),     # inferred from a real fee
    ("2 Year Fixed", None, None),
])
def test_variant_detection(name, fee, variant):
    assert select.variant_of(Product("x", name, 4.0, fee=fee)) == variant


@pytest.mark.parametrize("name,kind", [
    ("2 Year Fixed", "fixed"), ("2 Year Tracker", "tracker"),
    ("Lifetime Tracker", "tracker"), ("Offset", None),
])
def test_kind_detection(name, kind):
    assert select.kind_of(Product("x", name, 4.0)) == kind


def test_a_year_in_the_name_is_not_mistaken_for_an_ltv():
    assert select.ltv_of(Product("x", "2 Year Fixed Fee Saver", 4.0)) is None
    assert select.ltv_of(Product("x", "2 Year Fixed", 4.0, ltv="70%")) == 70
    assert select.ltv_of(Product("x", "2 Year Fixed at 70% LTV", 4.0)) == 70


def test_ambiguous_match_is_flagged_and_resolved():
    products = [
        Product("a", "2 Year Fixed", 4.30, ltv="70%", fee=0.0),
        Product("b", "2 Year Fixed Fee Saver", 4.19, ltv="70%", fee=0.0),
    ]
    rules = [r for r in select.rules_from_config(config_mod.load()) if r.id == "2yr-fee-saver-70"]
    result = select.apply_rules(products, rules)
    assert "2yr-fee-saver-70" in result.ambiguous
    # The explicitly-named product wins over the one matched by inference.
    assert result.products[0].initial_rate == 4.19


# ----------------------------------------------------------------------- store


def test_upsert_replaces_the_same_day(tmp_path):
    path = tmp_path / "rates.csv"
    store.upsert(path, "2026-07-01", [Product("a", "A", 4.5)])
    history = store.upsert(path, "2026-07-01", [Product("a", "A", 4.4)])
    assert len(history) == 1
    assert history[0].initial_rate == 4.4


def test_round_trip_preserves_optional_fields(tmp_path):
    path = tmp_path / "rates.csv"
    store.upsert(path, "2026-07-01", [Product("a", "A", 4.5, None, 0.0, "60%")])
    row = store.read(path)[0]
    assert row.aprc is None and row.fee == 0.0 and row.ltv == "60%"


def test_latest_two(tmp_path):
    path = tmp_path / "rates.csv"
    store.upsert(path, "2026-07-01", [Product("a", "A", 4.5)])
    history = store.upsert(path, "2026-07-02", [Product("a", "A", 4.3)])
    previous, current = store.latest_two(history, "a")
    assert (previous.initial_rate, current.initial_rate) == (4.5, 4.3)


# ---------------------------------------------------------------------- alerts


def history_for(rates: list[float], product_id: str = "a") -> list[store.Observation]:
    return [
        store.Observation(f"2026-07-{i + 1:02d}", product_id, "2 Year Fixed", rate)
        for i, rate in enumerate(rates)
    ]


def test_drop_below_threshold_is_ignored():
    assert notify.find_drops(history_for([4.50, 4.495]), ["a"], 0.01) == []


def test_drop_at_threshold_alerts():
    drops = notify.find_drops(history_for([4.50, 4.49]), ["a"], 0.01)
    assert len(drops) == 1
    assert drops[0].delta == pytest.approx(-0.01)


def test_rise_never_alerts():
    assert notify.find_drops(history_for([4.30, 4.60]), ["a"], 0.01) == []


def test_first_ever_reading_never_alerts():
    assert notify.find_drops(history_for([4.30]), ["a"], 0.01) == []


def test_record_low_is_flagged_only_when_it_is_one():
    assert notify.find_drops(history_for([4.0, 4.5, 3.9]), ["a"], 0.01)[0].record_low is True
    assert notify.find_drops(history_for([3.5, 4.5, 3.9]), ["a"], 0.01)[0].record_low is False


def test_untracked_products_do_not_alert():
    assert notify.find_drops(history_for([4.5, 4.0]), ["other"], 0.01) == []


def test_message_fits_in_an_sms_and_names_the_products():
    body = notify.compose(notify.find_drops(history_for([4.5, 4.0]), ["a"], 0.01), "https://x.test")
    assert "2 Year Fixed" in body and "4.5% -> 4%" in body
    assert "https://x.test" in body
    assert len(body) <= notify.SMS_MAX_CHARS


def test_long_message_is_truncated():
    drops = [notify.Drop(f"Product number {i} with a long name", 4.5, 4.0) for i in range(40)]
    assert len(notify.compose(drops)) <= notify.SMS_MAX_CHARS


ALERT_ENV = ("NTFY_TOPIC", "NTFY_SERVER", "NTFY_TOKEN",
             "TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_FROM", "ALERT_TO")


@pytest.fixture
def no_alert_env(monkeypatch):
    for var in ALERT_ENV:
        monkeypatch.delenv(var, raising=False)
    return monkeypatch


def test_send_sms_skips_cleanly_when_unconfigured(no_alert_env):
    assert notify.send_sms("hello") is False


def test_send_ntfy_skips_cleanly_when_unconfigured(no_alert_env):
    assert notify.send_ntfy("hello", "title") is False


def test_nothing_configured_reports_instead_of_failing(no_alert_env, capsys):
    drops = notify.find_drops(history_for([4.5, 4.0]), ["a"], 0.01)
    assert notify.send(drops) == []
    out = capsys.readouterr().out
    assert "No alert channel configured" in out
    assert "2 Year Fixed" in out  # the alert is still visible in the log


class FakePost:
    """Records ntfy posts instead of making them."""

    def __init__(self, status=200):
        self.calls = []
        self.status = status

    def __call__(self, url, data=None, headers=None, timeout=None):
        self.calls.append({"url": url, "data": data, "headers": headers or {}})
        return type("Response", (), {"status_code": self.status})()


def test_ntfy_posts_to_the_configured_topic(no_alert_env, monkeypatch):
    no_alert_env.setenv("NTFY_TOPIC", "my-secret-topic")
    post = FakePost()
    monkeypatch.setattr(notify.requests, "post", post)

    drops = notify.find_drops(history_for([4.5, 4.0]), ["a"], 0.01)
    assert notify.send(drops, "https://chart.test") == ["ntfy"]

    call = post.calls[0]
    assert call["url"] == "https://ntfy.sh/my-secret-topic"
    assert b"2 Year Fixed" in call["data"]
    # The chart is a tappable link, not body text.
    assert call["headers"]["Click"] == "https://chart.test"
    assert "https://chart.test" not in call["data"].decode()
    assert call["headers"]["Title"] == "Mortgage rate drop (first direct)"


def test_ntfy_honours_a_self_hosted_server_and_token(no_alert_env, monkeypatch):
    no_alert_env.setenv("NTFY_TOPIC", "topic")
    no_alert_env.setenv("NTFY_SERVER", "https://ntfy.example.com/")
    no_alert_env.setenv("NTFY_TOKEN", "tk_secret")
    post = FakePost()
    monkeypatch.setattr(notify.requests, "post", post)

    notify.send_ntfy("body", "title")
    assert post.calls[0]["url"] == "https://ntfy.example.com/topic"
    assert post.calls[0]["headers"]["Authorization"] == "Bearer tk_secret"


def test_ntfy_title_survives_non_ascii(no_alert_env, monkeypatch):
    """HTTP headers are Latin-1, so a stray unicode char must not crash the send."""
    no_alert_env.setenv("NTFY_TOPIC", "topic")
    post = FakePost()
    monkeypatch.setattr(notify.requests, "post", post)

    notify.send_ntfy("body", "Rate drop — first direct")
    assert post.calls[0]["headers"]["Title"].isascii()


def test_ntfy_error_is_raised_not_swallowed(no_alert_env, monkeypatch):
    no_alert_env.setenv("NTFY_TOPIC", "topic")
    monkeypatch.setattr(notify.requests, "post", FakePost(status=403))
    with pytest.raises(RuntimeError, match="403"):
        notify.send_ntfy("body", "title")


def test_a_failing_channel_does_not_silence_the_other(no_alert_env, monkeypatch, capsys):
    """A Twilio billing problem must not stop the ntfy push going out."""
    no_alert_env.setenv("NTFY_TOPIC", "topic")
    monkeypatch.setattr(notify.requests, "post", FakePost())
    monkeypatch.setattr(notify, "send_sms", lambda body: (_ for _ in ()).throw(RuntimeError("boom")))

    drops = notify.find_drops(history_for([4.5, 4.0]), ["a"], 0.01)
    assert notify.send(drops) == ["ntfy"]
    assert "sms alert failed" in capsys.readouterr().out


def test_both_channels_fire_when_both_configured(no_alert_env, monkeypatch):
    no_alert_env.setenv("NTFY_TOPIC", "topic")
    monkeypatch.setattr(notify.requests, "post", FakePost())
    monkeypatch.setattr(notify, "send_sms", lambda body: True)

    drops = notify.find_drops(history_for([4.5, 4.0]), ["a"], 0.01)
    assert notify.send(drops) == ["ntfy", "sms"]


def test_ntfy_body_is_not_truncated_to_sms_length(no_alert_env, monkeypatch):
    no_alert_env.setenv("NTFY_TOPIC", "topic")
    post = FakePost()
    monkeypatch.setattr(notify.requests, "post", post)

    drops = [notify.Drop(f"Product number {i} with a long name", 4.5, 4.0) for i in range(40)]
    notify.send(drops)
    assert len(post.calls[0]["data"]) > notify.SMS_MAX_CHARS


# -------------------------------------------------------------------- backfill

CDX_RESPONSE = [
    ["timestamp", "original"],
    ["20260601120000", "https://mortgages.firstdirect.com/mortgage-rates/list-rates"],
    ["20260615093000", "https://mortgages.firstdirect.com/mortgage-rates/list-rates"],
    ["20260702170000", "https://mortgages.firstdirect.com/mortgage-rates/list-rates"],
]


def test_cdx_response_becomes_dated_snapshots():
    snapshots = backfill.parse_cdx(CDX_RESPONSE)
    assert [s.date for s in snapshots] == ["2026-06-01", "2026-06-15", "2026-07-02"]


def test_cdx_edge_cases_do_not_crash():
    assert backfill.parse_cdx([]) == []
    assert backfill.parse_cdx([["timestamp", "original"]]) == []
    assert backfill.parse_cdx([["nothing", "useful"], ["a", "b"]]) == []
    # A truncated row is skipped rather than raising.
    assert backfill.parse_cdx([["timestamp", "original"], ["20260601120000"]]) == []


def test_snapshot_url_requests_the_unmodified_original():
    snapshot = backfill.Snapshot("20260601120000", "https://example.test/rates")
    url = backfill.SNAPSHOT_URL.format(timestamp=snapshot.timestamp, original=snapshot.original)
    # The "id_" suffix is what stops the archive injecting its own toolbar markup.
    assert url == "https://web.archive.org/web/20260601120000id_/https://example.test/rates"


def test_an_archived_page_parses_through_the_normal_pipeline():
    products = backfill.products_from_snapshot(fixture("full_page.html"), config_mod.load())
    assert [p.product_id for p in products][:3] == [
        "2yr-standard-70", "3yr-standard-70", "5yr-standard-70",
    ]


def _fake_backfill(monkeypatch, pages: dict[str, str]):
    """Run backfill against canned snapshots - no network."""
    snapshots = [backfill.Snapshot(ts, "https://x.test/rates") for ts in pages]
    monkeypatch.setattr(backfill, "list_snapshots", lambda *a, **k: snapshots)
    monkeypatch.setattr(backfill, "fetch_snapshot", lambda s, session=None: pages[s.timestamp])


def test_backfill_writes_recovered_days(tmp_path, monkeypatch):
    path = tmp_path / "rates.csv"
    _fake_backfill(monkeypatch, {
        "20260601120000": fixture("full_page.html"),
        "20260615093000": fixture("full_page.html"),
    })
    result = backfill.backfill(config_mod.load(), path, delay=0)
    assert result.snapshots_parsed == 2
    assert result.dates_added == ["2026-06-01", "2026-06-15"]
    assert {o.date for o in store.read(path)} == {"2026-06-01", "2026-06-15"}
    assert len(store.read(path)) == 12  # six products on each of two days


def test_backfill_never_overwrites_a_recorded_day(tmp_path, monkeypatch):
    """A real daily reading must win over an archived guess for the same date."""
    path = tmp_path / "rates.csv"
    store.upsert(path, "2026-06-01", [Product("2yr-standard-70", "2 Year Fixed Standard", 9.99)])
    _fake_backfill(monkeypatch, {"20260601120000": fixture("full_page.html")})

    result = backfill.backfill(config_mod.load(), path, delay=0)
    assert result.skipped_existing == 1
    assert result.dates_added == []
    assert store.read(path)[0].initial_rate == 9.99


def test_backfill_overwrite_flag_replaces_the_day(tmp_path, monkeypatch):
    path = tmp_path / "rates.csv"
    store.upsert(path, "2026-06-01", [Product("2yr-standard-70", "2 Year Fixed Standard", 9.99)])
    _fake_backfill(monkeypatch, {"20260601120000": fixture("full_page.html")})

    backfill.backfill(config_mod.load(), path, overwrite=True, delay=0)
    rows = store.read(path)
    assert len(rows) == 6
    assert 9.99 not in {r.initial_rate for r in rows}


def test_backfill_records_unreadable_snapshots_without_stopping(tmp_path, monkeypatch):
    path = tmp_path / "rates.csv"
    _fake_backfill(monkeypatch, {
        "20260601120000": "<html><body>a page redesign we cannot read</body></html>",
        "20260615093000": fixture("full_page.html"),
    })
    result = backfill.backfill(config_mod.load(), path, delay=0)
    assert result.dates_added == ["2026-06-15"]
    assert [d for d, _ in result.failures] == ["2026-06-01"]
    assert "Unreadable snapshots" in backfill.summarise(result)


def test_backfill_dry_run_writes_nothing(tmp_path, monkeypatch):
    path = tmp_path / "rates.csv"
    _fake_backfill(monkeypatch, {"20260601120000": fixture("full_page.html")})
    result = backfill.backfill(config_mod.load(), path, dry_run=True, delay=0)
    assert result.snapshots_parsed == 1
    assert not path.exists()


def test_backfill_limit_prefers_the_most_recent_snapshots(tmp_path, monkeypatch):
    path = tmp_path / "rates.csv"
    _fake_backfill(monkeypatch, {
        "20260601120000": fixture("full_page.html"),
        "20260615093000": fixture("full_page.html"),
        "20260702170000": fixture("full_page.html"),
    })
    result = backfill.backfill(config_mod.load(), path, limit=2, delay=0)
    assert result.dates_added == ["2026-06-15", "2026-07-02"]


def test_backfilled_history_charts_with_gaps_not_bridged(tmp_path, monkeypatch):
    path = tmp_path / "rates.csv"
    _fake_backfill(monkeypatch, {
        "20260601120000": fixture("full_page.html"),
        "20260702170000": fixture("full_page.html"),
    })
    backfill.backfill(config_mod.load(), path, delay=0)
    payload = chart.build_payload(store.read(path), ["2yr-standard-70"])
    # Two archived days, a month apart, are two points - not 32 invented ones.
    assert payload["dates"] == ["2026-06-01", "2026-07-02"]
    assert payload["products"][0]["values"] == [3.99, 3.99]


# ----------------------------------------------------------------------- chart


def test_chart_payload_aligns_series_with_gaps():
    history = [
        store.Observation("2026-07-01", "a", "A", 4.5),
        store.Observation("2026-07-01", "b", "B", 5.0),
        store.Observation("2026-07-02", "a", "A", 4.4),  # b missing this day
    ]
    payload = chart.build_payload(history, ["a", "b"])
    assert payload["dates"] == ["2026-07-01", "2026-07-02"]
    assert payload["products"][0]["values"] == [4.5, 4.4]
    assert payload["products"][1]["values"] == [5.0, None]


def test_chart_writes_a_self_contained_page(tmp_path):
    path = tmp_path / "index.html"
    chart.render(history_for([4.5, 4.4]), ["a"], path)
    html = path.read_text()
    assert "<!doctype html>" in html
    assert "__PAYLOAD__" not in html
    assert "src=\"http" not in html and "@import" not in html  # no external assets
    assert "4.4" in html


def test_chart_handles_an_empty_history(tmp_path):
    path = tmp_path / "index.html"
    payload = chart.render([], [], path)
    assert payload["products"] == [] and payload["dates"] == []
    assert path.exists()


# ------------------------------------------------------- the live rates table

# A real capture, so these tests are the ones that say whether the tracker
# works against the site as it actually is. The rates move daily; the shape
# does not, so assert on the shape and on plausibility, never on today's rate.

LIVE_PRODUCT_IDS = [
    "2yr-standard-70",
    "3yr-standard-70",
    "5yr-standard-70",
    "2yr-fee-saver-70",
    "3yr-fee-saver-70",
    "5yr-fee-saver-70",
]


def live_products():
    products, strategy = parse_products(fixture("live_filtered_table.html"))
    assert strategy == "tables"
    return products


def test_live_table_parses_six_products_across_six_ltv_bands():
    products = live_products()
    assert len(products) == 36
    assert len({p.name for p in products}) == 6
    assert sorted({p.ltv for p in products}) == ["60%", "70%", "75%", "80%", "85%", "90%"]


def test_live_names_are_the_product_not_the_fee_column():
    """The name cell hides a screen-reader span quoting the LTV and the rate.
    Read naively it looks like a percentage, the name column gets rejected, and
    every product ends up named after a fee."""
    names = {p.name for p in live_products()}
    assert names == {
        f"{term} Year Fixed Repayment - {variant}"
        for term in (2, 3, 5)
        for variant in ("Fee Saver", "Standard")
    }


def test_live_cells_carry_no_tooltip_prose():
    for product in live_products():
        assert "More info" not in product.name
        assert "£" not in product.name and "%" not in product.name
        assert re.fullmatch(r"\d{2}%", product.ltv or ""), product.ltv


def test_live_rates_are_plausible_and_distinct_per_band():
    products = live_products()
    for product in products:
        assert 0 < product.initial_rate < 25
        assert product.aprc is None or 0 < product.aprc < 25
    two_year_fee_saver = sorted(
        (p.ltv, p.initial_rate) for p in products
        if p.name == "2 Year Fixed Repayment - Fee Saver"
    )
    # A higher LTV is never cheaper than a lower one.
    rates = [rate for _, rate in two_year_fee_saver]
    assert rates == sorted(rates)


def test_live_booking_fee_separates_the_two_variants():
    fees = {p.name.split("- ")[-1]: p.fee for p in live_products()}
    assert fees["Fee Saver"] == 0.0
    assert fees["Standard"] > 0


def test_live_table_selects_exactly_the_tracked_six():
    cfg = config_mod.load()
    selection = select.apply_rules(live_products(), select.rules_from_config(cfg))
    assert [p.product_id for p in selection.products] == LIVE_PRODUCT_IDS
    assert selection.unmatched == []
    assert selection.ambiguous == {}
    assert all(p.ltv == "70%" for p in selection.products)


def test_cell_text_drops_tooltips_hidden_text_and_sort_buttons():
    cell = BeautifulSoup(
        """<td><a>2 Year Fixed Repayment - Fee Saver
             <span class="visuallyhidden">with 60% LTV and interest rate 4.71%</span></a>
           <div class="tooltip-container" role="tooltip">
             <button><span class="visuallyhidden">More info</span></button>
             <div class="tooltip__content">An explanation mentioning a product.</div>
           </div></td>""",
        "html.parser",
    ).find("td")
    assert cell_text(cell) == "2 Year Fixed Repayment - Fee Saver"


def test_header_text_prefers_the_declared_column_title():
    th = BeautifulSoup(
        '<th data-col-title="booking fee"><button>Sort Booking fee</button>'
        '<span>Booking fee</span></th>',
        "html.parser",
    ).find("th")
    assert header_text(th) == "booking fee"


def test_tooltip_prose_does_not_capture_the_name_column():
    """The APRC tooltip says "...depending on the product chosen", which is
    enough to make that column look like the product column."""
    html = """<table>
      <tr>
        <th data-col-title="Mortgage">Mortgage</th>
        <th data-col-title="Interest rate">Interest rate</th>
        <th data-col-title="Overall cost for comparison">Overall cost
          <div class="tooltip-container" role="tooltip">APRC varies by the product chosen.</div>
        </th>
      </tr>
      <tr><td>2 Year Fixed Repayment - Standard</td><td>4.57%</td><td>6.3%</td></tr>
    </table>"""
    products = parse_tables(html)
    assert [p.name for p in products] == ["2 Year Fixed Repayment - Standard"]
    assert products[0].initial_rate == 4.57
    assert products[0].aprc == 6.3
