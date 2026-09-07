"""Which award-search sites can this runner actually reach?

Loads each site's homepage and one search-style URL in a real browser and
records status, timing, and any bot-block text. Sends nothing, logs in
nowhere. Run it from the Actions tab (workflow "Probe award sites") or
locally with `python probe_sources.py` to compare a datacenter IP with a
home one.
"""

import time
from datetime import date, timedelta

DAY = (date.today() + timedelta(days=45)).isoformat()

SITES = [
    ("united (control)", "https://www.united.com/en/us",
     f"https://www.united.com/en/us/fsr/choose-flights?f=NYC&t=TYO&d={DAY}&tt=1&at=1&px=1&taxng=1&newHP=True&clm=7"),
    ("aeroplan", "https://www.aircanada.com/us/en/aco/home.html",
     f"https://www.aircanada.com/aeroplan/redeem/availability/outbound?org0=JFK&dest0=HND&departureDate0={DAY}&lang=en-CA&tripType=O&ADT=1&YTH=0&CHD=0&INF=0&INS=0&marketCode=INT"),
    ("lifemiles", "https://www.lifemiles.com/", "https://www.lifemiles.com/fly/find"),
    ("copa connectmiles", "https://www.copaair.com/en-us/", "https://www.copaair.com/en-us/star-alliance/"),
    ("ana (homepage only, no login)", "https://www.ana.co.jp/en/us/", None),
    ("google flights (control)", "https://www.google.com/travel/flights", None),
]
BLOCK_HINTS = ("access denied", "pardon our interruption", "unusual traffic", "verify you are human",
               "reference #", "request blocked", "captcha", "bot detection")
# A site that has not answered in this long is not going to. Kept short
# because a blocked host tarpits the connection rather than refusing it,
# and every one of those waits is paid twice, once per browser profile.
# Eighteen navigations at this cap is about three minutes, which keeps the
# all-blocked worst case well inside the step's ten-minute ceiling; a step
# killed by that ceiling would fail the run and mail the very notification
# this project just stopped sending.
VISIT_TIMEOUT = 10_000


def visit(page, url: str) -> str:
    t0 = time.time()
    try:
        resp = page.goto(url, wait_until="domcontentloaded", timeout=VISIT_TIMEOUT)
        page.wait_for_timeout(2_000)
        status = resp.status if resp else "?"
        title = ""
        try:
            title = page.title()[:60]
        except Exception:
            pass
        body = ""
        try:
            body = page.inner_text("body", timeout=5_000)[:3000].lower()
        except Exception:
            pass
        hint = next((h for h in BLOCK_HINTS if h in body), "")
        verdict = "BLOCKED" if hint or (isinstance(status, int) and status >= 400) else "ok"
        return f"{verdict:8} HTTP {status!s:4} {time.time() - t0:5.1f}s  title={title!r}" + (f"  page says: {hint!r}" if hint else "")
    except Exception as e:
        msg = str(e).splitlines()[0][:110]
        return f"{'FAILED':8} {time.time() - t0:5.1f}s  {type(e).__name__}: {msg}"


def main() -> None:
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        for name, opts in (("full-chromium", dict(channel="chromium")),
                           ("full-chromium-http1", dict(channel="chromium", args=["--disable-http2"]))):
            print(f"\n===== browser: {name} =====")
            browser = pw.chromium.launch(headless=True, args=opts.get("args", []) + ["--disable-blink-features=AutomationControlled"],
                                         channel=opts.get("channel"))
            ctx = browser.new_context(viewport={"width": 1366, "height": 900}, locale="en-US", timezone_id="America/New_York")
            # Bound every operation, not just navigation. page.title() takes no
            # timeout of its own and would otherwise fall back to Playwright's
            # 30s default on a page whose frame never settles, which is longer
            # than the navigation timeout and dominated the whole run.
            ctx.set_default_timeout(5_000)
            for site, home, search in SITES:
                # A fresh page per site: a navigation that a site kills at the
                # protocol level can linger and "interrupt" the next goto on
                # the same page, which would blame the wrong site.
                page = ctx.new_page()
                print(f"\n[{site}]")
                print(f"  home    {visit(page, home)}")
                if search:
                    print(f"  search  {visit(page, search)}")
                page.close()
            browser.close()


if __name__ == "__main__":
    main()
