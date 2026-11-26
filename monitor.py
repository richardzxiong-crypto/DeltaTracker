"""Award-sale monitor for Delta SkyMiles and Virgin Atlantic Flying Club.

Polls the RSS feeds of the major award-deal blogs, looks for new posts
about award or reward sales in any watched programme, and emails one
combined alert for anything unseen. Stdlib only — no pip installs. State
lives in seen.json and state.json (committed back by the workflow).
"""

import json
import os
from datetime import datetime, timedelta, timezone
import re
import smtplib
import ssl
import urllib.request
from urllib.parse import urlsplit
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
    # UK-based, and the most thorough on Virgin Atlantic reward sales.
    "https://www.headforpoints.com/feed/",
]

# The programmes watched, each as (name in the alert, mention, exclusion).
# A post matches when one clause names a programme AND looks like an award
# sale: newsletter digests name one programme in a blurb and "30k miles
# round-trip" about another in the next, and that must not fire.
# "Virgin" alone is not a mention of Virgin Atlantic: Virgin Hotels, Voyages
# and Australia run sales of their own (a Virgin Hotels award sale is already
# among the posts recorded), so those count only with "Virgin Atlantic" named.
PROGRAMS = [
    ("Delta", re.compile(r"\bdelta\b|skymiles", re.I), None),
    ("Virgin Atlantic",
     re.compile(r"\bvirgin atlantic\b|\bflying club\b|\bvirgin points\b", re.I),
     re.compile(r"^(?!.*virgin atlantic).*\bvirgin (hotels?|voyages|australia)\b", re.I | re.S)),
]
MENTION = re.compile("|".join(f"(?:{m.pattern})" for _, m, _ in PROGRAMS), re.I)
SENTENCE = re.compile(r"(?<=[.!?])\s+|\n+")
# Headlines list several unrelated deals in one line: "Last Chance Deals: IHG
# award sale, Delta Amex Offer, Southwest 2X promo". Punctuation is what
# separates one programme's offer from the next, so a title counts only when
# Delta and the sale phrase land in the same piece.
CLAUSE = re.compile(r"[,;:|•·]|\s[-–—]\s")
# Buying miles or points is a different product from an award sale and, at
# the usual ~2.5c each, a bad one under the cents-per-mile rule in the alert.
BUY = re.compile(
    r"\bbuy(ing)? (delta |virgin )?(skymiles|miles|points|virgin points)\b|"
    r"\b(skymiles|miles|points) purchase\b", re.I)
# Delta calls them awards and Virgin calls them rewards, so most phrases take
# either word.
SALE = re.compile(
    r"flash sale|(award|reward) sale|(award|reward) flash|skymiles award|"
    r"reward seats? sale|half[- ]price rewards?|\b\d{1,2}% off (award|reward)s?\b|"
    r"skymiles (sale|deal|flash|discount)|(award|reward) (deal|discount)|"
    r"discounted (award|reward|skymiles)|"
    r"(award|reward) (price|rate)s? (drop|cut)|(drops?|cuts?) (award|reward) (price|rate)|"
    r"\b(miles?|points?) round-?trip|"
    r"\b(from|as low as|as little as)( just| only)? [\d,.]+k? (skymiles|miles|virgin points|points)\b",
    re.I,
)
# Airlines run cash fare sales far more often than award sales, and the
# blogs call both a flash sale. A money price with no award word anywhere in
# the headline is a cash sale, not the mileage deal this watch is for.
CASH = re.compile(r"[$£€]\s?\d|\b(usd|gbp|eur)\s?\d", re.I)
AWARDISH = re.compile(r"award|reward|points?\b|miles\b|skymiles|avios", re.I)

SEEN_PATH = Path(__file__).parent / "seen.json"
# Reading four pages of six feeds records about 100 posts a day, and a
# post that ages out of here while still on a feed page reads as new again.
SEEN_CAP = 3000
STATE_PATH = Path(__file__).parent / "state.json"
HEARTBEAT_EVERY = timedelta(hours=20)  # lands once a day despite cron jitter
FAIL_STREAK = 3  # consecutive unreadable runs before a feed counts as broken
# An RSS feed is a window on the newest ~10 posts, not an archive. These
# blogs publish 8-15 posts a day each and GitHub honours about 5 of the
# hourly slots, so roughly 5 hours pass between polls and the window turns
# over in between: posts fall off unseen. Walk back through older pages
# until one overlaps with posts already recorded.
FEED_PAGES = 4
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


def paged(feed: str, page: int) -> str:
    """The URL for an older page of a feed. WordPress serves ?paged=N."""
    if page <= 1:
        return feed
    return f"{feed}{'&' if '?' in feed else '?'}paged={page}"


def age(published) -> str:
    """'published 2h 14m ago', or '' when the feed gave no date."""
    if not published:
        return ""
    mins = int((datetime.now(timezone.utc) - published).total_seconds() // 60)
    return f"published {mins // 60}h {mins % 60:02d}m ago"


def programs_in(text: str, context: str = "") -> list:
    """Names of the watched programmes this text mentions.

    The exclusion is checked against the wider context too: in "Virgin
    Voyages flash sale: cabins from 40k Virgin Points" the clause holding
    the mention does not say Voyages, but the headline does.
    """
    return [name for name, mention, exclude in PROGRAMS
            if mention.search(text)
            and not (exclude and exclude.search(f"{context} {text}"))]


def label_programs(title: str) -> list:
    """Programmes named by a headline's leading label, as in "Virgin Atlantic:
    New York to London from 20k points", where the colon that separates a
    digest's items also separates the programme from its own deal.

    Only a label that is nothing but a programme name counts: "Delta
    roundup: Iberia award sale" must not lend Delta to Iberia's sale.
    """
    label, sep, _ = title.partition(":")
    if not sep:
        return []
    rest = MENTION.sub("", label)
    if re.sub(r"[\s'’s]+", "", rest):
        return []
    return programs_in(label, title)


def why(title: str, desc: str):
    """(programmes, reason) when a post looks like an award sale, else None.

    One rule throughout: the sale phrase and the programme mention must
    land in the same clause, where a clause is what punctuation separates
    in a title, and within a sentence in the body. A counted-characters
    fallback used to stand behind this and was what let "American flash
    sale ... In other news, Delta ..." match across the full stop.
    """
    if BUY.search(title):
        return None
    lent = label_programs(title)
    for where, text in [("title", title)] + [("body", s) for s in SENTENCE.split(desc)]:
        for i, clause in enumerate(CLAUSE.split(text)):
            progs = programs_in(clause, f"{title} {text}")
            if where == "title" and i == 1:  # the first piece after the label
                progs = [n for n, _, _ in PROGRAMS if n in progs or n in lent]
            m = SALE.search(clause) if progs else None
            if not m:
                continue
            if (CASH.search(title) or CASH.search(text)) and not (
                    AWARDISH.search(title) or AWARDISH.search(text)):
                continue  # a fare sale priced in money
            return progs, f"{where} says {m.group(0)!r} about {' and '.join(progs)}"
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
    "Main Cabin or better. Price any deal in cents per mile or point "
    "(cash fare / miles x 100; 1.2+ good, 1.5+ book it)."
)
PROGRAM_TIPS = {
    "Delta": "Delta flash sales usually last ~72 hours: "
             "delta.com/us/en/flight-deals/flash-sale.",
    "Virgin Atlantic": "Virgin Atlantic reward sales usually run a few days: "
                       "virginatlantic.com, Book with points. Virgin-operated "
                       "rewards add taxes and carrier charges, steep in Upper "
                       "Class, so price the cash part too.",
}


def send_email(hits: list) -> None:
    """One email for every programme, grouped by programme."""
    names = [name for name, _, _ in PROGRAMS if any(name in h[2] for h in hits)]
    lines = ["New award-sale coverage spotted:\n"]
    for name in names:
        group = [h for h in hits if name in h[2]]
        lines.append(f"{name} ({len(group)}):")
        for title, link, _ in group:
            lines.append(f"- {title}\n  {link}")
        lines.append("")
    lines.append(PROFILE_REMINDER.strip())
    lines += [PROGRAM_TIPS[name] for name in names if name in PROGRAM_TIPS]
    n = len(hits)
    _send(
        f"Award sale alert: {' + '.join(names)} "
        f"({n} new post{'s' if n > 1 else ''})",
        "\n".join(lines),
    )


def send_test_email() -> None:
    """Exercise the SMTP path on demand, so the credentials can be proven
    without waiting for a real sale to appear in the feeds."""
    _send(
        "Award sale watch: test alert",
        "This is a test from your award sale watch "
        f"({', '.join(name for name, _, _ in PROGRAMS)}).\n\n"
        "If you are reading this, the SMTP credentials work and a real "
        "alert will reach you the same way.\n" + PROFILE_REMINDER,
    )


def load_state() -> dict:
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text())
    return {"last_heartbeat": None, "runs": 0, "posts": 0}


def save_state(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state, indent=1) + "\n")


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

    streaks = state.get("feed_failures") or {}
    feeds = f"{len(FEEDS) - len(failed)} of {len(FEEDS)} feeds readable"
    if streaks:
        feeds += " (failing: " + ", ".join(
            f"{urlsplit(f).netloc or f} x{n}" for f, n in sorted(streaks.items())) + ")"
    n = state["sales"]
    _send(
        "Award sale watch: daily heartbeat, "
        + (f"{n} sale alert(s) sent" if n else "no sale yet"),
        f"The watch is alive as of {now:%Y-%m-%d %H:%M} UTC.\n\n"
        f"Since the last heartbeat: {state['runs']} check(s), "
        f"{state['posts']} new post(s) scanned, {n} award sale(s) found.\n"
        f"Watching: {', '.join(name for name, _, _ in PROGRAMS)}.\n"
        f"Feeds: {feeds}.\n\n"
        "A matching post triggers a separate 'Award sale alert' email "
        "immediately. If this heartbeat stops arriving, the watch is down.",
    )
    state.update(last_heartbeat=now.isoformat(), runs=0, posts=0, sales=0)
    save_state(state)
    print("heartbeat sent")


def note_failures(state: dict, failed: list) -> list:
    """Track consecutive unreadable runs per feed; return the broken ones.

    A feed reading once as a truncated page or an error body is normal
    internet weather. A feed unreadable run after run is a blog that has
    started refusing us, which is the quietest way to miss a sale.
    Counting consecutive failures separates the two.
    """
    # Drop feeds no longer in FEEDS, or an edit to the list would leave a
    # stale streak behind that fails every future run.
    streaks = {f: n for f, n in (state.get("feed_failures") or {}).items() if f in FEEDS}
    for feed in FEEDS:
        if feed in failed:
            streaks[feed] = streaks.get(feed, 0) + 1
        else:
            streaks.pop(feed, None)  # one good read clears the streak
    state["feed_failures"] = streaks
    return sorted(f for f, n in streaks.items() if n >= FAIL_STREAK)


def report_failures(failed: list, broken: list) -> None:
    """Fail the run only for a feed that is persistently unreadable.

    Exiting non-zero turns a red run and GitHub's failure notification into
    the signal that a source stopped being watched. Doing that for a single
    bad read would cry wolf several times a week, so it waits for
    FAIL_STREAK consecutive misses. State is saved before this runs.
    """
    for feed in failed:
        print(f"warn: unreadable this run: {feed}")
    if broken:
        raise SystemExit(
            f"{len(broken)} feed(s) unreadable {FAIL_STREAK} runs in a row: "
            + ", ".join(broken)
        )
    if failed:
        print(f"below the {FAIL_STREAK}-run threshold, not failing the run")


def run_diagnosis() -> None:
    """Print how every live post naming a watched programme looks to the matcher.

    Reads the feeds, ignores seen.json, sends nothing and saves nothing.
    A post tagged loose-only has a sale phrase somewhere in its text but
    not in the same clause as the programme: the digest case the clause
    rule exists to reject. Use it to answer "what would fire right now?".
    """
    for feed in FEEDS:
        items = []
        for page in range(1, FEED_PAGES + 1):
            url = paged(feed, page)
            try:
                got = list(fetch_items(url))
            except Exception as e:
                print(f"{'UNREADABLE' if page == 1 else 'no older page'} {url}: {e}")
                break
            links = {i[1] for i in items}
            fresh = [i for i in got if i[1] not in links]
            note = ""
            if not got:
                note = "  <- empty, end of the archive"
            elif not fresh:
                note = "  <- same posts as an earlier page, this feed ignores ?paged"
            print(f"  page {page}: {len(got)} item(s), {len(fresh)} not on earlier pages{note}")
            items += fresh
            if not got or not fresh:
                break
        print(f"{feed}: {len(items)} distinct post(s) across pages")
        for title, link, desc, published in items:
            blob = f"{title} {desc}"
            if not MENTION.search(blob):
                continue
            found = why(title, desc)
            if found:
                tag, reason = "ALERT     ", found[1]
            elif SALE.search(blob):
                tag = "loose-only"
                reason = "sale phrase present, not in the same clause as the programme"
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
        (title, link, found[0])
        for title, link, desc, _ in fetch_items(DRILL_PATH.as_uri())
        if (found := why(title, desc))
    ]
    if not hits:
        raise SystemExit("drill failed: fixture sale post did not match")
    print(f"drill: {len(hits)} fixture post(s) matched, sending a real alert")
    send_email(hits)


def seeded_feeds(state: dict, seen: dict) -> set:
    """Feeds whose posts have been recorded before, so alerts from them are news.

    A feed added to FEEDS later arrives with every post it carries unseen,
    and reading it for the first time would mail a backlog of old sales.
    Its first read is a silent baseline, as the very first run is. The list
    lives in state.json; when that has none yet, a feed counts as seeded if
    any recorded post is on its site.
    """
    if "feeds_seeded" in state:
        return set(state["feeds_seeded"])
    hosts = {urlsplit(u).netloc for u in seen}
    return {f for f in FEEDS if urlsplit(f).netloc in hosts}


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

    state = load_state()
    seen = load_seen()
    seeded = set() if first_run else seeded_feeds(state, seen)
    hits = []
    failed = []
    new_posts = 0
    for feed in FEEDS:
        baseline = first_run or feed not in seeded
        try:
            for page in range(1, FEED_PAGES + 1):
                url = paged(feed, page)
                try:
                    items = list(fetch_items(url))
                except Exception as e:
                    if page == 1:
                        raise  # page one failing means the feed is unreadable
                    print(f"note: older page unavailable, {url}: {e}")
                    break  # deeper pages are best effort
                fresh = [i for i in items if i[1] not in seen]
                for title, link, desc, published in fresh:
                    seen[link] = None
                    new_posts += 1
                    if baseline:
                        continue  # recorded as already known, never alerted
                    found = why(title, desc)
                    if found:
                        print(f"match: {title}\n       {link}\n       {found[1]}; {age(published)}")
                        hits.append((title, link, found[0]))
                if page > 1:
                    print(f"note: page {page} of {feed} held {len(fresh)} post(s) "
                          "the newest page had already dropped")
                # A page with nothing new means this run has caught up with
                # what was already recorded, so everything older is covered.
                if not items or len(fresh) < len(items):
                    break
            if baseline and not first_run:
                print(f"new feed {feed}: its current posts recorded as a baseline, no alerts from them")
            seeded.add(feed)
        except Exception as e:  # one dead feed shouldn't lose the others
            print(f"warn: {feed}: {e}")
            failed.append(feed)
    state["feeds_seeded"] = sorted(f for f in seeded if f in FEEDS)

    if first_run:
        save_seen(seen)
        print(f"first run: seeded {len(seen)} item(s), no alert sent")
        broken = note_failures(state, failed)
        heartbeat(state, new_posts, 0, failed)
        report_failures(failed, broken)
        return

    if hits:
        print(f"{len(hits)} new award sale post(s) — sending alert")
        try:
            send_email(hits)
        except Exception:
            # Alert never went out — un-see those posts so the next run retries
            # instead of silently swallowing the sale.
            for _, link, _ in hits:
                seen.pop(link, None)
            save_seen(seen)
            raise
    else:
        print("no new award sale posts")
    save_seen(seen)
    broken = note_failures(state, failed)
    heartbeat(state, new_posts, len(hits), failed)
    report_failures(failed, broken)


if __name__ == "__main__":
    main()
