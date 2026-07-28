"""Alerts when a tracked rate drops.

Two delivery channels, both plain HTTP so there is no extra dependency. Whichever
is configured gets used; configure both and both fire. Everything is set through
environment variables (GitHub Actions secrets):

  ntfy - free phone push notification, no account needed
    NTFY_TOPIC            the topic name you chose - treat it as a password
    NTFY_SERVER           optional, defaults to https://ntfy.sh
    NTFY_TOKEN            optional, only for protected or self-hosted topics

  Twilio - a real SMS, costs a few pence per message
    TWILIO_ACCOUNT_SID    ACxxxxxxxx...
    TWILIO_AUTH_TOKEN     your auth token
    TWILIO_FROM           the Twilio number sending the text, e.g. +447700900123
    ALERT_TO              your mobile number, e.g. +447700900456

With neither configured the alert is printed to the log, so the tracker still
works end to end before any of this is set up.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

import requests

TWILIO_ENDPOINT = "https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json"
SMS_MAX_CHARS = 480  # ~3 concatenated SMS segments
DEFAULT_NTFY_SERVER = "https://ntfy.sh"


@dataclass
class Drop:
    name: str
    previous: float
    current: float
    record_low: bool = False

    @property
    def delta(self) -> float:
        return round(self.current - self.previous, 4)


def find_drops(history, tracked_ids, threshold_pp: float, flag_record_low: bool = True) -> list[Drop]:
    """Compare each tracked product's latest reading against the one before it."""
    from .store import latest_two

    drops: list[Drop] = []
    for product_id in tracked_ids:
        previous, current = latest_two(history, product_id)
        if previous is None or current is None:
            continue
        # 1e-9 tolerance so a threshold of exactly 0.01pp is not lost to float error.
        if previous.initial_rate - current.initial_rate < threshold_pp - 1e-9:
            continue
        earlier = [
            o.initial_rate
            for o in history
            if o.product_id == product_id and o.date < current.date
        ]
        drops.append(
            Drop(
                name=current.name,
                previous=previous.initial_rate,
                current=current.initial_rate,
                record_low=flag_record_low and bool(earlier) and current.initial_rate < min(earlier),
            )
        )
    return drops


def headline(drops: list[Drop]) -> str:
    return "Mortgage rate drop" if len(drops) == 1 else f"{len(drops)} mortgage rates dropped"


def compose(drops: list[Drop], chart_url: str | None = None,
            limit: int | None = SMS_MAX_CHARS, include_headline: bool = True) -> str:
    lines = [f"{headline(drops)} (first direct):"] if include_headline else []
    for drop in drops:
        low = " - record low" if drop.record_low else ""
        lines.append(f"- {drop.name}: {drop.previous:g}% -> {drop.current:g}% ({drop.delta:+g}pp){low}")
    if chart_url:
        lines.append(chart_url)
    message = "\n".join(lines)
    if limit and len(message) > limit:
        return message[: limit - 1] + "…"
    return message


def send_ntfy(body: str, title: str, click_url: str | None = None) -> bool:
    """Push a notification via ntfy. Returns True if it was actually sent."""
    topic = os.environ.get("NTFY_TOPIC")
    if not topic:
        return False

    server = (os.environ.get("NTFY_SERVER") or DEFAULT_NTFY_SERVER).rstrip("/")
    headers = {
        # Latin-1 is all the header encoding allows, so keep these plain ASCII.
        "Title": title.encode("ascii", "ignore").decode(),
        "Priority": "high",
        "Tags": "chart_with_downwards_trend",
    }
    if click_url:
        headers["Click"] = click_url
    token = os.environ.get("NTFY_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"

    response = requests.post(
        f"{server}/{topic}", data=body.encode("utf-8"), headers=headers, timeout=30
    )
    if response.status_code >= 400:
        raise RuntimeError(f"ntfy rejected the notification: HTTP {response.status_code}")
    print(f"Alert pushed via ntfy to {server}/***")
    return True


def send_sms(body: str) -> bool:
    """Send ``body`` as a text. Returns True if it was actually sent."""
    sid = os.environ.get("TWILIO_ACCOUNT_SID")
    token = os.environ.get("TWILIO_AUTH_TOKEN")
    sender = os.environ.get("TWILIO_FROM")
    recipient = os.environ.get("ALERT_TO")

    if not all([sid, token, sender, recipient]):
        return False

    response = requests.post(
        TWILIO_ENDPOINT.format(sid=sid),
        auth=(sid, token),
        data={"From": sender, "To": recipient, "Body": body},
        timeout=30,
    )
    if response.status_code >= 400:
        # Never print the response verbatim beyond its error message - it echoes
        # the request, and the log is public on a public repo.
        raise RuntimeError(f"Twilio rejected the message: HTTP {response.status_code}")
    print(f"Alert texted to {recipient[:-6]}****{recipient[-2:]}")
    return True


def send(drops: list[Drop], chart_url: str | None = None) -> list[str]:
    """Deliver an alert through every configured channel.

    Each channel is independent: one being unconfigured or failing must never
    stop the others, so a Twilio billing problem cannot silence the ntfy push.
    """
    title = f"{headline(drops)} (first direct)"
    delivered: list[str] = []

    channels = [
        # ntfy carries the chart as a tappable link rather than as body text.
        ("ntfy", lambda: send_ntfy(
            compose(drops, chart_url=None, limit=None, include_headline=False),
            title, chart_url)),
        ("sms", lambda: send_sms(compose(drops, chart_url))),
    ]
    for name, deliver in channels:
        try:
            if deliver():
                delivered.append(name)
        except Exception as exc:
            print(f"  {name} alert failed: {type(exc).__name__}: {exc}")

    if not delivered:
        print("No alert channel configured (set NTFY_TOPIC, or the Twilio secrets).")
        print("The alert would have said:\n" + compose(drops, chart_url))
    return delivered
