"""ANA business-class award watch: New York <-> Tokyo / Osaka.

Asks the seats.aero Partner API for Star Alliance partner award space on
ANA-operated flights and emails when business-class saver seats appear.
ANA's own programme prices this route at a fixed 75k-90k SkyMiles-equivalent
round trip by season, so the scarce thing is the seat, not the price: any
new ANA business seat is reported, with each partner programme's price set
against the one-way and round-trip thresholds.

Stdlib only. State lives in ana_state.json, committed by the workflow.
Needs the SEATS_AERO_KEY secret (seats.aero Pro); without it the check is
skipped with a notice rather than failing the run.
"""

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from monitor import _send

API = "https://seats.aero/partnerapi/search"
AUTH_HEADER = os.environ.get("SEATS_AERO_AUTH_HEADER") or "Partner-Authorization"
UA = {"User-Agent": "Mozilla/5.0 (delta-watch personal monitor)"}

NYC = ["JFK", "EWR"]
JAPAN = ["HND", "NRT", "KIX"]
CARRIER = "NH"                 # ANA
ONE_WAY_MAX = 65_000           # partner-programme miles, one way
ROUND_TRIP_MAX = 135_000       # partner-programme miles, out + back
ANA_OWN_RT = "75k-90k round trip via ANA Mileage Club (by season)"
DAYS_AHEAD = 330
PAGE = 1000
REALERT_AFTER = timedelta(hours=48)  # a seat that vanished and returned is news again
MAX_LINES = 40

STATE_PATH = Path(__file__).parent / "ana_state.json"
FIXTURE_PATH = Path(__file__).parent / "tests" / "ana-fixture.json"


def load_state() -> dict:
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text())
    return {"configured": False, "alerted": {}}


def save_state(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state, indent=1, sort_keys=True) + "\n")


def fetch(origins: list, dests: list, key: str) -> list:
    """All cached-search records for the airport sets, following the cursor."""
    today = date.today()
    params = {
        "origin_airport": ",".join(origins),
        "destination_airport": ",".join(dests),
        "cabin": "business",
        "start_date": today.isoformat(),
        "end_date": (today + timedelta(days=DAYS_AHEAD)).isoformat(),
        "take": PAGE,
    }
    records = []
    while True:
        url = f"{API}?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(url, headers={AUTH_HEADER: key, "Accept": "application/json", **UA})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                body = json.load(r)
        except urllib.error.HTTPError as e:
            head = e.read(300).decode("utf-8", "replace")
            raise RuntimeError(f"seats.aero HTTP {e.code} for {origins}->{dests}: {head}") from None
        records.extend(body.get("data") or [])
        if not body.get("hasMore") or body.get("cursor") in (None, 0, ""):
            return records
        params["cursor"] = body["cursor"]


def seat(rec: dict):
    """Normalise one record to an ANA business seat, or None."""
    if not rec.get("JAvailable"):
        return None
    airlines = [a.strip() for a in (rec.get("JAirlines") or "").split(",") if a.strip()]
    if CARRIER not in airlines:
        return None
    cost = rec.get("JMileageCostRaw")
    if not cost:
        try:
            cost = int(str(rec.get("JMileageCost") or "0").replace(",", ""))
        except ValueError:
            cost = 0
    route = rec.get("Route") or {}
    return {
        "date": str(rec.get("Date") or "")[:10],
        "origin": route.get("OriginAirport") or "",
        "dest": route.get("DestinationAirport") or "",
        "source": rec.get("Source") or route.get("Source") or "?",
        "cost": cost,
        "seats": rec.get("JRemainingSeats"),
        "nonstop": bool(rec.get("JDirect")) or airlines == [CARRIER],
        "airlines": airlines,
    }


def key_of(s: dict) -> str:
    # One key per date and route: the same seat shows up once per partner
    # programme that can see it, and that is one seat, not several.
    return f"{s['date']}|{s['origin']}|{s['dest']}"


def cheapest_per_date(seats: list) -> dict:
    """{(date, origin, dest): seat with the lowest partner cost}."""
    best = {}
    for s in seats:
        k = (s["date"], s["origin"], s["dest"])
        if k not in best or (s["cost"] or 10**9) < (best[k]["cost"] or 10**9):
            best[k] = s
    return best


def line(s: dict) -> str:
    kind = "nonstop" if s["nonstop"] else "+".join(s["airlines"])
    seats = f", {s['seats']} seat(s)" if s.get("seats") else ""
    cost = f"{s['cost']:,} via {s['source']}" if s["cost"] else f"via {s['source']}"
    return f"- {s['date']}  {s['origin']}->{s['dest']}  {kind}{seats}  {cost}"


def compose(new_out: list, new_ret: list, all_out: list, all_ret: list) -> tuple:
    """Subject and body for an alert about newly seen seats."""
    n = len(new_out) + len(new_ret)
    subject = (f"ANA business award space: {len(new_out)} new NYC->Japan, "
               f"{len(new_ret)} new Japan->NYC")
    parts = [f"{n} new ANA business-class saver date(s) NYC <-> Tokyo/Osaka.\n",
             f"Every ANA seat here is bookable at {ANA_OWN_RT}, "
             f"under the {ROUND_TRIP_MAX:,} round-trip threshold. "
             "Partner-programme prices are shown per line.\n"]

    ow = [s for s in new_out + new_ret if s["cost"] and s["cost"] <= ONE_WAY_MAX]
    if ow:
        parts.append(f"One-way at or under {ONE_WAY_MAX:,} via a partner:")
        parts += [line(s) for s in sorted(ow, key=lambda s: s["date"])][:MAX_LINES]
        parts.append("")

    best_out = min((s["cost"] for s in all_out if s["cost"]), default=None)
    best_ret = min((s["cost"] for s in all_ret if s["cost"]), default=None)
    if best_out and best_ret:
        total = best_out + best_ret
        verdict = "under" if total <= ROUND_TRIP_MAX else "over"
        parts.append(f"Cheapest partner round trip right now: {best_out:,} out + {best_ret:,} back "
                     f"= {total:,} ({verdict} the {ROUND_TRIP_MAX:,} threshold).\n")

    for label, group in (("New NYC -> Japan:", new_out), ("New Japan -> NYC:", new_ret)):
        if group:
            parts.append(label)
            rows = sorted(group, key=lambda s: (not s["nonstop"], s["date"]))
            parts += [line(s) for s in rows[:MAX_LINES]]
            if len(rows) > MAX_LINES:
                parts.append(f"  ... and {len(rows) - MAX_LINES} more")
            parts.append("")
    parts.append("Book ANA flights with ANA Mileage Club (Amex MR transfers, ~2-3 days) "
                 "or the partner named on the line. Award space can vanish within hours.")
    return subject, "\n".join(parts)


def diagnose(records_out: list, records_ret: list) -> None:
    for label, recs in (("NYC -> Japan", records_out), ("Japan -> NYC", records_ret)):
        print(f"== {label}: {len(recs)} record(s) from seats.aero ==")
        if recs:
            print("first raw record:", json.dumps(recs[0])[:600])
        by_source = {}
        for r in recs:
            by_source.setdefault(r.get("Source") or "?", 0)
            by_source[r.get("Source") or "?"] += 1
        print("records per source:", by_source)
        seats = list(cheapest_per_date([s for s in map(seat, recs) if s]).values())
        print(f"ANA business dates (cheapest programme each): {len(seats)}")
        for s in sorted(seats, key=lambda s: (s["date"], s["origin"]))[:MAX_LINES]:
            print("  " + line(s))


def main() -> None:
    if os.environ.get("TEST_EMAIL") == "true" or os.environ.get("DRILL") == "true":
        print("ana: skipped (Delta test/drill run)")
        return

    key = os.environ.get("SEATS_AERO_KEY")
    now = datetime.now(timezone.utc)
    state = load_state()

    if os.environ.get("ANA_FIXTURE") == "true":
        fx = json.loads(FIXTURE_PATH.read_text())
        records_out, records_ret = fx["out"], fx["ret"]
    elif not key:
        state.update(configured=False, last_check=now.isoformat())
        save_state(state)
        print("ana: SEATS_AERO_KEY not set, check skipped. Add the secret to enable it.")
        return
    else:
        records_out = fetch(NYC, JAPAN, key)
        records_ret = fetch(JAPAN, NYC, key)

    if os.environ.get("DIAGNOSE") == "true":
        diagnose(records_out, records_ret)
        return

    all_out = list(cheapest_per_date([s for s in map(seat, records_out) if s]).values())
    all_ret = list(cheapest_per_date([s for s in map(seat, records_ret) if s]).values())
    seen_now = {key_of(s): now.isoformat() for s in all_out + all_ret}

    # Forget seats not seen for a while (or whose date has passed) so that a
    # seat which vanished and came back is reported again.
    alerted = state.get("alerted") or {}
    alerted = {
        k: t for k, t in alerted.items()
        if now - datetime.fromisoformat(t) < REALERT_AFTER and k[:10] >= date.today().isoformat()
    }
    new_out = [s for s in all_out if key_of(s) not in alerted]
    new_ret = [s for s in all_ret if key_of(s) not in alerted]

    state.update(
        configured=True,
        last_check=now.isoformat(),
        seats_seen=len(all_out) + len(all_ret),
        alerted={**alerted, **seen_now},
    )

    if new_out or new_ret:
        subject, body = compose(new_out, new_ret, all_out, all_ret)
        print(f"ana: {len(new_out) + len(new_ret)} new ANA business date(s), sending alert")
        try:
            _send(subject, body)
        except Exception:
            # Don't mark them alerted; the next run retries.
            for s in new_out + new_ret:
                state["alerted"].pop(key_of(s), None)
            save_state(state)
            raise
        state["last_alert"] = now.isoformat()
    else:
        print(f"ana: {len(all_out) + len(all_ret)} ANA business date(s) open, none new")
    save_state(state)


if __name__ == "__main__":
    main()
