# Editing guide — where do I change X?

Five files (well, three files and two folders) control everything about what the agent fetches, ranks, and posts. None of them require Python knowledge to edit. Pick the right one based on what you want to change.

**Two editing surfaces, split by file:**

- **`inputs/` → edit in SharePoint.** SharePoint is the source of truth for the
  four workbooks, `portfolio_context.md`, and the content corpus. Open the file
  in SharePoint, edit, save — done. Every run mirrors that folder down before it
  starts (`src/sharepoint_sync.py`), so the change is live on the **next run**
  with no commit and no deploy. The copies of these files in this repo are a
  seed for a fresh clone and the fallback if SharePoint is unreachable; editing
  them there does nothing lasting, because the next sync overwrites them.
- **`prompts/` → edit in the admin UI** (see `admin/README.md`) or directly in
  git. These are not in SharePoint. A prompt edit commits to this repo and takes
  effect on the **next deploy**, not the next run.

| Want to change… | Edit | Where |
|---|---|---|
| Numbers, thresholds, model names, timeouts, dedup window, priority bucket structure, source tier list | `inputs/tuning.xlsx` | **SharePoint** · open in Excel, edit cell, save |
| What keywords the agent searches for | `inputs/keywords.xlsx` | **SharePoint** · edit/add rows, save |
| Which named voices and newsletters the agent watches | `inputs/voices.xlsx` | **SharePoint** · edit/add rows, save |
| What "sounds like the firm" — the taste profile for relevance scoring | `inputs/content/*.md` | **SharePoint** · add/remove/edit the firm's articles, blog posts, interviews, etc. |
| Which portfolio companies the **weekly Sector Agent** watches (name, sector, what they do, geo) | `inputs/portfolio.xlsx` | **SharePoint** · edit/add rows, save |
| Deeper portfolio context — competitors, what materially moves each company | `inputs/portfolio_context.md` | **SharePoint** · edit, save |
| LLM tone & how strictly the ranker interprets the rubric | `prompts/ranker_system.md` | **Admin UI → Prompts** · or any text editor + commit |
| Which stories qualify as Tier S/A/B/C | `prompts/magnitude_rubric.md` | **Admin UI → Prompts** · or any text editor + commit |
| Sector Agent tone / what counts as "material impact" (Tier + direction) | `prompts/sector_system.md`, `prompts/sector_impact_rubric.md` | **Admin UI → Prompts** · or any text editor + commit |

After any edit:
- SharePoint edits need nothing further — the next scheduled run pulls them.
- Prompt edits need a deploy before the box sees them.
- A `--test` flag (`python src/main.py --test`) lets you run the full pipeline and see the Slack output with a `[TEST]` marker before the change goes live. Run `python src/sharepoint_sync.py` first if you want it to reflect a fresh SharePoint edit.

### Setting up / debugging the SharePoint sync

First-time Azure setup is a separate walkthrough: **`docs/sharepoint-setup.md`**.

The sync is off unless all five `SHAREPOINT_*` env vars are set (tenant id,
client id, client secret, site, inputs path — see `src/sharepoint_sync.py`). With
them unset it prints "not configured" and the agent runs on the `inputs/` in git,
which is what happens on a laptop by default.

- Preview what it would pull, without writing: `python src/sharepoint_sync.py --dry-run`
- Verify the app's site permission: `python scripts/grant_sharepoint_access.py --site <host:/sites/Name> --list`

A sync failure is never fatal — it warns and the run proceeds on the last
successfully-synced files, so a SharePoint or Azure outage can't stop the 08:00
digest. That also means a silently stale digest is possible: if a SharePoint edit
doesn't show up, check the run's log for a `WARN: sharepoint sync failed` line.

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
