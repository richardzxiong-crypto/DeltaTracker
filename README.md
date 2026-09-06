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

A separate workflow, **"ANA award check (on demand)"**, checks actual award
*space* rather than blog coverage: business-class awards on ANA-operated
flights between New York and Tokyo / Osaka, one united.com search per date
in a range you choose. United shows Star Alliance partner space without a
login, which is the inventory ANA releases to partners, so there is no
account of yours involved and nothing to lock.

1. Actions tab > **ANA award check (on demand)** > Run workflow.
2. Enter the first and last date (up to 31 days), pick a direction, keep
   `NYC` and `TYO,OSA` unless you want something else, and run.
3. A few minutes later one email lists, per date, every flight with an ANA
   business award: flight numbers, routing, nonstop or connection, and the
   miles United would charge. ANA Mileage Club prices the same seat at
   75k-90k round trip by season, so any seat listed is under the 135k
   round-trip threshold; one-ways at or under 65k are flagged.

The job log has the same list, and the run's artifact keeps United's raw
responses and a screenshot of any search that failed. If united.com blocks
a search the run says so and moves on; three failures in a row stop it.

**Probe** ticked runs only the first search and prints United's raw
response shape instead of emailing, for adjusting the parser if United
changes its site.

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
