# Signal Agent — Project Brief

## Working rule — always start with FEEDBACK.md
Before starting new work, **read `FEEDBACK.md`** (repo root) and address its open
items first. When you fix one:
1. Remove it from `FEEDBACK.md`.
2. Record what changed, in plain language, in `RELEASE_NOTES.md`.
3. Ship it — via a PR, see below.

`FEEDBACK.md` is the backlog (what's broken / recommended next); `RELEASE_NOTES.md`
is the record of what's been shipped. Fixed things move from the first to the second.

## How to ship — PR only, never a direct push
`main` is branch-protected. `git push origin main` is rejected with
`GH013: Repository rule violations`. Every change goes through a PR whose
**title** begins with `[major]`, `[minor]` or `[patch]` — a required status check
enforces it (`.github/workflows/pr-title-bump.yml`), and Jenkins reads that
marker to pick the semver bump.

```bash
git checkout -b my-change
gh pr create --title "[patch] what this does" --body "..."
gh pr merge <n> --squash --delete-branch
```

**Then stop — do NOT deploy by hand.** Jenkins builds on merge (GitHub webhook →
`jenkins.xponentiate.com`), runs the tests, bumps `VERSION`, tags `vX.Y.Z`,
uploads the artifact and deploys to the box over SSM. It takes **2–3 minutes**;
checking the box sooner than that and concluding "Jenkins didn't fire" is a
mistake that has been made — watch for the `chore: bump version to X [skip ci]`
commit from `Jenkins CI` on `main` instead.

A manual artifact build fights this: it repoints `latest.tgz` at a commit that
carries no version bump, so a replacement instance boots unversioned and
rollback-by-version (`scripts/resolve-release-artifact.sh`) can't resolve it.
Only deploy by hand when Jenkins is genuinely broken — and confirm that from the
absence of a bump commit, not from the box being stale 30 seconds after a merge.

## What this is
A daily healthcare news digest agent for a VC firm (W Health Ventures / 2070 Health).
Runs autonomously and posts **two geo-scoped digests from the same app**: an
India digest (India + Global) to **Signal Agent India** at 08:00 IST, and a US
digest (US + Global) to **Signal Agent US** at 08:00 America/New_York. Each is a
full 15–25 story digest grouped by category with a 5-story "Today's biggest
stories" section on top. Every story is tagged India/US/Global and bucketed under
one of the 8 priority buckets (no "Other" section). Global/cross-cutting stories
(all AI-in-Healthcare, Hot-TAs) and unclassified RSS go to BOTH channels.

Run selection is by `--geo {india,us,both}` (default `both` = legacy single
channel). Each geo run does its own deep sweep and posts to its own channel; the
two runs fire from separate systemd timers (see `docs/scheduling.md`).

There is also a **third agent**: a **weekly Sector Agent** (`src/sector_main.py`,
`src/sector.py`) that posts a portfolio-impact digest to a third channel (**Signal
Agent Sector**) at 08:00 IST on Mondays. It watches the ~16 W Health portfolio
companies (from `inputs/portfolio.xlsx`) and surfaces sector/regulatory/macro and
direct-competitor news with a **material impact** (positive/negative) on each
company — grouped by company, NOT by the 8 priority buckets, and ranked by impact
rather than healthcare magnitude. It reuses the daily pipeline's fetch/dedup/slack
transport but runs against its **own SQLite db** (`data/db/sector.db`) so its
stories never touch the daily candidate pool.

## Inputs
**SharePoint is the source of truth for everything under `inputs/`, and
`inputs/` is NOT in git** (it's gitignored — a local cache, not source).
`src/sharepoint_sync.py` mirrors the SharePoint folder down at the top of every
run, as its own process before `main.py` — `config.py` parses `tuning.xlsx` at
*import* time, so an in-process sync would apply one run late. A SharePoint edit
is live on the next run: no commit, no deploy.

Consequences to know:
- A fresh clone can't run anything until it syncs once — `import config` raises
  without `inputs/tuning.xlsx`. CI bootstraps via `scripts/build_default_tuning_xlsx.py`.
- Tests touching the real workbooks are `skipUnless`-guarded on the file
  existing, so they skip rather than fail on an unsynced checkout.
- Sync failure is deliberately non-fatal: the run proceeds on the previously
  synced files. A box therefore keeps working through a SharePoint outage, but a
  *stale* digest is possible — grep runs for `WARN: sharepoint sync failed`.

`prompts/` is NOT in SharePoint — it stays git-backed, admin-UI-editable, and
needs a deploy. See `docs/EDITING.md`.

- `inputs/keywords.xlsx` — single `Master Keywords` tab with columns Bucket / Sub-bucket / Keyword / Geo; ~2,240 keywords.
- `inputs/voices.xlsx` — 5 tabs (India Top Voices, US Top Voices, Newsletters & Publications, Firms & Org Pages, New Additions); ~225+ rows across them.
- `inputs/tuning.xlsx` — 4 sheets (Settings, Boosters, Priority Buckets, Source Tiers). Every numeric knob, every regex booster pattern, the priority-bucket structure, and the source tier list. The single editable surface for tuning. See `docs/EDITING.md`.
- `inputs/content/` — firm's own published content, used as the "taste profile" for relevance scoring. Subfolders: `2070_health/`, `articles_blog/`, `interviews_podcasts/`, `linkedin/`, `news_press/`.

## Architecture (4 layers — DO NOT collapse into one big LLM call)
1. **Query planner** — clusters keywords into ~51 thematic Perplexity queries/day across 4 tracks: Track A (13 priority plans), Track B (rotating sub-bucket plans — count derived from `track_b_rotation_days`, default 7 → ~35/day, capped by `track_b_plans_per_day`), voice (2 plans), firm (1 plan).
2. **Fetchers** — Perplexity API for open web (sonar-pro, recency=day) + RSS pulls for newsletters AND voices that have an `rss_url` (column J of the Top Voices tabs). Both run concurrently via `asyncio` (Perplexity under `Semaphore(5)`, RSS under `Semaphore(10)`).
3. **Dedupe + score** — order-independent near-dup clustering (connected-components / union-find over the cosine-similarity matrix). A relevance score (cosine vs `inputs/content/` corpus + boosters) is still computed for audit, but it NO LONGER gates or ranks the digest — a deterministic healthcare topicality gate (`topicality.py`) decides candidacy and magnitude tiering decides inclusion. One batched Chroma query per scoring run.
4. **Ranker** — a single LLM call assigns each candidate a magnitude tier (S/A/B/C), a one-line headline, AND its best-fit bucket (one of the 8). Uses **Claude** when `ANTHROPIC_API_KEY` is set (`ranker_provider=anthropic`), else falls back to Perplexity `sonar-reasoning-pro`. Within a tier, ordering is by recency.

## Modules
- `src/sharepoint_sync.py` — mirrors the SharePoint inputs folder into `inputs/` before a run. Standalone (must not import `config`), app-only Graph, fail-soft.
- `src/config.py` — paths, env, prompt loading; re-exposes tunables from `inputs/tuning.xlsx`.
- `src/tunables.py` — loader for `inputs/tuning.xlsx`.
- `src/query_planner.py` — reads both Excels, emits list of query plans.
- `src/perplexity_client.py` — API wrapper, sync + async, with retries and daily budget enforcement.
- `src/rss_fetcher.py` — async concurrent RSS sweep over the newsletter list.
- `src/content_indexer.py` — embeds `inputs/content/` into local Chroma DB; idempotent via per-file hash.
- `src/storage.py` — SQLite schema (signals, stories, digests).
- `src/scorer.py` — order-independent (union-find) dedupe + relevance scoring; batched Chroma lookup.
- `src/ranker.py` — single reasoning call (Claude or Perplexity), magnitude-rubric tiering + bucket assignment; topicality-gated candidates.
- `src/anthropic_client.py` — Claude transport for the ranking call (Perplexity-compatible `complete()`; auto-skipped when no key).
- `src/topicality.py` — deterministic healthcare lexicon gate; now runs on every ranker path.
- `src/slack_client.py` — Block Kit formatter + Slack poster (chat.postMessage with per-channel `channel_id`, or webhook); concurrent URL validation.
- `src/main.py` — orchestrator (this is what the systemd timers trigger). `--geo` selects the sweep + target channel; `ranker.filter_by_geo` routes stories; `compute_post_at(spec, tz=...)` resolves 08:00 in each geo's timezone.

## Key constraints
- Total Perplexity calls per day must stay under 60 (enforced). The ranker runs on Claude when `ANTHROPIC_API_KEY` is set, so the budget is then fetch-only (~51 plans at a 7-day Track B rotation). With no key it falls back to Perplexity `sonar-reasoning-pro` and costs one more call per run — budget for 52, not 51. (This was silently the case in production until Aug 2026: `main.py` passed its fetch client to `rank_stories()`, which skipped vendor selection entirely. See `ranker._build_ranker_client`.)
- Must NOT repeat stories sent in the last 30 days (URL filter + cross-day embedding similarity check; window configurable via `dedup_window_days` in `inputs/tuning.xlsx`).
- Perplexity budget (`max_perplexity_calls_per_day`) is enforced PER GEO RUN — the India and US runs each get their own daily count (per-`(date, geo)` log file), so same-day runs don't starve each other.
- Idempotency + "already sent today" is PER CHANNEL (`has_sent_digest_for_date(..., slack_channel=...)`) — India shipping doesn't suppress US.
- Validate every URL with HEAD request before it ships in the Slack post (skippable via `--skip-url-validation`).
- Log every API call to `data/logs/` with timestamps and costs.
- All state in SQLite at `data/db/agent.db` — no external DB.

## Tech stack
- Python 3.11+
- httpx (sync + async — async powers the fetch sweep, RSS sweep, and Slack URL validation)
- openpyxl for Excel reading
- chromadb for vector store (local, no cloud)
- OpenAI `text-embedding-3-small` for embeddings
- numpy for clustering / cosine math
- SQLite + sqlite3 stdlib (no ORM)
- Slack Incoming Webhook (modern, provisioned via a Slack App — not the legacy custom integration) for delivery; the digest posts with `unfurl_links/unfurl_media=false` so links don't render as preview cards
- anthropic SDK for the Claude ranking call (optional — `ANTHROPIC_API_KEY`; falls back to Perplexity when unset)
- python-dotenv for config; tenacity for retries
- Microsoft Graph (app-only client credentials, `Sites.Selected`) for the SharePoint input sync — plain `httpx`, no `msal` dependency

## What "done" looks like for v1
Running `python src/main.py` end-to-end:
- Reads `inputs/`
- Hits Perplexity ~30–34 times
- Pulls RSS from voices file
- Stores everything in SQLite
- Outputs digest to console AND posts to Slack
- Completes in roughly 2–3 minutes (down from ~7–10 min pre-async)
- Costs under $3/run

## What to defer to v2
- n8n integration

## Admin UI
A small Next.js app in `admin/`, deployed on Vercel (see `admin/README.md`). It
edits **`prompts/` only** — the LLM prompt markdown — saving via the GitHub API,
which means prompt changes apply on the next deploy.

It used to edit Keywords / Sources / Tuning / Portfolio / Content too. Those
pages are gone: SharePoint owns `inputs/` now and the sync would overwrite
anything the UI wrote there.

## Editing the agent's behavior
For "I want to change X, where do I edit it?", see `docs/EDITING.md`.
For numeric-knob detail, see `docs/TUNING.md`.
For scheduling, see `docs/scheduling.md`.
