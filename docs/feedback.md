# Feedback loop — setup & how it works

The 👍 / 👎 reactions on the daily Slack digest can drive automatic tuning
suggestions. The code path is fully wired; it stays dormant until the config
below is in place. Here's the end-to-end flow and exactly what to configure.

Everything runs on the **EC2 box** (Whealth AWS account) and reaction events are
stored in **S3** — this replaced the old Vercel Blob backend. (See
`infra/README.md` for the deployment picture.)

## The flow

```
Slack reaction (👍/👎)
  → Slack Events API  →  admin /api/slack/events  →  S3 (events/YYYY-MM-DD/…)
       │
       │  (A) immediate visibility
       └─→ admin Suggestions page → "Recent reactions" panel (reads S3 live)
       │
       │  (B) daily, in run-digest.sh on the box (after the digest posts)
       └─→ feedback_puller.pull()        S3 → SQLite `feedback` table
           feedback_aggregator.aggregate()  SQLite → proposals/pending.json
           → admin Suggestions page shows proposals
```

(A) works as soon as reactions reach S3. (B) — the automatic *proposals* —
needs everything below.

## What you need to configure

### 1. Slack app — deliver reaction events
In api.slack.com → your app:
- **Event Subscriptions** → on. Request URL:
  `https://signal-admin.xponentiate.com/api/slack/events` (must verify ✓).
- Subscribe to **bot events**: `reaction_added`, `reaction_removed`.
- **OAuth & Permissions** → bot scopes: `reactions:read`, plus `chat:write` (for
  posting via the bot — see #3). Reinstall the app after changing scopes.

### 2. Admin app env — needed by the events receiver
Set on the admin service (rendered from Secrets Manager at deploy time into
`admin/.env.production`):
- `SLACK_SIGNING_SECRET` — verifies inbound Slack requests.
- `FEEDBACK_S3_BUCKET` — the events receiver writes reactions here, and the
  "Recent reactions" panel reads them back.
- (The box's IAM instance role grants S3 access — no static AWS keys needed.)

### 3. Digest must post via the bot (to capture a message ts)
Reactions are linked to a digest by the Slack message timestamp (`slack_ts`).
The **webhook** post path returns no ts, so reactions can't be linked. Posting
via `chat.postMessage` does. Set these in the agent env (Secrets Manager →
`agent.env`):
- `SLACK_BOT_TOKEN` — `xoxb-…` bot token.
- `SLACK_CHANNEL_ID` — the channel id the digest posts to (e.g. `C0123ABC`).

When both are set the daily run posts via the bot and stores the ts; when unset
it falls back to the webhook (no linking).

### 4. `FEEDBACK_S3_BUCKET` on the box — turns the loop on
Set in the agent env (Secrets Manager → `agent.env`). This is the switch:
`deploy/run-digest.sh` only runs the pull / aggregate steps when
`FEEDBACK_S3_BUCKET` is non-empty:

```bash
if [ -n "${FEEDBACK_S3_BUCKET:-}" ]; then
  .venv/bin/python src/feedback_puller.py
  .venv/bin/python src/feedback_aggregator.py
fi
```

Use the **same bucket** the admin app writes events to.

## Verifying

1. **Capture** — react 👍 on a digest in Slack, open the admin **Suggestions**
   page, click **Refresh** on "Recent reactions". The reaction should appear
   within seconds. If it doesn't, the problem is in #1 or #2 above.
2. **Proposals** — after at least one upvoted and one downvoted digest, the next
   daily run pulls the reactions and regenerates `proposals/pending.json`. The
   Suggestions page then shows proposals. Threshold lives in
   `src/feedback_aggregator.py` (`MIN_UPVOTED_DIGESTS` / `MIN_DOWNVOTED_DIGESTS`,
   currently 1 / 1).

## Notes

- The admin UI reads/writes repo files (`proposals/pending.json`, `tuning.xlsx`,
  prompts) via the GitHub API (`admin/lib/github.ts`). The aggregator on the box
  writes `proposals/pending.json` into the box's repo checkout — for the admin to
  surface freshly regenerated proposals, that file has to reach GitHub. In the
  AWS push model `run-digest.sh` has no git step, so confirm how regenerated
  proposals are committed back before relying on the automatic (B) path;
  applying a proposal (which the admin commits via GitHub) works regardless.
- The aggregator does **not** mutate `tuning.xlsx` directly. Application happens
  via the admin UI's accept-proposal flow, and takes effect on the **next
  deploy** (the box runs the inputs/prompts that shipped in the last deploy).
