# Delta Award Sale Watch

Emails you within ~6 hours whenever the major award-travel blogs
(Thrifty Traveler, Frequent Miler, Loyalty Lobby, One Mile at a Time,
Upgraded Points) publish coverage of a new Delta SkyMiles award or
flash sale. Runs free on GitHub Actions, stdlib Python only.

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
- **Are scheduled runs happening?** Runs triggered by the cron are labelled
  with the schedule rather than a person's avatar. GitHub commonly delays
  them 5-30 minutes, and can skip a slot entirely when its runners are
  busy - harmless for a sale that lasts ~72 hours.
- **Will email actually reach me?** Don't wait for a real sale to find out.
  Actions tab > Run workflow > tick **"Send a test email to verify SMTP,
  then stop"**. It sends one test message to `ALERT_TO` and exits without
  touching `seen.json`. If the app password is wrong the run fails on that
  step with the SMTP error.

Quiet inboxes are the normal state: the baseline run is silent by design,
and later runs only email when a post matches both the Delta and the
sale patterns.

## Tuning

- **Frequency:** edit the cron in the workflow. Every 3 hours:
  `"17 */3 * * *"`. Flash sales last ~72h, so 6h is plenty.
- **Keywords:** tighten or loosen the `SALE` regex in `monitor.py`.
- **Non-Gmail SMTP:** set `SMTP_HOST` / `SMTP_PORT` as extra secrets
  and pass them through in the workflow env block.

## Notes

- GitHub disables scheduled workflows in repos with no activity for
  60 days — the seen.json commits from each run keep it alive.
- When an alert lands, open the Delta Award Watch app in Claude and
  hit Scan to price the sale against your NYC profile in cents per mile.
