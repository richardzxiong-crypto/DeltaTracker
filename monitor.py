"""Delta SkyMiles flash-sale monitor.

Polls the RSS feeds of the major award-deal blogs, looks for new posts
about Delta award/flash sales, and emails an alert for anything unseen.
Stdlib only — no pip installs. State lives in seen.json (committed back
to the repo by the GitHub Actions workflow).
"""

import json
import os
from datetime import datetime, timedelta, timezone
import re
import smtplib
import ssl
import urllib.request
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from email.message import EmailMessage
from pathlib import Path

FEEDS = [
    "https://frequentmiler.com/feed/",
    "https://thriftytraveler.com/feed/",
    "https://loyaltylobby.com/feed/",
    "https://onemileatatime.com/feed/",
    "https://upgradedpoints.com/feed/",
]

# A post matches if it mentions Delta AND looks like an award sale, with
# the two close together: newsletter digests mention Delta in one blurb and
# "30k miles round-trip" about another airline in the next, and that must
# not fire. SkyMiles is Delta's programme, so it counts as a Delta mention.
DELTA = re.compile(r"\bdelta\b|skymiles", re.I)
SENTENCE = re.compile(r"(?<=[.!?])\s+|\n+")
PROXIMITY = 60  # fallback: chars either side of a sale phrase to look for Delta
# Buying miles is a different product from an award sale and, at Delta's
# usual ~2.5c/mile, a bad one under the cents-per-mile rule in the alert.
BUY = re.compile(r"\bbuy(ing)? (delta )?(skymiles|miles)\b|\b(skymiles|miles) purchase\b", re.I)
SALE = re.compile(
    r"flash sale|award sale|award flash|skymiles award|"
    r"skymiles (sale|deal|flash|discount)|award (deal|discount)|"
    r"discounted (award|skymiles)|"
    r"award (price|rate)s? (drop|cut)|(drops?|cuts?) award (price|rate)|"
    r"miles? round-?trip|"
    r"\b(from|as low as|as little as)( just| only)? [\d,]+k? (skymiles|miles)\b",
    re.I,
)

SEEN_PATH = Path(__file__).parent / "seen.json"
SEEN_CAP = 1000  # ~2 weeks of posts at the observed ~70/day
STATE_PATH = Path(__file__).parent / "state.json"
HEARTBEAT_EVERY = timedelta(hours=20)  # lands once a day despite cron jitter
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
        published = None
        try:
            published = parsedate_to_datetime(item.findtext("pubDate") or "")
            if published.tzinfo is None:
                published = published.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            pass
        if title and link:
            yield title, link, desc, published


def age(published) -> str:
    """'published 2h 14m ago', or '' when the feed gave no date."""
    if not published:
        return ""
    mins = int((datetime.now(timezone.utc) - published).total_seconds() // 60)
    return f"published {mins // 60}h {mins % 60:02d}m ago"


def why(title: str, desc: str):
    """Reason a post looks like a Delta award sale, or None.

    A sale phrase in a title that also names Delta is the normal case and
    is accepted outright. In the body, the sale phrase and the Delta
    mention must share a sentence, or sit within PROXIMITY characters of
    each other, which keeps multi-item digests from firing.
    """
    if BUY.search(title):
        return None
    if DELTA.search(title) and (m := SALE.search(title)):
        return f"title says {m.group(0)!r}"
    for sentence in SENTENCE.split(desc):
        if DELTA.search(sentence) and (m := SALE.search(sentence)):
            return f"body says {m.group(0)!r} in the same sentence as Delta"
    for m in SALE.finditer(desc):
        window = desc[max(0, m.start() - PROXIMITY): m.end() + PROXIMITY]
        if DELTA.search(window):
            return f"body says {m.group(0)!r} within {PROXIMITY} chars of Delta"
    return None


def matches(title: str, desc: str) -> bool:
    return why(title, desc) is not None


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


def load_state() -> dict:
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text())
    return {"last_heartbeat": None, "runs": 0, "posts": 0}


def save_state(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state, indent=1) + "\n")


def ana_status() -> str:
    """One line on the ANA award watch, read from its own state file."""
    path = Path(__file__).parent / "ana_state.json"
    if not path.exists():
        return "ANA watch: has not run yet."
    st = json.loads(path.read_text())
    if not st.get("configured"):
        return "ANA watch: not configured (add the SEATS_AERO_KEY secret to enable it)."
    now = datetime.now(timezone.utc)
    checked = datetime.fromisoformat(st["last_check"])
    hours = (now - checked).total_seconds() / 3600
    last_alert = st.get("last_alert")
    alert = (f"last alert {(now - datetime.fromisoformat(last_alert)).days}d ago"
             if last_alert else "no alert sent yet")
    return (f"ANA watch: checked {hours:.0f}h ago, {st.get('seats_seen', 0)} ANA business "
            f"date(s) open NYC<->Japan, {alert}.")


def heartbeat(state: dict, new_posts: int, sales: int, failed: list) -> None:
    """Send a short daily proof-of-life.

    A watch that only emails on a match is silent for weeks at a time, and
    from the inbox that silence looks exactly like a watch that has died.
    A heartbeat that stops arriving is the signal that something is wrong.
    """
    state["runs"] += 1
    state["posts"] += new_posts
    state["sales"] = state.get("sales", 0) + sales
    save_state(state)  # counters persist even if the send below fails

    now = datetime.now(timezone.utc)
    last = state.get("last_heartbeat")
    if last and now - datetime.fromisoformat(last) < HEARTBEAT_EVERY:
        return

    feeds = (
        f"{len(FEEDS) - len(failed)} of {len(FEEDS)} feeds readable"
        + (f" (unreadable: {', '.join(failed)})" if failed else "")
    )
    n = state["sales"]
    _send(
        "Delta award sale watch: daily heartbeat, "
        + (f"{n} sale alert(s) sent" if n else "no sale yet"),
        f"The watch is alive as of {now:%Y-%m-%d %H:%M} UTC.\n\n"
        f"Since the last heartbeat: {state['runs']} check(s), "
        f"{state['posts']} new post(s) scanned, {n} Delta award sale(s) found.\n"
        f"Feeds: {feeds}.\n"
        f"{ana_status()}\n\n"
        "A matching post triggers a separate 'Delta award sale alert' email "
        "immediately. If this heartbeat stops arriving, the watch is down.",
    )
    state.update(last_heartbeat=now.isoformat(), runs=0, posts=0, sales=0)
    save_state(state)
    print("heartbeat sent")


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


def run_diagnosis() -> None:
    """Print how every live post that mentions Delta looks to the matcher.

    Reads the feeds, ignores seen.json, sends nothing and saves nothing.
    A post tagged loose-only has a sale phrase somewhere in its text but
    not near a Delta mention: the digest case the proximity rule exists
    to reject. Use it to answer "what would fire right now, and why?".
    """
    for feed in FEEDS:
        try:
            items = list(fetch_items(feed))
        except Exception as e:
            print(f"UNREADABLE {feed}: {e}")
            continue
        for title, link, desc, published in items:
            blob = f"{title} {desc}"
            if not DELTA.search(blob):
                continue
            reason = why(title, desc)
            if reason:
                tag = "ALERT     "
            elif SALE.search(blob):
                tag = "loose-only"
                reason = "sale phrase present but not near a Delta mention"
            else:
                tag = "quiet     "
                reason = "no sale phrase"
            print(f"[{tag}] {title}\n             {link}\n             {reason}; {age(published)}")


def run_drill() -> None:
    """Push a known-good sale post through the real matching and alert path.

    Reads a committed fixture instead of the live feeds and never touches
    seen.json, so the end-to-end wiring can be exercised on demand without
    waiting for a sale or disturbing the baseline.
    """
    hits = [
        (title, link)
        for title, link, desc, _ in fetch_items(DRILL_PATH.as_uri())
        if matches(title, desc)
    ]
    if not hits:
        raise SystemExit("drill failed: fixture sale post did not match")
    print(f"drill: {len(hits)} fixture post(s) matched, sending a real alert")
    send_email(hits)


def main() -> None:
    if os.environ.get("DIAGNOSE") == "true":
        run_diagnosis()
        return

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
    new_posts = 0
    for feed in FEEDS:
        try:
            for title, link, desc, published in fetch_items(feed):
                if link in seen:
                    continue
                seen[link] = None
                new_posts += 1
                reason = why(title, desc)
                if reason:
                    print(f"match: {title}\n       {link}\n       {reason}; {age(published)}")
                    hits.append((title, link))
        except Exception as e:  # one dead feed shouldn't lose the others
            print(f"warn: {feed}: {e}")
            failed.append(feed)

    if first_run:
        save_seen(seen)
        print(f"first run: seeded {len(seen)} item(s), no alert sent")
        heartbeat(load_state(), new_posts, 0, failed)
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
    heartbeat(load_state(), new_posts, len(hits), failed)
    report_failures(failed)


if __name__ == "__main__":
    main()
