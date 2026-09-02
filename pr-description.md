# [minor] Fix the 2 Sept digest feedback: geo tagging, story ordering, bucket catch-all, degraded-run visibility

Answers the 2 September feedback on the India digest from Ashwin (too much
`[GLOBAL]`, "not the right logic") and Shirish (missing Ultrahuman funding news,
opinion piece under Venture & IPO). Full diagnosis in
`docs/2026-09-02-feedback-plan.md`.

All three complaints shared one root cause and two real bugs. The root cause is
already handled outside this PR: `ranker_provider` was flipped to `perplexity` in
`tuning.xlsx`, because the Claude path was silently truncating every response
(see FEEDBACK #11).

## What changed

### 1. RSS signals carry their publication's geography

An RSS story had no geo at all, so it came out `NULL`, rendered `[GLOBAL]`, and
was routed to **both** channels — 105 of 160 stories on 2 Sept.
`Newsletter.geography` was already parsed from `voices.xlsx`; `_parse_feed_body`
just never received it.

Verified on a live sweep: **130 signals → 103 India, 27 US, 0 unclassified.**

Unknown geography values degrade to `Global` (both channels), never to a dropped
story.

### 2. Ordering is tier → score → recency, not tier → recency → score

`_ordered_within_category` and `_top_summary` both had `-published_at` as the
primary key with `relevance_score` as a deep tiebreak. Since fetch already
windows to 24h, publish time barely discriminates, and on the degraded path
(everything Tier A) the digest became a plain recency feed.

Consequence on 2 Sept: an Ultrahuman funding exclusive (score 0.68, caught by
Entrackr RSS **and** two Perplexity plans) and "Even Healthcare raises $22M
Series B" (0.72, the day's highest) both missed the digest, beaten for their
bucket slots by lower-scored items published a few hours later.

### 3. The bucket catch-all is now a deliberate choice

`_default_bucket_key()` returned `PRIORITY_BUCKETS[0]`, i.e. whichever bucket sat
in row 1 of the sheet. That is `venture_ipo`, which is how a STAT opinion piece
about the American Diabetes Association shipped as venture news.

- New `default_bucket` tunable, validated against live bucket keys. Unset or
  unknown keeps the old first-bucket behaviour, so this is safe before the sheet
  is updated.
- A bucket with empty `geos` is now legal and means **display-only**: the ranker
  may assign it and `default_bucket` may point at it, but `query_planner` emits
  no Track-A plans for it, so it costs zero Perplexity calls.

Together these let "Other healthcare news" exist as an honest destination, and
give the *ranker* somewhere to put non-deal health news instead of forcing it
into a deal bucket. Requires the two `tuning.xlsx` edits listed under Deploy
notes; without them behaviour is unchanged.

### 4. A degraded run is visible in Slack

`used_fallback` now appends a warning context block to the post. It was
previously printed only to the server console (`main.py:389`), which is how ten
days of RSS-only digests (22–31 Aug) shipped unnoticed. Appended *after* the
block-ceiling trim so it can never be the block that gets dropped. Closes the
ranker half of FEEDBACK #4; the SharePoint-sync half stays open, narrowed.

### 5. Headline-rewrite review follow-ups

From the review of PR #14, all 🟡 suggestions, no blockers:

- **`httpx.InvalidURL` is not an `httpx.HTTPError`** (verified: its MRO is
  `InvalidURL → Exception`), and `asyncio.gather` had no `return_exceptions`, so
  **one malformed URL aborted all 25 fetches** and silently disabled the rewrite
  for the whole run. Broadened the except and added `return_exceptions=True`.
- Parse failures now log the response body, so "unparseable response" is
  distinguishable from "the model kept every headline". This is the exact blind
  spot that hid FEEDBACK #11 for five days.
- Over-length headlines are trimmed at a word boundary instead of discarded.
- `MAX_HEADLINE_CHARS` now follows `config.ONE_LINER_MAX_CHARS` instead of
  hardcoding 90.
- The vendor client is built *before* the 25-article fetch, so a no-op costs
  nothing.
- The fail-soft handler's own `_log` is guarded (`run_pipeline` is `try/finally`
  with no `except`).
- The rewrite call's cost is added into the run's `cost_usd`.

Not adopted: the prompt-injection note on the 2,000-char excerpt. Real, but
bounded by `_escape_mrkdwn` and the length cap to one misleading line, with no
link or markup escape.

## Testing

244 tests pass. New coverage:

- `test_rss_fetcher`: geography stamped on every signal, normalisation table,
  and no-geography leaves `raw` untouched.
- `test_ranker`: the Ultrahuman case (higher score, older story wins the slot),
  tier still outranks score, recency breaks a score tie, and four
  `default_bucket` cases.
- `test_query_planner`: a display-only bucket emits **zero** plans and doesn't
  disturb the real ones. Note `test_each_priority_bucket_represented` was
  updated to exclude geo-less buckets — it would otherwise fail CI the moment
  the sheet gains one.
- `test_slack_client`: notice present on fallback, absent when healthy, present
  on an empty digest, and surviving the block ceiling.
- `test_headline_rewriter`: real (unmocked) `_fetch_excerpts` with a bad URL
  alongside a good one, a non-HTTP exception, a `by_priority` fixture, word-
  boundary trimming, unusable values, and the parse-failure log.

Two `test_config` cases fail locally for want of a `.env` (`OPENAI_API_KEY`,
`PERPLEXITY_API_KEY`, `SLACK_WEBHOOK_URL`). Pre-existing and environmental,
confirmed against a clean tree; CI supplies them.

## Deploy notes

No env, schema, dependency or infra changes.

Two `tuning.xlsx` edits activate item 3 (SharePoint, no deploy):

1. `Priority Buckets` → add a final row: key `other_healthcare`, display
   `Other healthcare news`, `sub_buckets` any placeholder, **`geos` blank**.
2. `Settings` → add `default_bucket` = `other_healthcare`.

One further edit is required **before** `ranker_provider` ever goes back to
`anthropic` (FEEDBACK #11): `anthropic_max_tokens_rank` 4096 → 8192.

**Behaviour change to expect:** the US digest stops receiving Indian RSS, so it
gets smaller and more American. On 2 Sept it was taking all 148 RSS signals
because they were unclassified; it would now take about 27.
