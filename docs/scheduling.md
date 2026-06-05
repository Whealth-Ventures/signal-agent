# Scheduling — getting the digest to fire at exactly 10:00 IST

GitHub Actions' native `schedule:` cron is best-effort. Historical runs on this
repo have slipped from 10:00 IST to 2:00 PM IST, and on a few days the run was
skipped entirely (GitHub auto-disables scheduled workflows on inactive repos
after 60 days). For a digest that the team reads first thing each morning,
that's not good enough.

The fix is two triggers, layered:

1. **Primary — external pinger** fires `repository_dispatch` at exactly 10:00 IST.
2. **Fallback — GitHub's cron** stays as a safety net. It's set ~3 h early at
   `'30 1 * * *'` UTC (01:30 UTC = 07:00 IST) because GitHub's cron slips: the
   old 04:30 UTC fire was landing ~13:00 IST, so firing early makes the slipped
   run land near 10:00 IST. (On a fast day the fallback may arrive earlier.)

The workflow file (`.github/workflows/daily-digest.yml`) already wires both up.
Only the external pinger needs setup.

## Setup — cron-job.org (free, recommended)

1. Sign up at <https://cron-job.org> (free tier covers this use case easily).
2. Create a Personal Access Token on GitHub:
   - Settings → Developer settings → Personal access tokens → Fine-grained tokens
   - Repository access: **Only this repo** (`<owner>/signal-agent`)
   - Permissions: **Actions: Read & Write** + **Contents: Read**
   - Copy the token (`github_pat_…`) — you'll paste it into cron-job.org.
3. In cron-job.org, create a new cronjob:
   - **URL**: `https://api.github.com/repos/<owner>/signal-agent/dispatches`
   - **Schedule**: `10:00` daily, timezone **Asia/Kolkata**
   - **Request method**: `POST`
   - **Headers**:
     - `Accept: application/vnd.github+json`
     - `Authorization: Bearer <your_pat>`
     - `X-GitHub-Api-Version: 2022-11-28`
     - `Content-Type: application/json`
   - **Request body** (raw):
     ```json
     {"event_type": "daily-digest"}
     ```
   - **Notifications**: turn on email-on-failure so you hear about ping failures.
4. Save and run once with "Test now" to confirm it triggers the workflow.

## Alternatives

- **EasyCron**, **cronitor.io**, **Pipedream** — same pattern, pick whichever
  you prefer.
- **Cloud Scheduler (GCP) / EventBridge (AWS) / Azure Logic Apps** — fine if
  you already have a cloud account.
- **Self-hosted** — a `systemd` timer or even a Mac launchd job on a machine
  that's always on. Cheapest and most reliable, but you own the uptime.

## How to verify it's working

After the first scheduled run:

```bash
gh run list --workflow daily-digest.yml --limit 5
```

Look at the **Event** column — it should say `repository_dispatch` for runs
triggered by the external pinger (not `schedule`). If you see `schedule`, the
fallback fired because the external pinger missed; check the pinger's logs.

## What about the GitHub cron — why keep it?

Defense in depth. If the external pinger service has an outage, or your PAT
expires, the GitHub schedule still gets the digest out (just possibly late).
The concurrency group `daily-digest` ensures both triggers can't double-fire
on the same day — whichever wins the race, the other is blocked.

## Disabling the fallback

If you don't want the fallback (e.g. external pinger is rock-solid and you
prefer single source of truth), remove the `schedule:` block from
`.github/workflows/daily-digest.yml`. The `repository_dispatch` trigger stays.
