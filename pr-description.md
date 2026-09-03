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

### 1. Geo is decided from the story, not from how we found it

Two layers here, because the first one alone gets it wrong.

**1a. The ranker returns a per-story `geo`.** It already reads every candidate
and already returns `tier`, `bucket` and `one_liner` per story; `geo` is a
fourth field on the same call, so this costs nothing. The prompt is explicit
that this is where the *news* happened and **not** the nationality of the
publication. `_effective_geo` precedence: ranker → inherited → `None`.

This is the actual answer to "not the right logic". Before it, geo was
inherited from the query plan that surfaced the story, so anything a Global
plan found was Global whatever it said.

**1b. RSS signals carry their publication's `Geography`** from `voices.xlsx`,
as the fallback layer. `Newsletter.geography` was already parsed;
`_parse_feed_body` never received it. Live sweep: **130 signals → 103 India,
27 US, 0 unclassified.**

Why 1b is only a fallback — measured on the 3 Sept digest, publication
geography is right for local reporting and wrong for international:

```
Gland Pharma Visakhapatnam USFDA        -> India   correct
AIG Hospitals Rs 2000 cr Visakhapatnam  -> India   correct
Sanford Health / North Memorial         -> India   WRONG, Minnesota
Medtronic / Cornerstone Robotics        -> India   WRONG, US
```

Both were reported by Digital Health News, an Indian publication. The same
mechanism is why *"Novartis pauses eight CAR-T trials"* shipped tagged `[IND]`
that morning: `medicaldialogues.in` is India-geo, the news is global. So the
proxy fills gaps and the ranker decides.

Unknown values degrade to `Global` (kept by both channels), never to a dropped
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

### 6. The same story can no longer occupy two slots

A publisher serves one article under several URLs — BioSpectrum India uses
`/news/16/28410/<slug>` and `/news/101/28410/<slug>` — so URL matching missed it
and both copies competed separately. On the 3 Sept India digest, AIG Hospitals
and Luma Fertility each shipped twice, costing **4 of 15 slots**.

`collapse_duplicates` now runs on the **candidate pool**, before bucketing and
selection, so the freed slot goes to the next real story instead of being lost,
and the ranker prompt never contains two copies to tier separately. Sameness is
any of: normalised title, normalised URL, or host + final path segment (only for
segments of 12+ chars, so it can't collide on `/feed` or `/news`). The
highest-scoring copy survives.

### 7. `blocked_domains`, to keep AI-generated social roundups out

Perplexity cites whatever the open web offers, including AI-generated
"daily news roundup" pages on social platforms that recycle weeks-old headlines
under a fresh publish date. Verified on the box: **13 Facebook-cited signals in
30 days producing 11 stories, 10 of which shipped** between 22 Jul and 3 Sept.
The clearest case is 3 Sept's "Even Healthcare Series B", whose canonical URL was
`facebook.com/01indiapo/posts/ai-daily-reporter-2-september-2026…` — the same
cluster held the real report at `digitalhealthnews.com`, published **23 July**.
So it was simultaneously the wrong link and six-week-old news presented as new.

Facebook was never a configured source, so it could not be removed from
SharePoint. New `blocked_domains` setting: semicolon-separated host list,
matched across subdomains, enforced in `storage.save_signals` — the single choke
point every signal passes through, Perplexity and RSS, daily and sector — so a
blocked host cannot enter the pool by any route.

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

## Review round 2 (PR #16)

Both blockers reproduced and fixed, plus every suggestion except one.

**Blocker: blank Settings cell crashed `import config`.** Fixed at the root
rather than at the two call sites: `Tunables.get` now treats a row whose value
cell is blank as absent ([src/tunables.py:66](src/tunables.py#L66)), so every
current and future tunable is covered. Clearing a cell is how an operator turns
a setting off, and both deploy notes tell them to add these rows.

**Blocker: the `u:` identity key discarded the query string.** Reproduced —
three unrelated `pharmabiz.com/NewsDetails.aspx?aid=…` articles collapsed to
one. Worse than reported: the `s:` key collided too, because `NewsDetails.aspx`
is 16 chars and passed the length guard. Now the `u:` key keeps the query minus
tracking params (an article id often lives only there), and the `s:` key
requires a hyphen as well as 12+ chars, so it fires on a real slug and not on a
script name. Four regression tests.

**`filter_by_geo` now re-runs selection instead of filtering it.** The review
was right that item 1 invalidates the old docstring, and my own before/after
render showed the symptom without my spotting the cause: two US highlights
dropped and "Today's biggest stories" fell to a single bullet. Promoted stories
are merged back into their buckets, geo-filtered, then top-summary and bodies
are chosen again, so a dropped highlight is refilled.

Also fixed: per-item audit for every duplicate drop (id, url, title, matched
key, and the survivor it matched); the `geo` field spec moved out of the bucket
legend where it read as a ninth bucket; thin decision coverage (<60%) now
counts as a degraded run so the Slack notice fires on a truncated response;
`_group_for_prompt` reordered score-primary so truncation costs the weakest
candidates rather than the oldest; blocked domains gain a jsonl audit and are
re-checked against the candidate pool, since the save-time filter is
forward-only; geo-less buckets excluded from `_priority_sub_bucket_names`; and
an unknown `Geography` cell is logged rather than silently coerced to Global.

**Not adopted:** nothing outright, but the deeper fix for partial responses
(retry or split the call) is deferred — the 60% coverage threshold makes the
failure *visible*, which is the part that mattered, and Perplexity shows no
truncation (5,693–7,651 completion tokens across 22 runs). Recorded as
FEEDBACK #15.

## Review round 3

Both blockers reproduced and fixed. Five of six suggestions fixed; one deferred
with a reason.

**Blocker: `filter_by_geo` re-selected from an already-capped, geo-blind cut.**
Reproduced at 120 candidates on a 70/20/10 split: the US channel got **6 items
and 1 of 8 bucket sections while 24 eligible US stories sat unused**. `_select`
discarded the full per-bucket lists, so no downstream filter could reach them.
`RankingResult` now carries `ranked_by_bucket` — every ranked candidate,
grouped and ordered — and each channel makes its own selection from that. Same
simulation after the fix: **21 items and 8/8 sections for both channels.**
The review was right that the re-selection alone couldn't fix this and that the
fix had to happen before the cap.

**Blocker: a zero-decision response was reported healthy.** The `bool(decisions)`
guard exempted the worst case. Confirmed: `{"stories": []}` and a response whose
`story_id`s match nothing both parse cleanly, so `parse_fallback` stays False,
and both shipped 21 wholly un-ranked stories with no Slack notice. Guard removed;
coverage 0 now counts as partial.

Also fixed: `--revert` on the backfill now matches `AND geo = ?` so it can only
undo its own writes, never blank a geo a later run re-derived; the
`unknown_geography` audit fires once per distinct value instead of once per feed
item and is wrapped so a logs-dir failure can't abort feed parsing; and the
`filter_by_geo` tests are now built from a **real** `rank_stories` result rather
than hand-made fixtures — the review correctly identified that the old fixtures
"describe a world without the per-bucket cap", which is exactly why the first
blocker survived a green suite. Coverage is now tested at 0/N as well as 1/N
and N/N.

**Deferred, with reason: the candidate ceiling applied before the filters**
(`candidate_pool_size` is a SQL `LIMIT`, so topicality and blocklist drops leave
the effective count below it — 71 of 120 on 3 Sept). The fix is to raise the
limit, but every extra candidate enters the ranking prompt, and that call
already took **331.8s against a 120s timeout and cleared the posting deadline by
7 seconds** (FEEDBACK #12). Raising it now trades a small recall gain for a
higher chance of a *late* digest, which is the worse failure. Recorded as
FEEDBACK #16, explicitly blocked on #12. Note this PR makes a bigger pool more
valuable than before, since each channel now selects from the whole ranked set —
so it is worth doing, after the latency work.

## Deploy notes

No env, schema, dependency or infra changes.

Three `tuning.xlsx` edits activate items 3 and 7 (SharePoint, no deploy). The
first must NOT be made until this is deployed, since the currently deployed code
rejects a bucket with empty `geos`:

1. `Priority Buckets` → add row 10: key `other_healthcare`, display
   `Other healthcare news`, `sub_buckets` `(catch-all)`, **`geos` blank**.
   `sub_buckets` cannot be empty (the loader requires it), but the value is
   inert: geo-less buckets are now excluded from `_priority_sub_bucket_names`,
   so even a placeholder that collides with a real Master Keywords sub-bucket
   name can't strip that sub-bucket's Track B coverage.
2. `Settings` → add `default_bucket` = `other_healthcare`.
3. `Settings` → add `blocked_domains` =
   `facebook.com; fb.com; fb.watch; instagram.com; x.com; twitter.com; linkedin.com; reddit.com; youtube.com; youtu.be`

   All-time pollution this removes: 98 signals / ~87 stories (facebook 46,
   instagram 25, linkedin 12, x 8, youtube 4, reddit 3), every one a Perplexity
   citation rather than a configured feed. Verified that nothing configured
   breaks: the 373 LinkedIn URLs in `voices.xlsx` are identity metadata for
   voices and firms, which are named inside prompts and never crawled, so they
   never become signal URLs. The one YouTube source (Newsletters row 32,
   `AHealthcareZ`) has produced zero signals in its entire history — feed
   discovery never worked on its `/c/` channel URL — so blocking `youtube.com`
   costs nothing live, though that row can now never work and may as well be
   deleted from the sheet.

One further edit is required **before** `ranker_provider` ever goes back to
`anthropic` (FEEDBACK #11): `anthropic_max_tokens_rank` 4096 → 8192.

A one-shot `scripts/backfill_story_geo.py` repairs the 1,186 pre-change
NULL-geo rows still in the 30-day pool (dry-run by default, writes a revert
snapshot on `--apply`). Verified plan: 804 → India, 374 → US, 8 → Global, none
left NULL.

Item 1a demotes that backfill from important to **optional**: the ranker
re-derives geo on every run regardless of what the DB holds, so a NULL row now
only matters if the ranker also omits that story's geo. Worth running anyway so
the fallback layer is sound, but it is no longer on the critical path.

**Behaviour change to expect:** the US digest stops receiving Indian RSS, so it
gets smaller and more American. On 2 Sept it was taking all 148 RSS signals
because they were unclassified; it would now take about 27.
