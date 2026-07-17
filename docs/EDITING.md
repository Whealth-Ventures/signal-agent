# Editing guide — where do I change X?

Five files (well, three files and two folders) control everything about what the agent fetches, ranks, and posts. None of them require Python knowledge to edit. Pick the right one based on what you want to change.

| Want to change… | Edit | How |
|---|---|---|
| Numbers, thresholds, model names, timeouts, dedup window, priority bucket structure, source tier list | `inputs/tuning.xlsx` | Open in Excel, edit cell, save |
| What keywords the agent searches for | `inputs/keywords.xlsx` | Open in Excel, edit/add rows, save |
| Which named voices and newsletters the agent watches | `inputs/voices.xlsx` | Open in Excel, edit/add rows, save |
| LLM tone & how strictly the ranker interprets the rubric | `prompts/ranker_system.md` | Open in any text editor, edit, save |
| Which stories qualify as Tier S/A/B/C | `prompts/magnitude_rubric.md` | Open in any text editor, edit, save |
| What "sounds like the firm" — the taste profile for relevance scoring | `inputs/content/*.md` | Add/remove/edit the firm's articles, blog posts, interviews, etc. |

After any edit:
- If you edit via GitHub's web UI, commit on the same screen — the next 04:30 UTC cron run picks it up automatically.
- If you edit on your laptop, save and commit/push.
- A `--test` flag (`python src/main.py --test`) lets you run the full pipeline and see the Slack output with a `[TEST]` marker before the change goes live.

## `inputs/tuning.xlsx` — the four sheets

The single editing surface for every numeric/structural knob.

### Settings (24 rows)
Flat `name | value | description` table. Holds every scalar.

Highest-impact rows:
- `max_perplexity_calls_per_day` — hard ceiling. Raising costs more; lowering means fewer plans get to run.
- `cluster_similarity_threshold` — how aggressive within-day dedup is. Lower = more aggressive (different outlets covering the same story collapse).
- `historical_dedup_threshold` — same but across the 30-day window. Lower = harder for repeats to slip through.
- `top_summary_size` — how many stories appear in "Today's biggest stories" at the top of the post.
- `max_digest_items` — sanity ceiling on the whole digest. Typical days land 15-25; the ceiling rarely binds.

### Boosters (10 rows)
`name | weight | pattern_regex | description`. Each booster nudges a story's relevance score up or down. Negative weights are penalties.

Three boosters are "special" — `tier1_voice`, `trusted_publication`, `firm_mention`. Their `pattern_regex` cell is blank; the scorer matches them by name/host against rows in `voices.xlsx`. Don't try to add a pattern to those.

For the others, the regex is matched (case-insensitively) against title + summary. To add a new booster, add a row. To disable one without deleting it, set weight to 0.

### Priority Buckets (8 rows)
`key | display | sub_buckets | geos`. These are the eight daily-tracked categories. Each row produces one or more Perplexity queries (one per geo). The `sub_buckets` column references sheet names from `keywords.xlsx` — semicolon-separated if a bucket maps to multiple sub-buckets.

To add a ninth category: add a row with a unique kebab-case `key`, a display name, the matching sub-buckets from `keywords.xlsx`, and the geos (`India`, `US`, `Global`, or a semicolon-separated combination).

### Source Tiers (36 rows)
`host`. Ordered list. When dedupe collapses N URLs about the same story, the one whose host appears earliest wins the canonical link in the Slack post.

To bump a source up: cut its row, paste it higher. To add a new trusted outlet: add the host (no `https://`, no `www.`).

## Two channels (India / US split)

The agent posts a geo-scoped digest to two Slack channels from the same app,
selected by `python src/main.py --geo {india,us,both}`:

- **`india`** → India + Global stories → **Signal Agent India** (08:00 IST)
- **`us`** → US + Global stories → **Signal Agent US** (08:00 America/New_York)
- **`both`** (default) → everything → single channel (legacy)

`Global` (all AI-in-Healthcare, Hot-TAs, cross-cutting) and unclassified RSS go
to **both** channels. Each geo run does its own deep sweep (Track B is scoped to
that geo's sub-bucket universe), so each channel is a full digest.

Where to change things:

- **Channel IDs** → env / AWS Secrets Manager (`signal-agent/prod/agent-env`):
  `SLACK_CHANNEL_ID_INDIA`, `SLACK_CHANNEL_ID_US` (both fall back to
  `SLACK_CHANNEL_ID`). The same `SLACK_BOT_TOKEN` powers both — the bot must be
  **invited to each channel** (`/invite @signal_agent`).
- **Channel labels** → `SLACK_CHANNEL_LABEL_INDIA` / `_US`.
- **Post times / timezones** → see `docs/scheduling.md` (`DIGEST_POST_AT`,
  `DIGEST_TZ` for India, `DIGEST_TZ_US` for US; two systemd timers).
- **Per-geo depth** → `track_b_plans_per_day` / `track_b_rotation_days` in
  `inputs/tuning.xlsx` now apply per geo run. Lower `track_b_rotation_days` (or
  raise `max_digest_items`) to pack each channel deeper.

## Everything else is unchanged

- `inputs/keywords.xlsx` — single `Master Keywords` tab; columns Bucket / Sub-bucket / Keyword / Geo.
- `inputs/voices.xlsx` — five tabs (Overview, India Top Voices, US Top Voices, Newsletters & Publications, Firms & Org Pages, New Additions).
- `prompts/ranker_system.md` and `prompts/magnitude_rubric.md` — markdown, loaded verbatim, passed straight to the LLM.
- `inputs/content/` — the firm's published content corpus. The `content_indexer` hashes each file; only changed files get re-embedded on the next run.

## What's *not* edited in the xlsx (and why)

A few things still live in code, not in `tuning.xlsx`:

- **The 4-layer architecture** (query planner → fetchers → scorer → ranker). Structural, not a tuning knob.
- **Source code paths and env-var names** (`config.py` upper section). Code-adjacent.
- **The PriorityBucket data structure** (`src/tunables.py`). The data lives in the xlsx; the dataclass that holds it is code.

If you want to rewire how the agent works at a deeper level, that's a code change — open an issue or a PR.

## Restoring defaults

If `tuning.xlsx` gets into a bad state, regenerate it from the in-code defaults:

```bash
python scripts/build_default_tuning_xlsx.py --force
```

This overwrites the file. The original literal values are also visible in `scripts/build_default_tuning_xlsx.py`.
