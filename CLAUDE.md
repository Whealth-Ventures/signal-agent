# Signal Agent — Project Brief

## What this is
A daily healthcare news digest agent for a VC firm (W Health Ventures / 2070 Health).
Runs autonomously, posts to a Slack channel at 10am IST every day. Typical digest
is 15–25 stories grouped by category, with a 5-story "Today's biggest stories"
section at the top.

## Inputs
- `inputs/keywords.xlsx` — single `Master Keywords` tab with columns Bucket / Sub-bucket / Keyword / Geo; ~2,240 keywords.
- `inputs/voices.xlsx` — 5 tabs (India Top Voices, US Top Voices, Newsletters & Publications, Firms & Org Pages, New Additions); ~225+ rows across them.
- `inputs/tuning.xlsx` — 4 sheets (Settings, Boosters, Priority Buckets, Source Tiers). Every numeric knob, every regex booster pattern, the priority-bucket structure, and the source tier list. The single editable surface for tuning. See `docs/EDITING.md`.
- `inputs/content/` — firm's own published content, used as the "taste profile" for relevance scoring. Subfolders: `2070_health/`, `articles_blog/`, `interviews_podcasts/`, `linkedin/`, `news_press/`.

## Architecture (4 layers — DO NOT collapse into one big LLM call)
1. **Query planner** — clusters keywords into ~34 thematic Perplexity queries/day across 4 tracks: Track A (13 priority plans), Track B (18 rotating sub-bucket plans), voice (2 plans), firm (1 plan).
2. **Fetchers** — Perplexity API for open web (sonar-pro, recency=day) + RSS pulls for named voices/newsletters. Both run concurrently via `asyncio` (Perplexity under `Semaphore(5)`, RSS under `Semaphore(10)`).
3. **Dedupe + score** — embedding similarity (numpy-accelerated) to dedupe stories; relevance score = cosine sim vs `inputs/content/` corpus + deterministic boosters. One batched Chroma query per scoring run.
4. **Ranker** — Perplexity `sonar-reasoning-pro` call assigns each candidate a magnitude tier (S/A/B/C) plus a one-line headline. NOT Claude API — keeps vendor count low.

## Modules
- `src/config.py` — paths, env, prompt loading; re-exposes tunables from `inputs/tuning.xlsx`.
- `src/tunables.py` — loader for `inputs/tuning.xlsx`.
- `src/query_planner.py` — reads both Excels, emits list of query plans.
- `src/perplexity_client.py` — API wrapper, sync + async, with retries and daily budget enforcement.
- `src/rss_fetcher.py` — async concurrent RSS sweep over the newsletter list.
- `src/content_indexer.py` — embeds `inputs/content/` into local Chroma DB; idempotent via per-file hash.
- `src/storage.py` — SQLite schema (signals, stories, digests).
- `src/scorer.py` — numpy-clustered dedupe + relevance scoring; batched Chroma lookup.
- `src/ranker.py` — single Perplexity reasoning call, magnitude-rubric tiering.
- `src/slack_client.py` — Block Kit formatter + Slack Incoming Webhook poster; concurrent URL validation.
- `src/main.py` — orchestrator (this is what cron triggers).

## Key constraints
- Total Perplexity calls per day must stay under 60 (enforced; ranker reserves 2 calls of headroom).
- Must NOT repeat stories sent in the last 30 days (URL filter + cross-day embedding similarity check; window configurable via `dedup_window_days` in `inputs/tuning.xlsx`).
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
- Slack Incoming Webhook (modern, provisioned via a Slack App — not the legacy custom integration) for delivery
- python-dotenv for config; tenacity for retries; msal/azure pieces NOT yet wired (SharePoint migration is paused)

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
- SharePoint sync for inputs (paused; needs Azure admin access)
- n8n integration
- Feedback loop (thumbs up/down via Slack reactions)
- Web dashboard
- Multi-channel support

## Editing the agent's behavior
For "I want to change X, where do I edit it?", see `docs/EDITING.md`.
For numeric-knob detail, see `docs/TUNING.md`.
For scheduling, see `docs/scheduling.md`.
