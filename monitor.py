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
    r"flash sale|award sale|award flash|skymiles award|"
    r"skymiles (sale|deal|flash|discount)|award (deal|discount)|"
    r"discounted (award|skymiles)|"
    r"award (price|rate)s? (drop|cut)|(drops?|cuts?) award (price|rate)|"
    r"miles? round-?trip|"
    r"from [\d,]+k? (skymiles|miles)",
    re.I,
)

SEEN_PATH = Path(__file__).parent / "seen.json"
SEEN_CAP = 1000  # ~2 weeks of posts at the observed ~70/day
DRILL_PATH = Path(__file__).parent / "tests" / "drill-feed.xml"
UA = {"User-Agent": "Mozilla/5.0 (delta-watch personal monitor)"}


def load_seen() -> dict:
    # A dict preserves arrival order while still giving O(1) membership.
    if SEEN_PATH.exists():
        return dict.fromkeys(json.loads(SEEN_PATH.read_text()))
    return {}


def save_seen(seen: dict) -> None:
    # Keep the file bounded by dropping the OLDEST entries. Trimming a sorted
    # list instead would evict by URL spelling, which quietly un-sees whole
    # domains once the cap is hit and then re-alerts on them every run.
    SEEN_PATH.write_text(json.dumps(list(seen)[-SEEN_CAP:], indent=0))


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


def report_failures(failed: list) -> None:
    """Fail the run when a feed could not be read.

    A blog that starts blocking us is the quietest way to miss a sale: the
    remaining feeds still succeed, so the job stays green while a source is
    no longer watched. Exiting non-zero turns that into a red run and the
    failure notification GitHub sends for it. State is already saved by the
    time this runs, so nothing is lost.
    """
    if failed:
        raise SystemExit(
            f"{len(failed)} of {len(FEEDS)} feeds unreadable: {', '.join(failed)}"
        )


def run_drill() -> None:
    """Push a known-good sale post through the real matching and alert path.

    Reads a committed fixture instead of the live feeds and never touches
    seen.json, so the end-to-end wiring can be exercised on demand without
    waiting for a sale or disturbing the baseline.
    """
    hits = [
        (title, link)
        for title, link, desc in fetch_items(DRILL_PATH.as_uri())
        if matches(title, desc)
    ]
    if not hits:
        raise SystemExit("drill failed: fixture sale post did not match")
    print(f"drill: {len(hits)} fixture post(s) matched, sending a real alert")
    send_email(hits)


def main() -> None:
    if os.environ.get("DRILL") == "true":
        run_drill()
        return

    if os.environ.get("TEST_EMAIL") == "true":
        send_test_email()
        print("test alert sent \u2014 check the inbox for ALERT_TO")
        return

    # No seen.json yet means this is the baseline run: record what is already
    # live and stay quiet, so alerts only fire for posts published after it.
    first_run = not SEEN_PATH.exists()

    seen = load_seen()
    hits = []
    failed = []
    for feed in FEEDS:
        try:
            for title, link, desc in fetch_items(feed):
                if link in seen:
                    continue
                seen[link] = None
                if matches(title, desc):
                    hits.append((title, link))
        except Exception as e:  # one dead feed shouldn't lose the others
            print(f"warn: {feed}: {e}")
            failed.append(feed)

    if first_run:
        save_seen(seen)
        print(f"first run: seeded {len(seen)} item(s), no alert sent")
        report_failures(failed)
        return

    if hits:
        print(f"{len(hits)} new Delta sale post(s) — sending alert")
        try:
            send_email(hits)
        except Exception:
            # Alert never went out — un-see those posts so the next run retries
            # instead of silently swallowing the sale.
            for _, link in hits:
                seen.pop(link, None)
            save_seen(seen)
            raise
    else:
        print("no new Delta sale posts")
    save_seen(seen)
    report_failures(failed)


if __name__ == "__main__":
    main()
