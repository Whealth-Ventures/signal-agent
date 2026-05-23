# Signal Agent — How It Works (Plain English)

A daily news digest robot for healthcare investors. Every morning at 10am IST it
emails you the 5 most important healthcare stories of the last 24 hours. This
document explains what happens between "cron fires" and "email lands in inbox",
without assuming you've read the code.

---

## The big picture in one paragraph

You hand the agent two spreadsheets — one with ~2,280 keywords (e.g. "telehealth
reimbursement", "Ayushman Bharat", "GLP-1 manufacturing") and one with ~225
trusted voices (people, newsletters, company pages). The agent groups those
keywords into ~30 thematic searches, asks Perplexity (an AI search engine) to
find news matching each theme from the last 24 hours, also pulls RSS feeds from
the trusted publications, dedupes everything, scores each story for how well it
matches your firm's existing writing (the `content/` folder is your "taste
profile"), and finally asks Perplexity again — this time using its reasoning
model — to pick the best 5. Those 5 get rendered into an HTML email and sent.

---

## The 4-layer pipeline

Each layer hands work to the next. They're deliberately separate: if the email
ever looks bad, you can usually point at exactly one layer that's misbehaving.

### Layer 1 — Query Planner (`src/query_planner.py`)

**Input:** `inputs/keywords.xlsx` and `inputs/voices.xlsx`.
**Output:** ~30 "query plans" — each one a single, well-phrased question to ask
Perplexity.

Why not ask one question per keyword? 2,280 calls/day would cost ~$15+ and hit
rate limits. So the planner clusters keywords by **(geography, bucket)** —
e.g. all "India + Care Delivery Models" keywords become one query that names
the bucket and lists 3 sample keywords per sub-bucket. It also generates one
"voice-anchored" query per geography that names every Tier-1 voice by name —
because Tier-1 voices mostly post on LinkedIn/X where RSS doesn't reach.

This layer is **fully deterministic** — same Excel in, same plans out, byte for
byte. No LLM here. That's intentional: the planning logic is auditable.

### Layer 2 — Fetchers (Perplexity + RSS)

**Perplexity** (`src/perplexity_client.py`) — for each query plan, calls
Perplexity's `sonar-pro` model with `recency=day`. Perplexity searches the open
web in real time, reads the results, and returns a JSON list of stories
(title, URL, published date, 2-sentence summary). The client enforces a
**60 calls/day budget** (with a 2-call headroom kept aside for the ranker) and
a 0.5-second floor between calls so we don't get rate-limited.

**RSS** (`src/rss_fetcher.py`) — pulls the named newsletters from the voices
spreadsheet directly. This catches stories from sources we already trust,
without burning Perplexity calls on them.

Everything from both fetchers gets stored as a "Signal" in SQLite
(`data/db/agent.db`).

### Layer 3 — Dedupe + Score (`src/scorer.py`)

Two jobs:

1. **Dedupe.** The same story often shows up in 5 different feeds with
   different headlines. The scorer embeds each signal (using OpenAI's
   `text-embedding-3-small`), measures cosine similarity between embeddings,
   and any pair scoring above **0.85** is treated as the same story. Duplicates
   collapse into a single "Story" record. URLs already sent in the last
   **7 days** are dropped entirely (no story repeats within a week).

2. **Relevance score.** Each story gets compared against a vector index of
   your firm's `content/` folder (your published articles, blog posts,
   interviews, LinkedIn posts) — this is your "taste profile". Cosine
   similarity to the firm's content is the base score. Then deterministic
   **boosters** add or subtract:

   - +0.10 if a Tier-1 voice authored or is named in the story
   - +0.05 for funding / M&A / regulatory language
   - +0.03 for product launches or leadership moves
   - −0.10 for listicles ("10 Best…")
   - −0.05 for opinion/perspective columns

   The result is one number per story between roughly 0 and 1.

The corpus only gets embedded once — on first run, the agent indexes your
`content/` folder into a local Chroma vector DB at `data/vector_store/`. After
that it's reused.

### Layer 4 — Ranker (`src/ranker.py`)

Takes the **top 25** scored stories, hands their titles + summaries to
Perplexity's `sonar-reasoning-pro` model, and asks: "pick the 5 most
investor-relevant for a healthcare VC, and explain why in ≤280 chars each."
This is the only LLM call that does subjective judgment. If the model fails or
returns garbage, the agent falls back to "just take the top 5 by score" so the
pipeline never blocks on the ranker.

**Why Perplexity again instead of Claude?** To keep vendor count to one. You
already pay Perplexity for the fetchers; the ranker is a marginal cost
(~$0.02/day).

### Output — Email (`src/emailer.py`)

The 5 ranked stories get rendered into an HTML email via a Jinja2 template,
then sent via SMTP. **Before sending**, every URL is HEAD-checked — if a URL
404s or times out, that story is dropped from the email rather than shipping a
broken link. The digest is also recorded in SQLite so tomorrow's run knows
"don't repeat these".

---

## What the run on 2026-05-04 actually did (spoiler: it was truncated)

Looking at `data/logs/pipeline_2026-05-04.jsonl`, the most recent successful
end-to-end run had these characteristics:

| Field | Value | What it means |
|---|---|---|
| `plans_total` | **3** | Only 3 of ~30 query plans ran |
| `signals_collected` | 7 | Perplexity returned 7 stories |
| `rss_fetch_skipped` | true | RSS was skipped entirely |
| `calls_used_today` | 10 | Used 10 of 60 Perplexity calls |
| `ranked_count` | 5 | Top-5 picked |
| `stories_sent` | 4 | One was dropped at the URL-check step |

**This was a truncated run.** Specifically, it was launched with
`--max-plans 3 --skip-rss` (probably for fast iteration / testing). A full run
would have:

- Run **all ~30 query plans** (every (geo, bucket) combo + Tier-1 voice queries)
- Pulled **all RSS feeds** from the newsletters tab (~60 sources)
- Probably collected 100–300 raw signals before dedupe
- Cost roughly $1–2 instead of ~$0.05

To get a full run, just run it without flags: `python src/main.py`.

---

## Knobs you can turn to improve quality

Quality has three axes: **breadth** (how much we look at), **precision**
(how aggressively we filter junk), and **judgment** (which 5 we pick). Here's
what to tweak for each.

### To increase breadth (look at more)

| Knob | File | Default | Meaning |
|---|---|---|---|
| `MAX_PERPLEXITY_CALLS_PER_DAY` | `src/config.py` | 60 | Hard ceiling. Raise to 100 if you want more queries (and don't mind paying ~$1.50 more/day) |
| `KEYWORDS_PER_SUB_BUCKET_IN_PROMPT` | `src/query_planner.py` | 3 | How many sample keywords get listed in each Perplexity prompt. Raising to 5–7 gives Perplexity more lexical hooks |
| `PERPLEXITY_RECENCY` | `src/config.py` | `"day"` | Set to `"week"` to widen the time window (useful on Mondays / after holidays) |
| `inputs/keywords.xlsx` | — | ~2,280 keywords | Add new sub-buckets / keywords here to expand the search universe |
| `inputs/voices.xlsx` | — | ~225 sources | Add tier-1 voices / newsletters; the planner will pick them up automatically |
| `content/` folder | — | 58 files | More firm-published content = a sharper "taste profile" for relevance scoring |

### To improve precision (filter junk harder)

| Knob | File | Default | Meaning |
|---|---|---|---|
| `SIMILARITY_THRESHOLD` | `src/scorer.py` | 0.85 | Lower (e.g. 0.78) = collapse more "near-duplicates" together. Higher = treat slight variants as separate |
| `BOOSTERS` table | `src/scorer.py` | see file | Edit these to reweight what matters — e.g. bump `funding` to +0.10 if you only care about deals; make `listicle` more punitive |
| `DEDUPE_LOOKBACK_DAYS` | `src/config.py` | 7 | Increase to 14 to reduce repeats further (at the cost of slow-burn stories getting suppressed) |

### To improve judgment (smarter top-5 selection)

| Knob | File | Default | Meaning |
|---|---|---|---|
| `CANDIDATE_POOL_SIZE` | `src/ranker.py` | 25 | How many top-scored stories the ranker chooses from. Raise to 40 if you suspect good stories are getting cut at the score stage |
| `PERPLEXITY_MODEL_RANK` | `src/config.py` | `sonar-reasoning-pro` | Already the strongest reasoning model in Perplexity's lineup. No upgrade available — your lever here is the ranker prompt itself |
| `_SYSTEM_PROMPT` (ranker) | `src/ranker.py` | see file | The "what makes a good story" instructions sent to the ranker. Edit this to teach the ranker your firm's specific lens (e.g. "we care more about India scale-ups than US biotech") |
| `SUMMARY_MAX_CHARS_IN_PROMPT` | `src/ranker.py` | 200 | Each story's summary is truncated to 200 chars before going to the ranker. Raise to 400 if you think the ranker is missing nuance |
| `DIGEST_TOP_N` | `src/config.py` | 5 | Change the email length |

### Quick recipes

- **Email feels too India-light:** add more keywords to the US/Cross-Cutting tabs
  in `keywords.xlsx`, or duplicate certain US buckets to weight them more
  heavily in the plan list.
- **Email keeps recommending listicles:** make `BOOSTERS["listicle"]` more
  punitive (e.g. `-0.20`).
- **Email misses big stories you knew about:** the source probably wasn't in
  RSS and the keyword cluster missed it. Add the source to `voices.xlsx`
  newsletters tab.
- **Stories feel "generically newsy" not "investor-relevant":** the ranker
  prompt is the lever — edit `_SYSTEM_PROMPT` in `src/ranker.py` to add specific
  criteria ("prioritize stage and check size", "downweight clinical-trial
  readouts unless phase 3").
- **Want to test a change without sending email:** add `--dry-run`. The HTML
  digest gets written to `data/logs/dry_run_digest_<date>.html` and nothing
  hits SMTP or the digest table.

### CLI flags (for ad-hoc runs)

| Flag | Effect |
|---|---|
| `--max-plans N` | Run only the first N query plans. Used for fast tests. **This is the flag that truncated the recent run** |
| `--skip-rss` | Skip the RSS fetch entirely |
| `--skip-content-index` | Skip the first-run check that indexes `content/` |
| `--skip-url-validation` | Skip the HEAD-check on every story URL before email |
| `--dry-run` | Render HTML digest to disk, don't send email or persist digest record |

---

## Where things live on disk

```
inputs/
  keywords.xlsx        ← what we search for
  voices.xlsx          ← who we trust
content/               ← your firm's writing — the "taste profile"
src/                   ← the 10 modules
data/
  db/agent.db          ← SQLite: signals, stories, digests
  vector_store/        ← Chroma: embedded content/ corpus (built once)
  logs/                ← One JSONL per module per day, plus dry-run HTML files
.env                   ← API keys + SMTP creds
```

If something goes wrong, the logs in `data/logs/<module>_<date>.jsonl` are
your first stop — every API call, every signal, every cost is recorded there.
