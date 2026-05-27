# Tuning the digest

Every knob that shapes the digest's character lives in one of three places:

| Surface | What you tweak there | Format |
|---|---|---|
| `src/config.py` | numeric thresholds, dedup window, booster weights, budget caps | Python constants — one literal each |
| `prompts/*.md` | the two LLM prompts (system message + magnitude rubric) | Markdown, read verbatim at startup |
| `inputs/*.xlsx` | keywords + voices the agent watches | Excel sheets |

This page is the index for the first two. The xlsx files are self-explanatory.

## The two prompts (biggest levers)

These are the highest-impact tweaks. Editing them is a copy edit, not a code change — open the file, change the text, commit. The agent loads them at startup.

### `prompts/ranker_system.md`
System message sent to `sonar-reasoning-pro` on every digest run.
- **What it controls:** tone, audience framing ("you are an editor for a VC firm"), how strictly the model interprets the magnitude rubric, output format constraint (JSON only).
- **When to tweak:** when the digest feels off-tone (too marketing-y, too breathless, too dry), or when the model is ignoring the rubric.

### `prompts/magnitude_rubric.md`
The S/A/B/C tier definitions injected into every ranker prompt.
- **What it controls:** what kinds of stories get included vs. dropped. All Tier S included → Tier A if room → Tier B only when a category would be empty → Tier C dropped.
- **When to tweak:** when the digest is too noisy (raise the bar for each tier) or too thin (lower it). Moving "$100M M&A" from S to A makes M&A coverage less guaranteed; moving "leadership move at smaller player" from B to C drops a whole class of stories.

## Numeric knobs in `config.py`

All grouped by purpose. Defaults shown.

### Budget
- `MAX_PERPLEXITY_CALLS_PER_DAY = 60` — hard cap; the fetch sweep stops early to leave room for the ranker call.
- `DAILY_BUDGET_USD = 3.0` — soft budget reference, currently used only for logging.

### Digest shape
- `MAX_DIGEST_ITEMS = 40` — sanity ceiling on total ranked stories. Typical days land 15–25.
- `TOP_SUMMARY_SIZE = 5` — count of stories promoted into the "Today's biggest stories" section at the top of the Slack post.

### Dedup
- `DEDUP_WINDOW_DAYS = 30` — how far back "recently sent" goes. Three different filters use it: URL dedup at signal ingestion, ranker candidate filter, and the cross-day embedding similarity check. Raise if you keep seeing repeats; lower if fresh-but-still-relevant stories are being squeezed out.
- `HISTORICAL_DEDUP_THRESHOLD = 0.80` — cosine similarity above this, between a new story and any sent-in-last-30-days story, drops the new one. Lower = more aggressive cross-outlet dedup; higher = let near-duplicates through.

### Scoring
- `CLUSTER_SIMILARITY_THRESHOLD = 0.85` — within-day clustering threshold. Signals with embedding cosine above this collapse into one story.
- `TOP_K_CONTENT_SIMILARITY = 5` — how many corpus chunks to compare against when measuring "does this sound like the firm." More = smoother score, slower.
- `SUMMARY_TRUNCATE_FOR_EMBED = 400` — characters of summary text fed to the embedding model. Bigger doesn't measurably help.

### Boosters (the `BOOSTERS` dict)

Additive adjustments to each story's base content-similarity score. Negative values are penalties. Inline regex for the text-pattern boosters; the three set-based ones (`tier1_voice`, `trusted_publication`, `firm_mention`) match against names/hosts loaded from `voices.xlsx`.

| Booster | Default | What triggers it |
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

**Tuning intuition:** boosters shape the *ordering* of candidates inside the ranker pool, but they don't directly determine what's in the digest. The LLM + magnitude rubric does final selection. If a category feels under-represented (e.g. M&A), raising its booster makes more M&A stories survive to the ranker; the rubric still has the final say. The ranker pool is currently 200 stories cap.

### Ranker prompt mechanics
- `MIN_CANDIDATE_SCORE = 0.0` — pre-filter on relevance_score before the LLM sees stories. 0.0 = no pre-filter. Worth raising only if cheap pre-filtering becomes important; otherwise the magnitude rubric is the right place to gate.
- `ONE_LINER_MAX_CHARS = 120` — hard cap on the one-line headline. Forces newsroom punchiness.
- `RANKER_SUMMARY_MAX_CHARS = 220` — how much of each story's summary the prompt shows the LLM.

### Models
- `PERPLEXITY_MODEL_FETCH = "sonar-pro"` — used for the 34-plan fetch sweep.
- `PERPLEXITY_MODEL_RANK = "sonar-reasoning-pro"` — used for the single ranking call.
- `PERPLEXITY_RECENCY = "day"` — recency filter on the fetch sweep.
- `EMBEDDING_MODEL = "text-embedding-3-small"` — for both content-corpus indexing and signal embedding. Switching models requires re-indexing the corpus.

### HTTP
- `HTTP_TIMEOUT_S = 30` — default for fetch calls.
- `HTTP_TIMEOUT_RANK_S = 120` — looser for `sonar-reasoning-pro` (it does extended chain-of-thought).
- `HTTP_MAX_RETRIES = 4`
- `URL_VALIDATION_TIMEOUT_S = 10` — HEAD validation budget per story before posting.

### Schedule
- `DIGEST_TZ = "Asia/Kolkata"`, `DIGEST_HOUR_LOCAL = 10` — the *intended* fire time (10am IST). The actual cron is in `.github/workflows/daily-digest.yml` (set to 04:30 UTC). GitHub Actions cron is best-effort with documented delays of 5–60+ minutes.

### Track B rotation
- `TRACK_B_PLANS_PER_DAY = 18`, `TRACK_B_ROTATION_DAYS = 14` — non-priority sub-buckets cycle through a 14-day rotation, 18 picks per day. Tuned so all ~245 (sub-bucket × geo) combinations are covered within the cycle.

## Where the structural choices live (NOT tweaks — code changes)

These shape the architecture, not the day-to-day output. Changing them is a code change, not a tuning operation.

- `PRIORITY_BUCKETS` (in `config.py`) — the 8 daily-tracked categories and which `sub_bucket × geo` plans they emit.
- `SOURCE_TIER_1` (in `config.py`) — ordered list of outlets used to pick the canonical URL when N URLs all describe the same story.
- The 34-plan structure (in `src/query_planner.py`) — Track A (priority buckets) + Track B (rotating sub-buckets) + voices/firms.

## Doing the tweak

1. Edit the value in `config.py` or the text in a `prompts/*.md` file.
2. `python -m unittest discover -s tests` to make sure nothing breaks.
3. `python src/main.py --test` to see the effect on Slack (posts with `[TEST]` marker, doesn't pollute dedup state).
4. Commit. GHA cron picks up changes on the next 04:30 UTC fire.
