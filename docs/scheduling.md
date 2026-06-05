# Scheduling — getting the digest to fire at exactly 10:00 IST

GitHub Actions' native `schedule:` cron is best-effort. Historical runs on this
repo have slipped from 10:00 IST to 2:00 PM IST, and on a few days the run was
skipped entirely (GitHub auto-disables scheduled workflows on inactive repos
after 60 days). For a digest that the team reads first thing each morning,
that's not good enough.

The fix has two parts: a punctual trigger, and an in-job hold so delivery time
doesn't depend on how long the build takes.

**Hold-until-10:00 (sharp delivery).** The trigger fires a few minutes *before*
10:00. The pipeline fetches/scores/ranks the whole digest (~3 min), then holds
until exactly 10:00 IST before posting to Slack. So the message lands at 10:00
regardless of build duration. This is the `DIGEST_POST_AT="10:00"` env in the
workflow's run step (→ `--post-at`); the hold is capped at 30 min, so an
over-early or badly-slipped trigger just posts as soon as it's ready instead of
idling. Set `DIGEST_POST_AT` empty to disable and post immediately.

**Two triggers, both aimed at ~09:50 IST:**

1. **Primary — external pinger** fires `repository_dispatch` at **09:50 IST**.
   Punctual; this is the path that gives sharp 10:00 delivery.
2. **Fallback — GitHub's cron** at `'20 4 * * *'` UTC (09:50 IST). GitHub's cron
   slips, so it often fires late; when it fires before 10:00 the in-job hold
   still lands it at 10:00, and when it slips past 10:00 it posts immediately.

Firing both is safe: the `daily-digest` concurrency group serializes overlapping
runs, and the pipeline's **idempotency guard** (`has_sent_digest_for_date`) skips
posting if today's digest already went out — so you never get two digests.

The workflow file (`.github/workflows/daily-digest.yml`) already wires all of
this up. Only the external pinger needs setup.

## Setup — cron-job.org (free, recommended)

1. Sign up at <https://cron-job.org> (free tier covers this use case easily).
2. Create a Personal Access Token on GitHub:
   - Settings → Developer settings → Personal access tokens → Fine-grained tokens
   - Repository access: **Only this repo** (`<owner>/signal-agent`)
   - Permissions: **Actions: Read & Write** + **Contents: Read**
   - Copy the token (`github_pat_…`) — you'll paste it into cron-job.org.
3. In cron-job.org, create a new cronjob:
   - **URL**: `https://api.github.com/repos/<owner>/signal-agent/dispatches`
   - **Schedule**: `09:50` daily, timezone **Asia/Kolkata** (the pipeline holds
     until 10:00 before posting — fire ~10 min early so the build finishes first)
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
Two layers stop a double-send: the concurrency group `daily-digest` serializes
overlapping runs, and the idempotency guard skips posting once today's digest
has been sent — so even non-overlapping triggers can't produce two digests.
(Re-send deliberately with the workflow's **Run workflow → force** input.)

## Disabling the fallback

If you don't want the fallback (e.g. external pinger is rock-solid and you
prefer single source of truth), remove the `schedule:` block from
`.github/workflows/daily-digest.yml`. The `repository_dispatch` trigger stays.
