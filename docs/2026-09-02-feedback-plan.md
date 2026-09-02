# Plan — 2 September 2026 digest feedback

Feedback from Ashwin Krishnan and Shirish Sharma on the 2 Sept India digest,
raised in the Signal Agent India channel.

| # | Raised | Verbatim |
|---|---|---|
| A | Ashwin | "this is still predominantly global" / "Not the right logic" |
| B | Shirish | "we're missing Ultrahuman funding news in the alerts (which would be a priority news for us to capture)" |
| C | Shirish | "Venture & IPO category has an opinion piece on Diabetes association" |

## Diagnosis (verified on the box, 2 Sept 2026, read-only)

### The dominant cause: the ranker made zero LLM calls

`data/logs/anthropic_2026-09-02.jsonl` holds exactly one entry, 02:22:31 UTC:

```
HTTP 400 invalid_request_error
"This organization has been disabled. An organization admin can appeal at
 https://console.anthropic.com/appeal"
error_code: organization_on_hold
```

`ranker_2026-09-02.jsonl` → `used_fallback=True`, `model=null`, `cost_usd=0.0`,
91 candidates, 14 ranked in 1.4s. `ranker_provider` is still `anthropic` in
`tuning.xlsx`, so the Perplexity path was never taken either.

So the digest shipped with **no LLM tiers, no LLM bucket assignment, no LLM
one-liners**. All three complaints are downstream of that. See FEEDBACK #9.

### The Claude ranker never worked, even before the org hold

Found while triaging the headline-rewrite PR review, 2 Sept, from the box's own
call logs. `anthropic_max_tokens_rank` is **4096**, and the rank response needs
roughly **7000**:

| Path | Dates | completion_tokens | Fallback rate |
|---|---|---|---|
| `claude-sonnet-4-5` | 8 to 13 Aug | **4096 on 10 of 11 calls** (cap) | ~10 of 11 |
| `sonar-reasoning-pro` | 18 Jul to 8 Aug | 5,693 to 7,651 | **0 of 22** |

Pinned at exactly the cap means the JSON was truncated mid-object, so
`_extract_json` returned `None` and `parse_ranked` reported
`parse_fallback=True`. That surfaces as `used_fallback=True` **with an empty
`call_error`**, which in the console is indistinguishable from a vendor outage.
From roughly 14 Aug the org/billing failures took over and masked it entirely.

Two consequences:

1. **Step 0 is not a stopgap.** Perplexity is the only ranker configuration that
   has ever parsed successfully in this repo. 22 for 22.
2. **Flipping back to `anthropic` after the org appeal will silently reproduce
   this**, unless `anthropic_max_tokens_rank` goes to 8192 or higher first.
   Recorded as a FEEDBACK item; do not flip back without it.

### B — the sources were right, selection dropped the story

Ultrahuman was caught three times over:

| Source | Title |
|---|---|
| Entrackr (RSS) | Exclusive: Qualcomm Ventures to lead $60 Mn round for Ultrahuman |
| Perplexity `tb__longevity_and_healthspan__india` | Wearable health startup Ultrahuman plans Rs 583 crore Series C round led by Qualcomm Ventures |
| Perplexity `tb__mental_wellness_consumer__india` | Wearable health startup Ultrahuman eyes higher valuation in new funding round |

All three clustered into one story: `geo=India`, **relevance_score 0.676, the
second-highest of 160 stories that day.** It did not make the digest.

Cause: on the fallback path every story is Tier A, and both selection sort keys
put **recency primary and relevance_score as a deep tiebreak**:

- `_ordered_within_category` — [`src/ranker.py:366`](../src/ranker.py#L366)
  `key=(tier, -published_at, -relevance_score)`
- `_top_summary` — [`src/ranker.py:377`](../src/ranker.py#L377)
  `pool.append((tier, -published_at, bucket_order, -relevance_score, ...))`
  with the inline comment `# recency primary (post-#5)`

With `per_bucket_max=2`, Ultrahuman lost its Venture & IPO slot to the STAT
op-ed and a smart-walker study purely because they published a few hours later.
Same mechanism dropped **"Even Healthcare raises $22M Series B" at 0.72, the
day's single highest-scored story.**

Net effect: with the LLM dead, the digest is "the 14 most recently published
stories, 2 per bucket". Score is decorative.

### C — Venture & IPO is the catch-all bucket

[`_default_bucket_key()`](../src/ranker.py#L73) returns `PRIORITY_BUCKETS[0]`,
and `venture_ipo` is row 1 of the Priority Buckets tab. With no LLM bucket
assignment, every story lacking a Track-A `priority_bucket` lands there.
`bucket_default_assigned` fired in today's ranker log. The diabetes op-ed had no
bucket, landed in Venture & IPO, and won a slot on recency.

B and C are the same two lines of code.

### A — geo is never derived from the story

Geo is inherited from the **query plan that found the story**
([`src/main.py:126`](../src/main.py#L126) stamps `raw["geo"] = plan.geography`),
majority-voted per cluster by
[`scorer.pick_geo`](../src/scorer.py#L253). RSS carries no geo at all, so it
comes out `NULL`, renders `[GLOBAL]`
([`slack_client._GEO_TAG`](../src/slack_client.py#L56)) and is routed to **both**
channels ([`ranker.filter_by_geo`](../src/ranker.py#L600) keeps `None`).

Story pool on 2 Sept vs what shipped:

| | NULL | India | Global | US |
|---|---|---|---|---|
| Stories created | 105 | 38 | 12 | 2 |
| In the digest | 1 | 5 | 8 | 0 |

Proof this is mislabelling rather than global news: *"VSEZ sterile oncology
manufacturing facility clears U.S. FDA inspection"* is Gland Pharma in
Visakhapatnam, tagged `[GLOBAL]`.

Second driver: `hot_tas` and `ai_healthcare` are **Global-only buckets** in
`tuning.xlsx`, so their pharma-PR stories always carry `[GLOBAL]` and always get
guaranteed slots.

Ashwin's "not the right logic" is correct. Global is not a fallback bucket by
design; it is what every unclassified story silently becomes.

### Related: a degraded digest is invisible in Slack

The run prints `(used score-based fallback — ranker call failed or output was
unparseable)` to the console only, [`src/main.py:389`](../src/main.py#L389).
Nothing reaches Slack. This entire feedback thread exists because a broken
ranker looks exactly like a healthy digest. FEEDBACK #4, now proven twice in
one month.

## Plan

Branch: `feat/subhanu` off `main`, cut **after** the headline-rewrite PR merges
so it carries that work. Release to `main` as a single `[minor]` PROD release.

### Step 0 — flip the ranker to Perplexity (no code, do now)

`inputs/tuning.xlsx` → `Settings` sheet → `ranker_provider`: `anthropic` →
`perplexity`. Edit in SharePoint; the next run syncs it. No deploy.

Restores tiers, LLM bucket assignment and LLM one-liners, and activates the
headline rewrite once it merges. Budget is fine: the 2 Sept India run used 27 of
60 Perplexity calls, and this adds 2 (rank + headline).

Revert to `anthropic` once the org hold is lifted (FEEDBACK #9).

### Step 1 — stamp RSS signals with their publication's geography

`Newsletter.geography` is already parsed
([`query_planner.py:261`](../src/query_planner.py#L261)) and the values are
clean: `India` 22, `US` 24, `Global` 1. `_parse_feed_body` simply never receives
it.

- [`src/rss_fetcher.py:217`](../src/rss_fetcher.py#L217) `_parse_feed_body` —
  add `geography: str = ""`, stamp `raw["geo"]` when set.
- Plumb `geography` through `fetch_feed` (:264), `_fetch_feed_async` (:296) and
  the async newsletter loop (:386, has `nl` in scope).
- Map: `India` → `India`, `US` → `US`, anything else → `Global` (unknown
  degrades to today's routing, not to a dropped story).

`scorer.pick_geo` and `_geo_tag` then work unchanged.

**Expected effect on the 2 Sept data:** ~108 of 148 RSS signals become `India`
(Medical Dialogues 50, Entrackr 19, DHN 12, MediaNama 10, BioSpectrum 6,
Express Pharma 4, IndiaMedToday 3, Business Standard IPO 2, eHealth 2), 20
become `US` (STAT ×2), 1 `Global`.

**Risk, must be measured before merge:** India-tagged stories stop being routed
to the US channel. The US digest currently receives all 148 RSS signals as
`NULL`; afterwards it receives ~21. The India digest gains correct tags and
loses the 20 STAT items. Verify with a `[TEST]` run of **both** geos and compare
story counts before merging. Mitigation if the US digest goes thin: its own
Perplexity US + Global plans are unaffected, and 24 of 47 publications are
US-geo, so the shortfall is a same-day artefact rather than structural.

### Step 2 — rank by score within tier, not by recency

- [`src/ranker.py:366`](../src/ranker.py#L366) `_ordered_within_category` →
  `key=(tier, -relevance_score, -published_at)`
- [`src/ranker.py:377`](../src/ranker.py#L377) `_top_summary` → swap
  `-published_at` and `-relevance_score` in the tuple.

Deliberate reversal of the earlier "recency primary" decision. Justification:
signals are already windowed to the last 24 hours at fetch time, so recency
within the digest is a weak discriminator, while relevance_score carries the
corpus-similarity and booster signal. Recency stays as the tiebreak.

This is the direct fix for B, and it holds on the LLM path too, not just the
fallback.

**Test:** the Ultrahuman case as a unit test. Two stories in one bucket,
`per_bucket_max=1`, both Tier A: the higher-scored older story must win.

### Step 3 — stop misfiling unbucketed stories into Venture & IPO

`geos` cannot be empty ([`tunables.py:220`](../src/tunables.py#L220) raises), so
a display-only 9th bucket needs one relaxed validation. Three parts:

- **3a.** [`tunables.py:220`](../src/tunables.py#L220) — allow empty `geos`,
  meaning "display-only bucket, emits no query plans". `_build_track_a_plans`
  already loops `for geo in bucket.geos`, so an empty tuple emits nothing and
  costs no Perplexity calls.
- **3b.** [`ranker.py:73`](../src/ranker.py#L73) `_default_bucket_key()` — read a
  new `default_bucket` tunable, validated against the live bucket keys, falling
  back to `PRIORITY_BUCKETS[0]` if unset or invalid. The catch-all becomes a
  deliberate choice instead of a spreadsheet row order.
- **3c.** `tuning.xlsx` → `Priority Buckets`: add a final row
  `other_healthcare | Other healthcare news | (placeholder) | (geos blank)`, and
  `Settings` → `default_bucket = other_healthcare`.

Because `_valid_bucket_keys()` is derived from the same tunable, the ranker
prompt also gains `other_healthcare` as a legal choice, so the **LLM** gets
somewhere honest to put non-deal health news instead of being forced to pick a
deal bucket. That fixes C on both the LLM and the fallback path.

Optional add-on, decide during implementation:

- **3d.** When the LLM call succeeded and the LLM declined to bucket a story,
  drop it rather than defaulting. Omission from a response that saw all 91
  candidates is a signal. Do **not** apply on the fallback path, where nothing
  has an LLM bucket and the digest would collapse to Track-A stories only.
- **3e.** One line in `prompts/ranker_system.md` telling the ranker to tier
  op-eds, opinion and "perspective" pieces below news. Prompt-only, no code.

### Step 4 — surface a degraded run in Slack

Add the fallback notice to the Slack post, not just the console
([`src/main.py:389`](../src/main.py#L389)) — one line when `used_fallback` is
true or `perplexity_calls == 0`. Closes FEEDBACK #4. Cheap, and it is the reason
this feedback took a month to surface.

### Step 5 — cap the global share, only if still needed

Ashwin suggested "only 5 global news articles at max". Deliberately last: after
step 1 most of the false global disappears, so the cap may be unnecessary and
would otherwise mask the real problem.

If the firm still wants less global pharma PR after steps 1 to 4, the first
lever is `tuning.xlsx`, not code: change `hot_tas` and `ai_healthcare` off
`Global`-only, or drop their weight. No deploy needed.

### Step 6 — headline-rewrite PR review follow-ups

The headline-rewrite PR was approved with eight suggestions and no blockers, so
they ride here rather than blocking that merge. Two are worth doing **before**
the first dry run, because each silently disables the rewrite for a whole run
and we would misread the result as "the feature does nothing".

- **6a (do first).** [`headline_rewriter.py:93`](../src/headline_rewriter.py#L93)
  catches only `httpx.HTTPError`, and `httpx.InvalidURL` is **not** a subclass
  (verified on httpx 0.28.1: its MRO is `InvalidURL → Exception`). With no
  `return_exceptions=True` on the `asyncio.gather` at
  [:97](../src/headline_rewriter.py#L97), **one malformed URL aborts all 25
  fetches** and the whole step no-ops. Prod URLs already include odd shapes
  (NSE PDF archives, mismatched slugs). Fix: broaden the except and pass
  `return_exceptions=True`.
- **6b (do first).** Log the response snippet on a parse failure, the way the
  ranker does with `response_text[:2000]`
  ([`ranker.py:569`](../src/ranker.py#L569)). Right now a parse failure and "the
  LLM kept every headline" both log `rewritten: 0` and nothing else, which is
  precisely the blind spot that hid the max-tokens truncation above for five
  days.

The remaining six are ordinary cleanups:

- Build the vendor client before the 25-article fetch
  ([:161](../src/headline_rewriter.py#L161)) so a no-op costs nothing.
- Truncate an over-length headline at a word boundary instead of discarding it
  ([:112](../src/headline_rewriter.py#L112)); the ranker already truncates
  ([`ranker.py:287`](../src/ranker.py#L287)).
- Replace the hardcoded `MAX_HEADLINE_CHARS = 90`
  ([:41](../src/headline_rewriter.py#L41)) with the existing
  `config.ONE_LINER_MAX_CHARS` tunable.
- Guard the `_log` call inside the fail-soft handler
  ([:210](../src/headline_rewriter.py#L210)); `run_pipeline` is `try/finally`
  with no `except`, so a logs-dir write failure there can still abort the run.
- Add the two missing test cases: a real `_fetch_excerpts` failure path, and a
  fixture with stories in `by_priority` rather than everything in
  `top_summary`.
- Add `resp.estimated_cost_usd` from the rewrite call into the pipeline's
  `cost_usd` summary, which currently understates each run by one
  ranker-class call.

Not adopted: the prompt-injection note on the 2,000-char excerpt
([:169](../src/headline_rewriter.py#L169)). Real but bounded by `_escape_mrkdwn`
and the length cap to one misleading line, with no link or markup escape. Revisit
if we ever let the rewrite emit URLs.

### Step 7 — raise the Anthropic rank token cap

`anthropic_max_tokens_rank`: 4096 → 8192, in `tuning.xlsx` Settings. Pure
spreadsheet change, no deploy. Required **before** anyone flips
`ranker_provider` back to `anthropic`, per the truncation finding above.
Verify by checking that the next Claude rank call reports
`completion_tokens` below the new cap.

## Verification before the PR

1. `[TEST]` India run and `[TEST]` US run on the branch, with
   `ranker_provider=perplexity`. Record story count, geo mix, and per-bucket
   contents for both, before and after.
2. Ultrahuman and "Even Healthcare raises $22M" must appear in an India run
   replayed against 2 Sept data.
3. No story in a deal bucket without an assigned bucket.
4. Full offline test suite green.

## Release

Single `[minor]` PR from `feat/subhanu` to `main`. FEEDBACK items #4 and #10
(if the flock fix rides along) move to `RELEASE_NOTES.md` in the same PR, per
the repo rule.

## Out of scope

- FEEDBACK #9, the Anthropic org hold. Org-admin appeal, external to this repo.
  Step 0 routes around it.
- FEEDBACK #10, the Monday SharePoint sync race. Still deferred to the next
  sector-agent work.
- FEEDBACK #2, `sa-fetch.sh` deletion propagation. Unrelated, still open.
