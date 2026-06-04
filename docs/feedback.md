# Feedback loop — setup & how it works

The 👍 / 👎 reactions on the daily Slack digest can drive automatic tuning
suggestions. The code path is fully wired; it stays dormant until the secrets
below are in place. Here's the end-to-end flow and exactly what to configure.

## The flow

```
Slack reaction (👍/👎)
  → Slack Events API  →  admin /api/slack/events  →  Vercel Blob (events/…)
       │
       │  (A) immediate visibility
       └─→ admin Suggestions page → "Recent reactions" panel (reads Blob live)
       │
       │  (B) daily, in the GitHub Actions cron
       └─→ feedback_puller.pull()      Blob → SQLite `feedback` table
           feedback_aggregator.aggregate()  SQLite → proposals/pending.json
           workflow commits pending.json → admin Suggestions page shows proposals
```

(A) works as soon as reactions reach Blob. (B) — the automatic *proposals* —
needs everything below.

## What you need to configure

### 1. Slack app — deliver reaction events
In api.slack.com → your app:
- **Event Subscriptions** → on. Request URL: `https://signal-agent-admin.vercel.app/api/slack/events` (must verify ✓).
- Subscribe to **bot events**: `reaction_added`, `reaction_removed`.
- **OAuth & Permissions** → bot scopes: `reactions:read`, plus `chat:write` (for posting via the bot — see #3). Reinstall the app after changing scopes.

### 2. Vercel (admin app) env — already needed by the receiver
- `SLACK_SIGNING_SECRET` — verifies inbound Slack requests.
- `BLOB_READ_WRITE_TOKEN` — the events receiver writes reactions here, and the
  "Recent reactions" panel reads them back.

### 3. Digest must post via the bot (to capture a message ts)
Reactions are linked to a digest by the Slack message timestamp (`slack_ts`).
The **webhook** post path returns no ts, so reactions can't be linked. Posting
via `chat.postMessage` does. Add these as **GitHub Actions repo secrets**:
- `SLACK_BOT_TOKEN` — `xoxb-…` bot token.
- `SLACK_CHANNEL_ID` — the channel id the digest posts to (e.g. `C0123ABC`).

When both are set the daily run posts via the bot and stores the ts; when unset
it falls back to the webhook (no linking).

### 4. Blob token for the cron — turns the loop on
Add as a **GitHub Actions repo secret**:
- `BLOB_READ_WRITE_TOKEN` — same value as the admin app's. This is the switch:
  the `FEEDBACK_ENABLED` gate in `.github/workflows/daily-digest.yml` only runs
  the pull / aggregate / commit steps when this secret is present.

> Where to find the Blob token: Vercel → Storage → your Blob store → it's the
> `BLOB_READ_WRITE_TOKEN` already set on the admin project. Copy that value into
> GitHub → repo → Settings → Secrets and variables → Actions → New repository secret.

## Verifying

1. **Capture** — react 👍 on a digest in Slack, open the admin **Suggestions**
   page, click **Refresh** on "Recent reactions". The reaction should appear
   within seconds. If it doesn't, the problem is in #1 or #2 above.
2. **Proposals** — after at least one upvoted and one downvoted digest, the next
   daily run (or a manual `workflow_dispatch` of *Daily Digest*) pulls the
   reactions, regenerates `proposals/pending.json`, and commits it. The
   Suggestions page then shows proposals. Threshold lives in
   `src/feedback_aggregator.py` (`MIN_UPVOTED_DIGESTS` / `MIN_DOWNVOTED_DIGESTS`,
   currently 1 / 1).

## Notes

- The Blob list endpoint used by `feedback_puller.py` (`https://vercel.com/api/blob`)
  is the same default base URL the `@vercel/blob` SDK uses — verified against the
  SDK source, no change needed.
- The cron commits as `ashwinknan@gmail.com` so the admin redeploy it triggers
  isn't blocked by Vercel's Hobby commit-author rule.
