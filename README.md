# first direct mortgage rate tracker

Checks <https://mortgages.firstdirect.com/mortgage-rates/list-rates> once a day
from GitHub Actions — no computer of yours needs to be on — records each
product's initial rate, redraws a chart you can open in a browser, and texts you
when a rate drops.

**Tracking six products at 70% LTV**, charted in this order: 2, 3 and 5 year
fixed Standard, then 2, 3 and 5 year fixed Fee Saver.

```
.github/workflows/mortgage-rates.yml   the daily schedule
.github/workflows/backfill.yml         one-off recovery from the Internet Archive
tracker/                               scrape → store → chart → alert
tracker/config.json                    which products to track, alert threshold
tracker/select.py                      picks the wanted six out of the whole page
data/rates.csv                         the history, one row per product per day
docs/index.html                        the chart (regenerated every run)
tests/                                 parser + alert tests, run before each scrape
```

## Setup

### 1. Check it is picking the right six products

The page lists dozens of products across every LTV band. Selection is by
**criteria, not names** — `select` in `tracker/config.json` says "70% LTV, 2/3/5
year, fixed, fee saver or standard", and the matcher works out which rows on the
page those are. That way first direct rewording a product name does not break the
match or split a chart line in two.

Verify it against the live page:

```bash
pip install -r requirements.txt
python -m tracker discover
```

Every product found is listed with what the matcher classified it as —
`[70% LTV, 2yr, fee saver, fixed]` — followed by the six it selected. If a
wanted product shows `term ?` or `variant ?`, or the wrong row got picked, adjust
its rule:

```json
{ "id": "3yr-fee-saver-70", "label": "3 Year Fixed Fee Saver",
  "term_years": 3, "variant": "fee saver", "kind": "fixed", "ltv": 70 }
```

Any criterion you leave out simply is not checked. `ltv` at the top of `select`
applies to every rule that does not state its own. The `id` is what the history
and the chart are keyed on — keep it stable, and the series survives any renaming
on the page.

If a rule matches nothing on a given day, the run says so loudly and records the
others rather than guessing. If it matches more than one, it takes the best fit
and prints every candidate so you can tighten the rule.

**Escape hatch:** to bypass the rules entirely, put exact scraped ids (as shown
by `discover`) in `track_products` and they win.

The order of the `products` list is the display order — it drives the legend, the
table columns and which colour each product gets. It currently reads Standard
2/3/5 year, then Fee Saver 2/3/5 year. Reordering it repaints the whole chart
(colour follows position), so it is worth settling early and then leaving alone.

The hover tooltip is the deliberate exception: its rows are sorted by rate, so
they line up top-to-bottom with the lines under the crosshair.

### 2. Turn on the chart page

Repo **Settings → Pages → Source: Deploy from a branch**, branch `main`, folder
`/docs`. The chart is then at
`https://<username>.github.io/mortgage-rates/`, and it updates itself every day.

You can also just open `docs/index.html` from a local clone — it is a single
self-contained file with no external assets, so it works offline.

### 3. Turn on alerts

Two channels are supported. Set up either, or both — whichever is configured
gets used, and they are independent, so a problem with one never silences the
other. With neither set up everything else still runs and the alert is written
to the workflow log.

#### ntfy — free phone push, no account (recommended)

1. Install **ntfy** from the App Store or Google Play (or use
   <https://ntfy.sh> in a browser).
2. **Generate** a topic name — do not invent one by hand, and never copy one out
   of documentation:

   ```bash
   python -c "import secrets; print('fd-rates-' + secrets.token_hex(9))"
   ```

   ntfy has no accounts and no passwords: **the topic name is the only thing
   protecting it.** Anyone who knows or guesses it can read your alerts and push
   fake ones to your phone. So it must be long, random, and never written down
   anywhere public — including this file, a commit message, or a screenshot.
3. In the app, tap **+** and subscribe to that exact name.
4. Add it as a repo secret under **Settings → Secrets and variables → Actions**.
   Secrets are encrypted and are not visible in the repo, even though this
   repository is public:

   | Secret | Value |
   |---|---|
   | `NTFY_TOPIC` | the name you generated |

That is the whole setup. The notification title says how many rates dropped, the
body lists them, and tapping it opens the chart.

If a topic name ever leaks, there is nothing to revoke — generate a new one,
subscribe to it in the app, and update the `NTFY_TOPIC` secret.

Optional, only if you self-host ntfy or protect the topic: `NTFY_SERVER`
(defaults to `https://ntfy.sh`) and `NTFY_TOKEN`.

#### Twilio — a real SMS, a few pence each

Use this if you want an actual text message rather than an app notification.
Sign up at [twilio.com](https://www.twilio.com/), then add four secrets:

| Secret | Value |
|---|---|
| `TWILIO_ACCOUNT_SID` | starts `AC…` |
| `TWILIO_AUTH_TOKEN` | your auth token |
| `TWILIO_FROM` | the Twilio number sending the text, e.g. `+447700900123` |
| `ALERT_TO` | your mobile, e.g. `+447700900456` |

On a trial account you can only send to numbers you have verified, which is fine
when you are only texting yourself.

### 4. Check the alerts actually reach you

Do not wait for a real rate drop to find out whether this works:

**Actions → Mortgage rates → Run workflow → tick "test_alert"**. It sends a
fake alert with made-up numbers through every configured channel and then stops —
it does not scrape or commit anything. Locally, the same thing is
`python -m tracker test-alert`.

### 5. Try a real run

**Actions → Mortgage rates → Run workflow**, with *dry run* ticked for a
no-commit, no-alert test. Untick it for a real run.

## Where the graph lives

`docs/index.html` is regenerated on every run and committed to the repo, so
there are three ways to it:

1. **GitHub Pages** (the good one) — Settings → Pages → Deploy from a branch,
   `main`, folder `/docs`. It is then a normal web page at
   `https://<username>.github.io/mortgage-rates/`, openable on a phone,
   bookmarkable, and the link the alert text carries. Give it a minute after the
   first run.
2. **Straight from GitHub** — open `docs/index.html` in the repo and click
   *Preview*. Works with no setup, but it is clunky and GitHub does not run the
   page's JavaScript in the raw file view.
3. **Offline** — clone or download the repo and open `docs/index.html`. It is a
   single self-contained file with no external assets, so it works with no
   connection at all.

`data/rates.csv` is the underlying history if you would rather work in a
spreadsheet — it opens in Excel or Sheets directly.

## Filling in the past

The live page only ever shows today's rates, so the chart normally starts empty
and builds forward one day at a time. The Internet Archive has snapshots of the
page going back years, and the ordinary parser can read them:

**Actions → Backfill from the Internet Archive → Run workflow.** Leave *dry run*
ticked the first time — it reports what it can recover without writing anything.
Untick it to keep the results.

Expect gaps, and expect older snapshots to fail: page redesigns the current
parser cannot read are reported per-date in the log rather than silently
skipped. The chart breaks its lines across gaps instead of drawing a straight
line through months it has no data for.

It never overwrites a day the daily job recorded — a real reading always beats an
archived one — so it is safe to run at any point, not just before the first
scrape.

## When it alerts

A text goes out when a tracked product's rate is at least
`alert.drop_threshold_pp` percentage points **below its previous reading**
(default `0.01`, i.e. any drop at all). Rate rises never alert. A drop that also
takes the product below every rate previously recorded is marked "record low".
One text covers all the drops in a day, and it looks like this:

```
2 mortgage rates dropped (first direct):
- 2 Year Fixed Standard: 4.19% -> 4.09% (-0.1pp) - record low
- 3 Year Fixed Fee Saver: 4.04% -> 3.94% (-0.1pp)
https://your-username.github.io/mortgage-rates/
```

The mechanism, end to end: the Action finishes scraping, compares against the
previous reading in `data/rates.csv`, and if anything fell it posts the message
to whichever channels are configured — ntfy (a push notification to your phone,
with the chart as a tappable link) and/or Twilio (an ordinary SMS).

Two consequences of comparing against the *previous reading*: nothing can alert
on the very first run (there is nothing to compare to), and if a run is skipped
or fails, the next successful run compares against the last good day rather than
missing the drop.

Backfilling never sends alerts — it only writes history.

## Running it locally

```bash
python -m tracker run --dry-run     # scrape and print, change nothing
python -m tracker run               # the full daily job
python -m tracker discover          # list what the parser can see
python -m tracker chart             # rebuild the chart from data/rates.csv
python -m tracker test-alert        # prove notifications reach your phone
python -m tracker backfill --dry-run  # see what the archive could recover
python -m pytest tests -q           # the test suite
```

## If the scrape breaks

Bank sites get redesigned, and this one is behind a bot filter that blocks some
networks outright. When a run fails, the workflow uploads the page it fetched as
a `page-html` artifact. Download it and replay it offline as many times as you
like:

```bash
python -m tracker discover --html page.html
```

Parsing tries three strategies in order and keeps the first that finds anything:

1. **`embedded_json`** — rate data serialised into a `<script>` tag.
2. **`tables`** — every `<table>` whose headers name a rate column. An LTV is
   read from an LTV column, or inherited from the section heading above the
   table ("Borrowing up to 70% LTV") when there isn't one.
3. **`cards`** — repeated blocks that each pair a heading with a percentage,
   again inheriting an LTV from an enclosing section if needed.

Scraping is deliberately greedy — it collects every product at every LTV — and
`select` narrows it afterwards. So a parse problem and a selection problem look
different in the log, and are fixed in different places.

Two escape hatches, in order of effort:

- **CSS selectors.** Fill in `selectors` in `tracker/config.json` — `card` (the
  repeating block), `name` and `rate` — and the card strategy uses those instead
  of guessing.
- **A fixture.** Save the page into `tests/fixtures/`, add it to
  `tests/test_tracker.py`, and change the parser until the test passes. The
  suite runs before every scrape, so a parser regression fails the job instead of
  quietly recording nothing.

If the plain HTTP fetch returns a page without rates in it, the run automatically
retries through a headless Chromium, which handles the page being rendered in
JavaScript. A day where nothing parses records nothing — it never writes a blank
or a zero into the history.

## Notes

- One request a day with a normal browser user-agent. Rates are public
  information, but they are indicative: check the real figures with first direct
  before acting on a text message.
- `data/rates.csv` is the durable artefact — the chart is derived from it and can
  always be rebuilt with `python -m tracker chart`.
- Re-running on a day that already has data replaces that day's rows, so a
  re-run never double-counts.
