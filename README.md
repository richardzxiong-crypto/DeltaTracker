# Delta Award Sale Watch

Emails you within a few hours whenever the major award-travel blogs
(Thrifty Traveler, Frequent Miler, Loyalty Lobby, One Mile at a Time,
Upgraded Points) publish coverage of a new Delta SkyMiles award or
flash sale, and sends a short daily heartbeat so you can tell "nothing
to report" apart from "not running". Runs free on GitHub Actions,
stdlib Python only.

## Setup (~10 minutes)

1. Create a new **private** GitHub repo.
2. Add `monitor.py` at the repo root and the workflow at
   `.github/workflows/delta-watch.yml`.
3. Create a Gmail **app password**: Google Account > Security >
   2-Step Verification > App passwords. (Regular passwords won't work.)
4. In the repo: Settings > Secrets and variables > Actions, add:
   - `SMTP_USER` — your Gmail address
   - `SMTP_PASS` — the 16-character app password
   - `ALERT_TO` — where alerts go (can be the same address, or a
     carrier email-to-SMS gateway if you want texts)
5. Test it: Actions tab > "Delta award sale watch" > Run workflow.
   First run seeds `seen.json` with current posts; alerts fire only
   for posts published after that.

## Verifying it works

- **Did a run succeed?** Actions tab > "Delta award sale watch". A green
  check is a clean run. The "Check feeds and alert" step prints either
  `no new Delta sale posts` or how many alerts it sent.
- **Is the state persisting?** `seen.json` should exist at the repo root,
  with `chore: update seen items` commits from `delta-watch-bot` as the
  feeds churn.
- **Is it still alive?** Expect one "daily heartbeat" email roughly every
  20-24 hours with how many checks ran and posts were scanned. If the
  heartbeat stops, the watch is down: open the Actions tab and look for a
  red run.
- **A red run means a feed is really gone, not just slow.** Blogs
  occasionally serve a truncated or non-XML page; one bad read is logged
  and ignored. A feed unreadable three runs in a row fails the run, which
  is when GitHub emails you. The heartbeat lists any feed currently on a
  failing streak.
- **Are scheduled runs happening?** Runs triggered by the cron are labelled
  with the schedule rather than a person's avatar. GitHub honours only a
  fraction of scheduled slots on shared runners (observed: 2 of 6 in a
  day) and delays the rest by hours, so the cron asks hourly to land a
  few real checks a day - plenty for a sale that lasts ~72 hours.
- **Will email actually reach me?** Don't wait for a real sale to find out.
  Actions tab > Run workflow > tick **"Send a test email to verify SMTP,
  then stop"**. It sends one test message to `ALERT_TO` and exits without
  touching `seen.json`. If the app password is wrong the run fails on that
  step with the SMTP error.

- **Why did (or didn't) a post alert?** Every match is logged with its
  reason. To see how the matcher views everything currently live, run
  the workflow with **"Print which live posts would alert and why"**
  ticked: it lists each post mentioning Delta as ALERT, quiet, or
  loose-only (a sale phrase present but not near a Delta mention, the
  digest case that is deliberately rejected). Sends and saves nothing.

Quiet inboxes are the normal state: the baseline run is silent by design,
and later runs only email when a post matches both the Delta and the
sale patterns. The daily heartbeat is the proof it is still looking.

## ANA business award check (on demand)

The repository has exactly two workflows. **"Delta award sale watch"** is
the hourly one above, on GitHub's runners. **"ANA award check (on demand)"**
below is manual and runs on your own machine; it never runs on a schedule,
so it cannot produce surprise failure mail.

A separate workflow, **"ANA award check (on demand)"**, checks actual award
*space* rather than blog coverage: business-class awards on ANA-operated
flights between New York and Tokyo / Osaka, one united.com search per date
in a range you choose. United shows Star Alliance partner space without a
login, which is the inventory ANA releases to partners, so no account of
yours is involved and nothing can be locked.

**It cannot run on GitHub's own servers.** The "Probe award sites"
workflow showed why: from a hosted runner, united.com accepts the
connection and never answers, Aeroplan and LifeMiles return 403 Access
Denied, Copa returns 401. Every no-login award site blocks cloud address
ranges outright. From a home connection the same sites load normally.

So the workflow runs on a **self-hosted runner**: a small GitHub agent on
your own computer. The Actions button works exactly the same; the job just
executes on your machine, from your IP.

### One-time setup (about 10 minutes)

1. **Make the repo private first.** GitHub advises against self-hosted
   runners on public repos. These workflows only run when you press the
   button, but private removes the question. Settings > General > Danger
   Zone > Change visibility. (The hourly Delta watch then uses the free
   2,000 minutes/month; it needs about 400.)
2. Settings > Actions > **Runners** > **New self-hosted runner**. Pick your
   OS, then paste the download and configure commands it shows into a
   terminal. Accept the defaults; the runner registers with the label
   `self-hosted`.
3. Start it with `./run.sh` (Mac/Linux) or `run.cmd` (Windows) and leave
   that terminal open while you want to use it, or install it as a
   service with `./svc.sh install && ./svc.sh start` so it is always on.
4. Python 3 must be on the machine; the workflow installs Playwright and
   a Chromium build into your user cache on first run.

### Running a check

Actions tab > **ANA award check (on demand)** > Run workflow. Enter the
first and last date (up to 31 days), pick a direction, keep `NYC` and
`TYO,OSA` unless you want something else, and leave the runner as
`self-hosted`. A few minutes later one email lists, per date, every flight
with an ANA business award: flight numbers, routing, nonstop or
connection, and the miles United would charge. ANA Mileage Club prices
the same seat at 75k-90k round trip by season, so every seat listed is
under the 135k round-trip threshold; one-ways at or under 65k are flagged.

The job log has the same list, and the run's artifact keeps United's raw
responses and a screenshot of any search that failed. Three failures in a
row stop the run.

Two diagnostics share the same button. **Site access** ticked skips the
search entirely and just reports whether this runner can reach each award
site, which is the quickest way to confirm a new self-hosted runner works
(leave the dates blank). **Probe** ticked runs only the first search and
prints United's raw response shape, for adjusting the parser if United
changes its site.

### Without a runner

The same script runs by hand:

```
pip install playwright && python -m playwright install chromium
python ana_check.py --start 2027-03-01 --end 2027-03-31 --no-email
```

`--direction out|return|both`, `--dests TYO,OSA`, `--probe`, and
`--headed` (watch the browser) are available. Without `--no-email` it
needs `SMTP_USER`, `SMTP_PASS` and `ALERT_TO` in the environment.

## Tuning

- **Frequency:** edit the cron in the workflow. It is hourly because
  GitHub drops most scheduled slots; don't go sparser than `*/3`.
- **Heartbeat cadence:** `HEARTBEAT_EVERY` in `monitor.py` (default 20h).
- **Keywords:** tighten or loosen the `SALE` regex in `monitor.py`.
- **Non-Gmail SMTP:** set `SMTP_HOST` / `SMTP_PORT` as extra secrets
  and pass them through in the workflow env block.

## Notes

- GitHub disables scheduled workflows in repos with no activity for
  60 days — the seen.json commits from each run keep it alive.
- When an alert lands, open the Delta Award Watch app in Claude and
  hit Scan to price the sale against your NYC profile in cents per mile.
