# Tuning the digest

Every knob that shapes the digest's character lives in one of three surfaces. All of them are file-edits, not code-edits.

| Surface | What you tweak there | Format |
|---|---|---|
| `inputs/tuning.xlsx` | numeric thresholds, dedup window, booster weights, priority bucket structure, source tier list, budget caps, model names | Excel — four sheets |
| `prompts/*.md` | the two LLM prompts (system message + magnitude rubric) | Markdown, read verbatim at startup |
| `inputs/*.xlsx` | keywords + voices the agent watches | Excel sheets |

For a one-page map of "I want to change X, where do I go?", see [EDITING.md](EDITING.md).

## The two prompts (biggest character levers)

These are the highest-impact tweaks. Editing them is a copy edit, not a code change.

### `prompts/ranker_system.md`
System message sent to `sonar-reasoning-pro` on every digest run.
- **What it controls:** tone, audience framing ("you are an editor for a VC firm"), how strictly the model interprets the magnitude rubric, output format constraint (JSON only).
- **When to tweak:** when the digest feels off-tone (too marketing-y, too breathless, too dry), or when the model is ignoring the rubric.

### `prompts/magnitude_rubric.md`
The S/A/B/C tier definitions injected into every ranker prompt.
- **What it controls:** what kinds of stories get included vs. dropped. All Tier S included → Tier A if room → Tier B only when a category would be empty → Tier C dropped.
- **When to tweak:** when the digest is too noisy (raise the bar for each tier) or too thin (lower it). Moving "$100M M&A" from S to A makes M&A coverage less guaranteed; moving "leadership move at smaller player" from B to C drops a whole class of stories.

## Numeric knobs in `inputs/tuning.xlsx` → Settings sheet

All grouped by purpose below. Defaults shown. To change a value: open `inputs/tuning.xlsx`, go to the **Settings** sheet, find the row by name, change the value cell, save.

### Budget
- `max_perplexity_calls_per_day = 60` — hard cap; the fetch sweep stops early to leave room for the ranker call.
- `daily_budget_usd = 3.0` — soft budget reference, currently used only for logging.

### Digest shape
- `max_digest_items = 22` — ceiling on total ranked stories. With `target_digest_min` as the floor, most days land in a tight band just under this.
- `target_digest_min = 18` — floor on total ranked stories. If the normal keep-all-S / keep-A selection lands below this, the ranker backfills with the best remaining Tier-B stories (highest tier→score first, across every category including "Other") until the floor is hit or candidates run out. This is what keeps slow news days from collapsing to ~10 stories. Tier-C is never backfilled, so quality holds. Set to `0` to disable the floor and get pure threshold behaviour. Keep it below `max_digest_items`.
- `top_summary_size = 5` — count of stories promoted into the "Today's biggest stories" section at the top of the Slack post.

### Dedup
- `dedup_window_days = 30` — how far back "recently sent" goes. Three different filters use it: URL dedup at signal ingestion, ranker candidate filter, and the cross-day embedding similarity check. Raise if you keep seeing repeats; lower if fresh-but-still-relevant stories are being squeezed out.
- `historical_dedup_threshold = 0.80` — cosine similarity above this, between a new story and any sent-in-last-30-days story, drops the new one. Lower = more aggressive cross-outlet dedup; higher = let near-duplicates through.

### Scoring
- `cluster_similarity_threshold = 0.85` — within-day clustering threshold. Signals with embedding cosine above this collapse into one story.
- `top_k_content_similarity = 5` — how many corpus chunks to compare against when measuring "does this sound like the firm." More = smoother score, slower.
- `summary_truncate_for_embed = 400` — characters of summary text fed to the embedding model. Bigger doesn't measurably help.

### Boosters (Boosters sheet)

Additive adjustments to each story's base content-similarity score. Negative values are penalties. The Boosters sheet has columns `name | weight | pattern_regex | description`.

| Booster | Default weight | What triggers it |
|---|---|---|
| `tier1_voice` | +0.10 | Story mentions a Tier-1 voice from `voices.xlsx` |
| `trusted_publication` | +0.08 | Story URL is from a host listed under newsletters in `voices.xlsx` |
| `firm_mention` | +0.08 | Story mentions a firm from the New Additions tab |
| `funding` | +0.05 | Title/summary mentions raises/series/funding |
| `m_and_a` | +0.05 | Title/summary mentions acquires/merges/m&a |
| `regulatory` | +0.05 | Mentions FDA/CDSCO/EMA/approved/cleared |
| `product` | +0.03 | Mentions launches/unveils/debuts |
| `leadership` | +0.03 | Mentions appoints/named/joins/hires |
| `listicle` | **−0.10** | Title starts with `N best/top/essential/...` |
| `opinion` | **−0.05** | Title starts with `Opinion:/Perspective:/Column:` |

The first three (`tier1_voice`, `trusted_publication`, `firm_mention`) have a blank `pattern_regex` — they're matched by name/host against rows in `voices.xlsx`, not by regex. Don't put a pattern in those rows.

The pattern column for the other boosters is a Python regex matched case-insensitively against the title + summary.

**Tuning intuition:** boosters shape the *ordering* of candidates inside the ranker pool, but they don't directly determine what's in the digest. The LLM + magnitude rubric does final selection. If a category feels under-represented (e.g. M&A), raising its booster makes more M&A stories survive to the ranker; the rubric still has the final say. The ranker pool is currently 200 stories cap.

### Ranker prompt mechanics
- `min_candidate_score = 0.0` — pre-filter on relevance_score before the LLM sees stories. 0.0 = no pre-filter. Worth raising only if cheap pre-filtering becomes important; otherwise the magnitude rubric is the right place to gate.
- `one_liner_max_chars = 120` — hard cap on the one-line headline. Forces newsroom punchiness.
- `ranker_summary_max_chars = 220` — how much of each story's summary the prompt shows the LLM.

### Models
- `perplexity_model_fetch = sonar-pro` — used for the 30+-plan fetch sweep.
- `perplexity_model_rank = sonar-reasoning-pro` — used for the single ranking call.
- `perplexity_recency = day` — recency filter on the fetch sweep.
- `embedding_model = text-embedding-3-small` — for both content-corpus indexing and signal embedding. Switching models requires re-indexing the corpus.

### HTTP
- `http_timeout_s = 30` — default for fetch calls.
- `http_timeout_rank_s = 120` — looser for `sonar-reasoning-pro` (it does extended chain-of-thought).
- `http_max_retries = 4`
- `url_validation_timeout_s = 10` — HEAD validation budget per story before posting.

### Schedule
- `digest_tz = Asia/Kolkata`, `digest_hour_local = 10` — the *intended* fire time (10am IST). The actual cron is in `.github/workflows/daily-digest.yml` (set to 04:30 UTC). GitHub Actions cron is best-effort with documented delays of 5–60+ minutes.

### Track B rotation
- `track_b_plans_per_day = 18`, `track_b_rotation_days = 14` — non-priority sub-buckets cycle through a 14-day rotation, 18 picks per day. Tuned so all ~245 (sub-bucket × geo) combinations are covered within the cycle.

## Structural levers in `inputs/tuning.xlsx`

### Priority Buckets sheet

The eight daily-tracked categories. Columns: `key | display | sub_buckets | geos`. `sub_buckets` is semicolon-separated to map one bucket to multiple keyword sub-buckets (e.g. AI in Healthcare → three sub-buckets). `geos` is semicolon-separated from {India, US, Global}.

Adding a row creates a ninth category. Removing one drops it (and all its plans) from the daily sweep. Reordering changes the order in the Slack post.

### Source Tiers sheet

`host`, one per row, in priority order. When dedupe collapses N URLs about the same story, the URL whose host matches earliest in this list wins the canonical link.

To bump a source up: cut its row, paste higher. To add a new trusted outlet: add the host (no `https://`, no `www.`).

## Doing the tweak

1. Edit the value in `inputs/tuning.xlsx` (or the text in a `prompts/*.md` file).
2. `python -m unittest discover -s tests` to make sure nothing breaks (optional but recommended for structural changes).
3. `python src/main.py --test` to see the effect on Slack (posts with `[TEST]` marker, doesn't pollute dedup state).
4. Commit. GHA cron picks up changes on the next 04:30 UTC fire.

## Restoring defaults

If `tuning.xlsx` gets into a bad state:

```bash
python scripts/build_default_tuning_xlsx.py --force
```

The original literal values are visible in `scripts/build_default_tuning_xlsx.py`.
