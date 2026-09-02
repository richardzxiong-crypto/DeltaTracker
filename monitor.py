"""Delta SkyMiles flash-sale monitor.

Polls the RSS feeds of the major award-deal blogs, looks for new posts
about Delta award/flash sales, and emails an alert for anything unseen.
Stdlib only — no pip installs. State lives in seen.json (committed back
to the repo by the GitHub Actions workflow).
"""

import json
import os
import re
import smtplib
import ssl
import urllib.request
import xml.etree.ElementTree as ET
from email.message import EmailMessage
from pathlib import Path

FEEDS = [
    "https://frequentmiler.com/feed/",
    "https://thriftytraveler.com/feed/",
    "https://loyaltylobby.com/feed/",
    "https://onemileatatime.com/feed/",
    "https://upgradedpoints.com/feed/",
]

# A post matches if it mentions Delta AND looks like an award sale.
DELTA = re.compile(r"\bdelta\b", re.I)
SALE = re.compile(
    r"flash sale|award sale|skymiles (sale|deal|flash)|award flash|"
    r"skymiles award|miles? round-?trip",
    re.I,
)

SEEN_PATH = Path(__file__).parent / "seen.json"
UA = {"User-Agent": "Mozilla/5.0 (delta-watch personal monitor)"}


def load_seen() -> set:
    if SEEN_PATH.exists():
        return set(json.loads(SEEN_PATH.read_text()))
    return set()


def save_seen(seen: set) -> None:
    # keep the file bounded
    SEEN_PATH.write_text(json.dumps(sorted(seen)[-500:], indent=0))


def fetch_items(url: str):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=30) as r:
        root = ET.fromstring(r.read())
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        desc = (item.findtext("description") or "")[:500]
        if title and link:
            yield title, link, desc


def matches(title: str, desc: str) -> bool:
    blob = f"{title} {desc}"
    return bool(DELTA.search(blob) and SALE.search(blob))


def _send(subject: str, body: str) -> None:
    user = os.environ["SMTP_USER"]
    password = os.environ["SMTP_PASS"]
    # A secret that exists but is empty arrives as "", which os.environ.get
    # would hand back instead of the default, so fall back on falsiness.
    to = os.environ.get("ALERT_TO") or user
    host = os.environ.get("SMTP_HOST") or "smtp.gmail.com"
    port = int(os.environ.get("SMTP_PORT") or "465")

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = to
    msg.set_content(body)

    with smtplib.SMTP_SSL(host, port, context=ssl.create_default_context()) as s:
        s.login(user, password)
        s.send_message(msg)


PROFILE_REMINDER = (
    "\nReminder: NYC profile is JFK/LGA to Europe/Asia/South America, "
    "Main Cabin or better. Flash sales usually last ~72 hours \u2014 check "
    "delta.com/us/en/flight-deals/flash-sale and price it in cents per "
    "mile (cash / miles x 100; 1.2+ good, 1.5+ book it)."
)


def send_email(hits: list) -> None:
    n = len(hits)
    lines = ["New Delta award-sale coverage spotted:\n"]
    for title, link in hits:
        lines.append(f"- {title}\n  {link}\n")
    lines.append(PROFILE_REMINDER)
    _send(
        f"Delta award sale alert: {n} new post{'s' if n > 1 else ''}",
        "\n".join(lines),
    )


def send_test_email() -> None:
    """Exercise the SMTP path on demand, so the credentials can be proven
    without waiting for a real sale to appear in the feeds."""
    _send(
        "Delta award sale watch: test alert",
        "This is a test from your Delta award sale watch.\n\n"
        "If you are reading this, the SMTP credentials work and a real "
        "alert will reach you the same way.\n" + PROFILE_REMINDER,
    )


def main() -> None:
    if os.environ.get("TEST_EMAIL") == "true":
        send_test_email()
        print("test alert sent \u2014 check the inbox for ALERT_TO")
        return

    # No seen.json yet means this is the baseline run: record what is already
    # live and stay quiet, so alerts only fire for posts published after it.
    first_run = not SEEN_PATH.exists()

    seen = load_seen()
    hits = []
    for feed in FEEDS:
        try:
            for title, link, desc in fetch_items(feed):
                if link in seen:
                    continue
                seen.add(link)
                if matches(title, desc):
                    hits.append((title, link))
        except Exception as e:  # one dead feed shouldn't kill the run
            print(f"warn: {feed}: {e}")

    if first_run:
        save_seen(seen)
        print(f"first run: seeded {len(seen)} item(s), no alert sent")
        return

    if hits:
        print(f"{len(hits)} new Delta sale post(s) — sending alert")
        try:
            send_email(hits)
        except Exception:
            # Alert never went out — un-see those posts so the next run retries
            # instead of silently swallowing the sale.
            seen.difference_update(link for _, link in hits)
            save_seen(seen)
            raise
    else:
        print("no new Delta sale posts")
    save_seen(seen)


if __name__ == "__main__":
    main()
