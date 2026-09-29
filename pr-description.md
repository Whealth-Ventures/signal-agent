# [minor] PROD Release: Story archive with Q&A labels, OpenAI failure alerts, and a live Neon copy

**Phase A of the news archive: every new story gets its full article body and searchable tags in SQLite.** No ticket. The plan is in [docs/2026-09-25-news-archive-plan.md](docs/2026-09-25-news-archive-plan.md).

## What changes

- **New `src/enricher.py`**, run from `deploy/run-digest.sh` after the post and before the backup. It's its own process and never fatal.
- **New `story_details` table** in `agent.db`, created by `init_db`. The digest's own tables and queries are untouched.
- **Pass 1** fetches bodies with the existing `headline_rewriter._fetch_excerpts`, now with an optional `limit`. The default is unchanged.
- **Pass 2** tags stories with OpenAI `gpt-4.1-mini`: 10 per call, 4 calls in parallel, saved per batch.
- **Labels depend on the category.** Every story gets category, magnitude (the ranker's S/A/B/C rubric), companies, geo and a two-line summary. Each category adds its own `facts` (`enricher.CATEGORY_FIELDS`). For example, deal size appears only on deal categories, and regulator plus product only on FDA & Regulatory. Deals always go in a deal category.
- **Healthcare check first.** The model answers `healthcare: true/false`, and, when the article body was fetched, the digest's lexicon gate (`topicality.is_healthcare`) can veto a yes. Without a body the model decides alone, because title and summary are too thin for the lexicon. Non-healthcare stories become `not_healthcare`, tier C, with no facts. Healthcare stories outside the 8 categories become `other_healthcare`.
- **The prompt lives in `prompts/tagger_system.md`.** The category fields, their allowed values and the rubric are appended in code, so they can't drift from what the validator accepts.

## Slack alert when OpenAI fails

- **Today a dead OpenAI key means no digest, silently.** Scoring embeds with no error handling, so the run crashes before the post.
- **New `src/alerts.py`** spots failures a retry won't fix:
  - `insufficient_quota`: out of credits
  - 401 or 403: key rejected
  - `model_not_found`: model not available on the key

  It walks the exception's cause chain, and a plain rate limit is ignored.
- **It posts in the failing run's own channel**, with the fix:
  - out of credits: the billing link, and "the next run recovers on its own"
  - key rejected: update the secret, then redeploy
- **Call sites:**
  - `main.py` and `sector_main.py`: alert, then re-raise, so the journal still has the traceback. `--dry-run` never alerts.
  - `enricher.py`: once per run, after the batches, to the channel given by the new `--geo` flag from `run-digest.sh`.
- **At most one alert per channel per run.** A crashed digest stops `run-digest.sh`, so the enricher doesn't add a second alert.
- **Posting is fail-soft.** An alert that can't post is logged and never masks the original error.

## Live copy in Neon, for DBeaver

- **New `src/neon_sync.py`** runs at the end of `run-digest.sh` and `run-sector.sh`. It's non-fatal, and it's skipped when `DATABASE_URL` isn't set.
- **It's one-way.** `agent.db` goes to schema `public` and `sector.db` to schema `sector`. SQLite stays the source of truth, and nothing reads Neon back.
- **Not copied:** `stories.embedding` and `signals.raw_json`. They're binary or raw payloads, and they make up most of the SQLite file.
- **What each run sends:**
  - a table that's empty in Neon gets every row
  - `signals` get the last 45 days. They're insert-or-ignore and linked to a story once, so older rows never change
  - `stories` get every row, every run. A re-seen story is rewritten in place with no timestamp to window on. Sending all 8,581 took 7 seconds
  - `digests` and `digest_stories` get every row, since they're small
  - `story_details` gets rows newer than Neon's own newest, so a missed sync catches up on its own
- **Unchanged rows cost no write.** The upsert has an `IS DISTINCT FROM` guard.
- **One transaction per schema.** A failure leaves Neon exactly as it was after the last good sync.
- **Bodies older than 12 months are nulled in Neon only.** That keeps the 0.5 GB free tier lasting about 4 years.
- **NUL bytes are stripped on copy.** Postgres text rejects them, and the first real sync hit two scraped bodies that had them.
- **New dependency:** `psycopg[binary]>=3.2`. `deploy.sh` installs it from `requirements.txt`.
- **New env var:** `DATABASE_URL`, the direct (non-pooled) connection string. It needs adding to the agent secret in Secrets Manager.

## Safety

- **The digest can't be affected.** Enrichment starts after `main.py` exits, and `|| echo WARN` keeps `set -e` from ending the script.
- **A failed fetch gets a `failed` row, so it's never retried.**
- **A failed tag call costs only its own batch.** Those stories get tagged on the next run.
- **Off-list tag values become `other` or null.** Ids the model invents are ignored.
- **One new dependency:** `psycopg[binary]`, installed by `deploy.sh` from `requirements.txt`.
- **One new optional env var:** `DATABASE_URL`. It isn't in `REQUIRED_ENV`, so without it the Neon sync is skipped.
- No new unit file.
- **Timeouts:** the enricher is capped at 20 minutes and the Neon sync at 5, so neither can hold up the nightly backup.

## Changes after review

- **Blocker fixed:** the lexicon can now overrule the model only when a body was fetched.
  - With a failed fetch, "Ultrahuman raises $12M Series B" has no stem, so it was filed `not_healthcare` for good.
  - Re-labelling the 41 failed-fetch stories in the prod snapshot moved 2 real healthcare stories out of `not_healthcare`, including "Seniors Places… Senior Living".
  - The 2 left are correctly not healthcare: saw-palmetto poaching and a KKR earnings update.
- **Untagged stories are retried for 30 days**, not 2. The alert's "the next run recovers on its own" now holds for an outage of up to a month.
- **Neon gets every `stories` row, every run.** A first fix windowed on `published_at`. The re-review showed that isn't enough: a re-seen story keeps the article's own date, and `upsert_story` keeps `created_at`. The unchanged-row check keeps a full send free of writes. An `updated_at` watermark is the upgrade path if it ever gets slow.
- **Junk bodies count as a failed fetch.** That's anything under 300 characters, or a PDF read as text. In 319 real fetches, every body under 300 characters was a copyright line, a geo-block or consent page, a nav menu, a PDF, or a paywall teaser. A junk body used to count as a fetched article and hand the healthcare filter nothing to go on ("Please enable JavaScript to continue." reproduced the original mislabel).
- **Timeouts** on the enricher and the sync. There's also a comment on `main.py`'s line in `run-digest.sh` recording why it must stay unguarded: that's what guarantees one alert per channel per run.
- **Company names are capped at 120 characters.**
- **New tests:**
  - a failed fetch with no healthcare word keeps the model's category
  - an untagged story is retried after the fetch window
  - a re-seen story is re-sent to Neon
  - company names are capped
  - `sync()` itself, with a fake cursor: the watermark, NUL stripping, 12-month body retention, and skipping a missing DB

## Tests

- `tests/test_enricher.py`, 4 tests:
  - the fetch and tag window, validation fallbacks, and ignoring invented ids
  - facts outside the story's own category are dropped (an AI story never keeps a deal size)
  - a failed call keeps the bodies, and the next run tags them without re-fetching
  - every category has fields, and the prompt lists all of them
  - a non-healthcare story is tier C with no facts, whether the model or the lexicon says so
  - an unknown category becomes `other_healthcare`
- `tests/test_alerts.py`, 10 tests:
  - classification, including an error wrapped by another and a plain rate limit that must not alert
  - the posted text and channel
  - a failed post never raises
  - each of the three call sites alerts the right channel, and a dry run doesn't
- **The classifier was checked against real OpenAI responses:** a bad key gives 401 `invalid_api_key` → `auth`, and a missing model gives 404 `model_not_found` → `model`. Out of credits can't be triggered on demand, so it's covered by a unit test on the documented `insufficient_quota` code.
- `tests/test_neon_sync.py`, 6 tests:
  - the first sync sends every row, and later syncs send only the window
  - a late tag is still sent for an old fetch
  - embeddings and raw payloads are never selected, and every mirrored column exists in SQLite
  - the upsert skips unchanged rows
  - no `DATABASE_URL` means the sync is skipped without connecting
- **Live test of the Neon copy** on the 27 September prod backup, from a local run with prod untouched:
  - 319 real stories were labelled ($0.15)
  - the first sync copied 8,581 stories, 11,740 signals and 319 labelled articles, plus the sector data, in 8.2 seconds (24 MB in Neon)
  - the second sync sent only the windows and changed nothing
  - the IPO question runs correctly in Postgres
- Re-review tests: a JS wall or a PDF body is stored as a failed fetch and the model's category is kept; an old story whose score changed in place is re-sent to Neon.
- The full suite passes locally (329 passed, 3 skipped).
- **Live test on 80 real prod stories** (scratch DB, prod untouched): 75 of 80 bodies fetched, all 80 tagged in 16 seconds for $0.036. The "biggest IPO stories this month" query returns only healthcare IPOs (ADARx, Oura, Iambic, RegenLab and others). All 34 non-healthcare IPOs are `not_healthcare`. The two `test_config` env checks fail only because this checkout has no `.env`.

## After merge

- The next digest run enriches the last 2 days. Check `data/logs/enrich_<date>.jsonl`.
- The 30-day backfill is a separate command, listed in the plan doc. It costs about $2.
- Add `DATABASE_URL` (the direct connection) to the agent secret, then redeploy. Until then the Neon sync is skipped.

**Need from you:** review and merge.
