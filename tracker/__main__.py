"""Command line entry point.

    python -m tracker run                  scrape -> store -> chart -> alert
    python -m tracker run --dry-run        scrape and print, touch nothing
    python -m tracker discover             show every product the parser finds
    python -m tracker discover --html f    replay a saved page (no network)
    python -m tracker chart                rebuild the chart from stored data
    python -m tracker save-html page.html  download the page for offline tuning
"""

from __future__ import annotations

import argparse
import sys
from datetime import date as date_cls
from pathlib import Path

from . import chart as chart_mod
from . import config as config_mod
from . import notify, select, store
from .scrape import ParseError, Product, fetch_html, parse_products, scrape


def _tracked_ids(cfg, fallback=()) -> list[str]:
    """Chart/track order: explicit ids win, else the selection rules in order."""
    if cfg.get("track_products"):
        return list(cfg["track_products"])
    rules = select.rules_from_config(cfg)
    if rules:
        return [rule.id for rule in rules]
    seen: list[str] = []
    for item in fallback:
        if item.product_id not in seen:
            seen.append(item.product_id)
    return seen


def _print_products(products: list[Product], strategy: str) -> None:
    print(f"Found {len(products)} product(s) via the {strategy!r} strategy:\n")
    width = max((len(p.product_id) for p in products), default=10)
    for product in products:
        print(f"  {product.product_id:<{width}}  {product.initial_rate:>6.2f}%  "
              f"{product.name}  [{select.describe(product)}]")


def _apply_selection(products: list[Product], cfg) -> list[Product]:
    """Narrow the scrape to the wanted products, explaining what happened."""
    if cfg.get("track_products"):
        wanted = set(cfg["track_products"])
        return [p for p in products if p.product_id in wanted]

    rules = select.rules_from_config(cfg)
    if not rules:
        return products

    result = select.apply_rules(products, rules)
    print(f"\nSelected {len(result.products)} of {len(rules)} wanted product(s):")
    for product in result.products:
        print(f"  {product.product_id:<18} {product.initial_rate:>6.2f}%  {product.name}")
    for rule in result.unmatched:
        print(f"  {rule.id:<18}    ---   NOT FOUND on the page today ({rule.label})")
    for rule_id, candidates in result.ambiguous.items():
        names = ", ".join(f"{c.name} ({c.initial_rate:g}%)" for c in candidates)
        print(f"  note: {rule_id} matched {len(candidates)} products, took the best fit. Saw: {names}")
    return result.products


def cmd_discover(args, cfg) -> int:
    if args.html:
        html = Path(args.html).read_text()
        products, strategy = parse_products(html, cfg["selectors"])
    else:
        products, strategy = scrape(cfg["url"], cfg["selectors"], cfg["form"])
    _print_products(products, strategy)
    _apply_selection(products, cfg)
    print(
        "\nThe bracketed values are what the selector thinks each product is: "
        "LTV, term, fee saver vs standard, fixed vs tracker.\n"
        "Adjust `select` in tracker/config.json if a wanted product is not being matched."
    )
    return 0


def cmd_save_html(args, cfg) -> int:
    html = fetch_html(cfg["url"], render=args.render, form=cfg["form"])
    Path(args.path).write_text(html)
    print(f"Wrote {len(html):,} bytes to {args.path}")
    return 0


def cmd_test_alert(args, cfg) -> int:
    """Send a fake alert, so delivery can be proved without waiting for a drop."""
    drops = [
        notify.Drop("2 Year Fixed Standard", 4.19, 4.09, record_low=True),
        notify.Drop("3 Year Fixed Fee Saver", 4.04, 3.99),
    ]
    print("Sending a TEST alert (these numbers are made up):\n")
    print(notify.compose(drops, args.chart_url) + "\n")
    delivered = notify.send(drops, args.chart_url)
    if delivered:
        print(f"\nDelivered via: {', '.join(delivered)}")
        return 0
    return 1


def cmd_backfill(args, cfg) -> int:
    from . import backfill as backfill_mod

    result = backfill_mod.backfill(
        cfg,
        config_mod.DATA_PATH,
        from_year=args.from_year,
        to_year=args.to_year,
        limit=args.limit,
        overwrite=args.overwrite,
        dry_run=args.dry_run,
    )
    print(backfill_mod.summarise(result))

    if args.dry_run:
        print("\nDry run - nothing written.")
        return 0
    if result.dates_added:
        history = store.read(config_mod.DATA_PATH)
        chart_mod.render(history, _tracked_ids(cfg, history), config_mod.CHART_PATH)
        print(f"\nRebuilt {config_mod.CHART_PATH} with {len({o.date for o in history})} day(s).")
    else:
        print("\nNothing recovered - the chart is unchanged.")
    return 0


def cmd_chart(args, cfg) -> int:
    history = store.read(config_mod.DATA_PATH)
    payload = chart_mod.render(history, _tracked_ids(cfg, history), config_mod.CHART_PATH)
    print(f"Wrote {config_mod.CHART_PATH} ({len(payload['products'])} products, "
          f"{len(payload['dates'])} days)")
    return 0


def cmd_run(args, cfg) -> int:
    today = args.date or date_cls.today().isoformat()

    if args.html:
        products, strategy = parse_products(Path(args.html).read_text(), cfg["selectors"])
    else:
        products, strategy = scrape(cfg["url"], cfg["selectors"], cfg["form"])
    _print_products(products, strategy)

    tracked = _tracked_ids(cfg, products)
    kept = _apply_selection(products, cfg)
    if not kept:
        print("\nNone of the wanted products were found - refusing to record an empty day.")
        return 1

    if args.dry_run:
        print("\nDry run - nothing written.")
        return 0

    history = store.upsert(config_mod.DATA_PATH, today, kept)
    print(f"\nRecorded {len(kept)} rate(s) for {today} in {config_mod.DATA_PATH}")

    chart_mod.render(history, tracked, config_mod.CHART_PATH)
    print(f"Rebuilt {config_mod.CHART_PATH}")

    alert_cfg = cfg["alert"]
    drops = notify.find_drops(
        history,
        tracked,
        float(alert_cfg.get("drop_threshold_pp", 0.01)),
        bool(alert_cfg.get("alert_on_record_low", True)),
    )
    if drops:
        print("\n" + notify.compose(drops, args.chart_url))
        notify.send(drops, args.chart_url)
    else:
        print("No rate drops today - no alert sent.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="tracker", description=__doc__)
    sub = parser.add_subparsers(dest="command")

    run = sub.add_parser("run", help="scrape, store, rebuild the chart, alert on drops")
    run.add_argument("--dry-run", action="store_true", help="scrape and print only")
    run.add_argument("--html", help="parse this saved file instead of fetching")
    run.add_argument("--date", help="record under this ISO date instead of today")
    run.add_argument("--chart-url", help="link included in the alert text")
    run.set_defaults(func=cmd_run)

    discover = sub.add_parser("discover", help="list every product the parser can see")
    discover.add_argument("--html", help="parse this saved file instead of fetching")
    discover.set_defaults(func=cmd_discover)

    save = sub.add_parser("save-html", help="download the page for offline tuning")
    save.add_argument("path")
    save.add_argument("--render", action="store_true", help="render with a headless browser first")
    save.set_defaults(func=cmd_save_html)

    chart = sub.add_parser("chart", help="rebuild the chart from stored data")
    chart.set_defaults(func=cmd_chart)

    test_alert = sub.add_parser("test-alert", help="send a fake alert to check delivery works")
    test_alert.add_argument("--chart-url", help="link included in the alert")
    test_alert.set_defaults(func=cmd_test_alert)

    back = sub.add_parser("backfill", help="recover past rates from the Internet Archive")
    back.add_argument("--from-year", type=int, help="earliest year to consider")
    back.add_argument("--to-year", type=int, help="latest year to consider")
    back.add_argument("--limit", type=int, help="only the N most recent snapshots")
    back.add_argument("--overwrite", action="store_true", help="replace days already stored")
    back.add_argument("--dry-run", action="store_true", help="report only, write nothing")
    back.set_defaults(func=cmd_backfill)

    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 2

    cfg = config_mod.load()
    try:
        return args.func(args, cfg)
    except ParseError as exc:
        print(f"\nERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
