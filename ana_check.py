"""On-demand ANA business award check: New York <-> Tokyo / Osaka via united.com.

Run from the Actions tab with a date range. For each date it loads United's
award search once (no login: United shows Star Alliance partner space,
which is the inventory ANA releases to partners) and reports every flight
with an ANA-operated business-class award. Results go to the job log, an
artifact of raw responses, and one email.

Needs Playwright (installed by the workflow); everything else is stdlib.
"""

import json
import os
import re
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

from monitor import _send

ORIGIN = os.environ.get("ORIGIN") or "NYC"
DESTS = [d.strip().upper() for d in (os.environ.get("DESTS") or "TYO,OSA").split(",") if d.strip()]
DIRECTION = (os.environ.get("DIRECTION") or "both").lower()
PROBE = os.environ.get("PROBE") == "true"
MAX_DAYS = 31
PAUSE = 4.0            # seconds between searches: polite, and less bot-like
GIVE_UP_AFTER = 3      # consecutive failed searches before aborting
CARRIER = "NH"
ONE_WAY_MAX = 65_000
ROUND_TRIP_MAX = 135_000
ANA_OWN = "ANA Mileage Club prices this at 75k-90k round trip by season"
OUT_DIR = Path(__file__).parent / "ana-check-output"

SEARCH_URL = ("https://www.united.com/en/us/fsr/choose-flights"
              "?f={frm}&t={to}&d={day}&tt=1&at=1&px=1&taxng=1&newHP=True&clm=7&st=bestmatches")
HOME_URL = "https://www.united.com/en/us"
FLIGHTS_API = "FetchFlights"

# Tried in order until the first search succeeds. The headless *shell* binary
# has a browser fingerprint bot managers recognise; the full Chromium in new
# headless mode (channel="chromium") mostly does not. ERR_HTTP2_PROTOCOL_ERROR
# on first contact is the HTTP/2 fingerprint being rejected, so later rungs
# fall back to HTTP/1.1. No user-agent override: a UA that disagrees with the
# real binary version is itself a tell.
LAUNCH_LADDER = [
    ("full-chromium", dict(channel="chromium", args=["--disable-blink-features=AutomationControlled"])),
    ("full-chromium-http1", dict(channel="chromium", args=["--disable-blink-features=AutomationControlled", "--disable-http2"])),
    ("headless-shell-http1", dict(args=["--disable-blink-features=AutomationControlled", "--disable-http2"])),
]


def date_range() -> list:
    start = date.fromisoformat(os.environ["START_DATE"])
    end = date.fromisoformat(os.environ["END_DATE"])
    if end < start:
        raise SystemExit("END_DATE is before START_DATE")
    if (end - start).days + 1 > MAX_DAYS:
        raise SystemExit(f"range is {(end - start).days + 1} days; the cap is {MAX_DAYS} per run")
    return [start + timedelta(days=i) for i in range((end - start).days + 1)]


def legs() -> list:
    out = [(ORIGIN, d) for d in DESTS]
    back = [(d, ORIGIN) for d in DESTS]
    return {"out": out, "return": back}.get(DIRECTION, out + back)


def text_of(obj) -> str:
    """Lower-cased join of every string value in a nested object."""
    if isinstance(obj, dict):
        return " ".join(text_of(v) for v in obj.values())
    if isinstance(obj, list):
        return " ".join(text_of(v) for v in obj)
    return str(obj).lower() if isinstance(obj, str) else ""


def miles_of(product: dict):
    for p in product.get("Prices") or []:
        if str(p.get("Currency", "")).upper() in ("MILES", "MILE", "PTS", "POINTS"):
            try:
                return int(float(p.get("Amount") or 0))
            except (TypeError, ValueError):
                pass
    for k in ("Miles", "MileageCost", "Amount"):
        v = product.get(k)
        if isinstance(v, (int, float)) and v > 0:
            return int(v)
    return None


def business_awards(flight: dict) -> list:
    """Business-class award products on one itinerary, as (miles, label)."""
    found = []
    for prod in flight.get("Products") or []:
        blob = text_of({k: v for k, v in prod.items() if k != "Prices"})
        if not any(w in blob for w in ("business", "polaris")):
            continue
        if "premium" in blob and "business" not in blob:
            continue
        miles = miles_of(prod)
        if miles:
            label = prod.get("Description") or prod.get("ProductType") or "business"
            found.append((miles, str(label)))
    return found


def segments(flight: dict) -> list:
    """Operating carrier and flight number for each segment, in order."""
    segs = [flight] + list(flight.get("Connections") or [])
    return [(s.get("OperatingCarrier") or s.get("MarketingCarrier") or "?",
             str(s.get("FlightNumber") or "?"),
             s.get("Origin") or "?", s.get("Destination") or "?") for s in segs]


def parse(payload: dict) -> list:
    """ANA business awards in one FetchFlights response."""
    trips = (payload.get("data") or payload).get("Trips") or []
    rows = []
    for trip in trips:
        for fl in trip.get("Flights") or []:
            segs = segments(fl)
            if not any(carrier == CARRIER for carrier, *_ in segs):
                continue
            awards = business_awards(fl)
            if not awards:
                continue
            miles, label = min(awards)
            rows.append({
                "flights": " + ".join(f"{c}{n}" for c, n, _, _ in segs),
                "route": " > ".join([segs[0][2]] + [s[3] for s in segs]),
                "nonstop": len(segs) == 1,
                "all_ana": all(c == CARRIER for c, *_ in segs),
                "depart": (fl.get("DepartDateTime") or fl.get("DepartTimeLocal") or "")[:16],
                "miles": miles,
                "label": label,
            })
    return rows


def search(page, frm: str, to: str, day: date, api_log: list) -> dict:
    """Load one award search and capture United's flight-list API response.

    Every /api/ response seen during the load is appended to api_log as
    (status, url), so a failed search still shows what the site called.
    """
    url = SEARCH_URL.format(frm=frm, to=to, day=day.isoformat())

    def on_response(r):
        if "/api/" in r.url:
            api_log.append((r.status, r.url[:160]))

    page.on("response", on_response)
    try:
        with page.expect_response(lambda r: FLIGHTS_API in r.url and r.status == 200, timeout=75_000) as got:
            page.goto(url, wait_until="domcontentloaded", timeout=75_000)
        return got.value.json()
    finally:
        page.remove_listener("response", on_response)


def open_browser(pw, out_dir: Path):
    """Walk the launch ladder; return (name, browser, page) for the first rung
    whose homepage visit and warm-up succeed, else raise with every error."""
    errors = []
    for name, opts in LAUNCH_LADDER:
        browser = pw.chromium.launch(headless=True, **opts)
        try:
            ctx = browser.new_context(viewport={"width": 1366, "height": 900}, locale="en-US",
                                      timezone_id="America/New_York")
            page = ctx.new_page()
            resp = page.goto(HOME_URL, wait_until="domcontentloaded", timeout=60_000)
            page.wait_for_timeout(3_000)  # let the bot-manager sensor script run and set its cookies
            status = resp.status if resp else None
            hint = blocked(page)
            print(f"[{name}] homepage HTTP {status}, title={page.title()[:60]!r}, ua={page.evaluate('navigator.userAgent')[:90]!r}"
                  + (f", page says: {hint}" if hint else ""))
            if status and status < 400 and not hint:
                return name, browser, page
            errors.append(f"{name}: HTTP {status}" + (f" ({hint})" if hint else ""))
            page.screenshot(path=str(out_dir / f"home-{name}.png"))
        except Exception as e:
            errors.append(f"{name}: {type(e).__name__}: {str(e)[:140]}")
            print(f"[{name}] failed: {errors[-1]}")
        browser.close()
    raise RuntimeError("united.com refused every browser configuration: " + " | ".join(errors))


def blocked(page) -> str:
    """Text hinting at a bot block on the current page, or ''."""
    try:
        body = page.inner_text("body")[:2000].lower()
    except Exception:
        return ""
    for hint in ("access denied", "pardon our interruption", "unusual traffic", "verify you are human", "reference #"):
        if hint in body:
            return hint
    return ""


def report(results: list, errors: list, days: list) -> tuple:
    hits = [(d, frm, to, rows) for d, frm, to, rows in results if rows]
    span = f"{days[0]} to {days[-1]}"
    subject = f"ANA business award check: {len(hits)} of {len(results)} searches found ANA business space ({span})"
    lines = [f"Checked {len(results)} search(es) on united.com, {span}, {ORIGIN} <-> {', '.join(DESTS)}.",
             f"{ANA_OWN}, so any seat below is under the {ROUND_TRIP_MAX:,} round-trip threshold. "
             f"Miles shown are what United charges for the same seat.\n"]
    for d, frm, to, rows in hits:
        lines.append(f"{d}  {frm} -> {to}")
        for r in sorted(rows, key=lambda r: (not r["nonstop"], r["miles"])):
            kind = "nonstop" if r["nonstop"] else ("all-ANA connection" if r["all_ana"] else "connection with ANA leg")
            flag = "  <= 65k one-way" if r["miles"] <= ONE_WAY_MAX else ""
            lines.append(f"  {r['flights']:<14} {r['route']:<16} {r['depart'][11:16] or '':<6} {kind:<24} {r['miles']:>7,} via United{flag}")
        lines.append("")
    if not hits:
        lines.append("No ANA-operated business award space on any searched date.\n")
    outs = [min(r["miles"] for r in rows) for d, frm, to, rows in hits if frm == ORIGIN]
    rets = [min(r["miles"] for r in rows) for d, frm, to, rows in hits if to == ORIGIN]
    if outs and rets:
        total = min(outs) + min(rets)
        lines.append(f"Cheapest United round trip in this range: {min(outs):,} + {min(rets):,} = {total:,} "
                     f"({'under' if total <= ROUND_TRIP_MAX else 'over'} the {ROUND_TRIP_MAX:,} threshold).\n")
    if errors:
        lines.append(f"{len(errors)} search(es) could not be completed:")
        lines += [f"  {d} {frm}->{to}: {why}" for d, frm, to, why in errors]
    return subject, "\n".join(lines)


def main() -> None:
    from playwright.sync_api import sync_playwright

    days = date_range()
    plan = [(d, frm, to) for d in days for frm, to in legs()]
    if PROBE:
        plan = plan[:1]
    OUT_DIR.mkdir(exist_ok=True)
    print(f"{len(plan)} search(es): {ORIGIN} <-> {DESTS}, {days[0]}..{days[-1]}, direction={DIRECTION}")

    results, errors, streak = [], [], 0
    with sync_playwright() as pw:
        profile, browser, page = open_browser(pw, OUT_DIR)
        print(f"using browser profile: {profile}")
        for i, (d, frm, to) in enumerate(plan):
            if i:
                time.sleep(PAUSE)
            api_log = []
            try:
                payload = search(page, frm, to, d, api_log)
            except Exception as e:
                hint = blocked(page)
                why = f"{type(e).__name__}: {str(e)[:120]}" + (f" (page says: {hint})" if hint else "")
                print(f"FAIL {d} {frm}->{to}: {why}")
                if api_log:
                    print("  api calls seen:", *[f"\n    {st} {u}" for st, u in api_log[:25]])
                else:
                    print("  no /api/ responses observed; page title:", repr(page.title()[:80]))
                try:
                    page.screenshot(path=str(OUT_DIR / f"fail-{d}-{frm}-{to}.png"))
                except Exception:
                    pass
                errors.append((d, frm, to, why)); streak += 1
                if streak >= GIVE_UP_AFTER:
                    print(f"aborting after {streak} consecutive failures")
                    break
                continue
            streak = 0
            (OUT_DIR / f"{d}-{frm}-{to}.json").write_text(json.dumps(payload)[:2_000_000])
            if PROBE:
                print("api calls seen:", *[f"\n    {st} {u}" for st, u in api_log[:25]])
                trips = (payload.get("data") or payload).get("Trips") or []
                flights = trips[0].get("Flights") if trips else []
                print("top-level keys:", list(payload)[:20])
                print("data keys:", list((payload.get("data") or {}))[:30])
                print(f"trips: {len(trips)}, flights in first trip: {len(flights or [])}")
                for fl in (flights or [])[:3]:
                    print("--- flight ---")
                    print(json.dumps({k: v for k, v in fl.items() if k != "Products"}, default=str)[:1500])
                    print("products:", json.dumps(fl.get("Products"), default=str)[:2500])
                print("carriers seen:", sorted({fl.get("OperatingCarrier") for fl in (flights or [])}))
            rows = parse(payload)
            print(f"{d} {frm}->{to}: {len(rows)} ANA business option(s)")
            for r in rows:
                print("   ", r)
            results.append((d, frm, to, rows))
        browser.close()

    if PROBE:
        print("probe complete; no email sent")
        return
    subject, body = report(results, errors, days)
    print("\n" + body)
    _send(subject, body)
    print("\nemail sent")
    if errors and not results:
        raise SystemExit("every search failed; see errors above")


if __name__ == "__main__":
    main()
