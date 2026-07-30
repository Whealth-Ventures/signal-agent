# Sector Digest — Design & Implementation Plan

**Status:** Proposed (planning). Not yet implemented.
**Author:** drafted with Claude, 2026-07-22.

A bi-weekly, sector-scoped roundup that summarizes everything substantive that
happened in a portfolio company's market over the **last 14 days**. Distinct
from the daily India/US digests: different taxonomy, cadence, dedup namespace,
and output shape.

## Decisions locked

| Decision | Choice |
|---|---|
| Delivery | **One Slack channel, 7 separate messages** per cycle (one per sector) |
| Cadence | **1st & 15th of each month**, one orchestrated run, 08:00 IST |
| Dedup | **Fully independent** per sector — a story may appear in both a daily digest and the sector roundup |
| Magnitude bar | **Sector-relative** — no "≥$10M" floor; small raises are news in a narrow sector |
| Output | Flat bullet list grouped by **event type** (raises → M&A → clinical/regulatory → policy/legal → partnerships → launches) |

## Sectors

| # | Sector | Portco | Primary geo | Secondary geo (biggest only) | Notes |
|---|---|---|---|---|---|
| 1 | Mental / Behavioral Health | Everbright | US | — | Some focus on interventional psychiatry (TMS, ketamine, etc.) |
| 2 | Pediatrics | Hoola Health | India | US (≤3 S-tier) | |
| 3 | Diabetes | Beato | India | — | |
| 4 | Pain Management | Nivaan Care | India | — | |
| 5 | Parenting | Mylo | India | — | Consumer/health blend — watch topicality |
| 6 | Doctor-led medical weight loss | ElevateNow | India | — | GLP-1 / obesity clinics |
| 7 | Oncology | Everhope | India | US (≤3 S-tier) | |

## The critical prerequisite: stream-scoped dedup (Phase 0)

Today dedup is **global**:
- `storage.recently_sent_urls()` and `recent_story_embeddings()` join `digests`
  with **no channel/stream filter** — any story sent anywhere in the last 30
  days is suppressed everywhere.
- `scorer.run_scoring()` and `ranker.rank_stories()` call these unscoped.
- `has_sent_digest_for_date()` is per-channel, but 7 sector messages share one
  channel, so a per-channel key would collide.

Fix: introduce a **`stream`** identity on digests.
- Add nullable `stream TEXT` column to `digests` (idempotent migration).
- Streams: `geo:india`, `geo:us`, `both` (daily); `sector:<key>` (sector).
- Add optional `stream=` filter to `recently_sent_urls`,
  `recent_story_embeddings`, and `has_sent_digest_for_date`.
- Thread a `stream` param through `scorer.run_scoring` and
  `ranker.rank_stories` (default `None` = legacy global behavior, so the daily
  pipeline is unchanged unless we opt it in).

This single change gives independent per-sector dedup and correct 7-message
idempotency.

> Note: the `stories` table is shared and keyed by URL hash. A URL fetched by
> both the daily and sector runs shares one row; `upsert_story` would overwrite
> `geo`/`bucket`/`priority_bucket`. Store the sector **event-type** in a
> separate field (reuse `bucket` or add `event_type`) so it never clobbers the
> daily `priority_bucket`. Selection is driven by `digest_stories` + `stream`,
> so this is low-risk metadata churn — but keep event-type out of
> `priority_bucket`.

## Inputs — `inputs/sectors.xlsx` (new)

Follows the "Excel is source of truth, admin UI edits it" pattern.

- **Sectors** tab: `key`, `display`, `portco`, `primary_geo`, `secondary_geo`,
  `secondary_max_stories`, `target_story_count`, `notes`.
- **Sector Keywords** tab: `Sector`, `Keyword`, `Geo` (mirrors Master Keywords).
- **Sector Watchlist** tab: `Sector`, `Company/Competitor` — named directly in
  the prompts. High leverage: naming competitors is what makes the existing AI
  plans recall well. For Beato/Nivaan/Mylo/ElevateNow this sharpens results a
  lot.

Channel id lives in config/env (single shared channel), not per-sector.

## Layer-by-layer changes

### Query planner (`src/query_planner.py`)
- New `load_sectors()` + `build_sector_plans(sector, today)`.
- Per sector, ~4 **facet** prompts (raises/M&A, clinical/regulatory,
  policy/legal/enforcement, product/partnerships/launches), each anchored on
  sector keywords + watchlist names, **14-day lookback**, **no size floor**.
- Secondary-geo sectors (peds, onc) get one extra "biggest US stories" prompt.
- Plans carry `track="sector"`, `sector=<key>`, and a `lookback_days`/`recency`
  hint (see fetch note).

### Fetch (`src/perplexity_client.py`)
- Verify/add per-plan **recency** control. Daily is hardcoded ~`day`; sector
  needs `week`/`month` + a hard published-date cutoff (`>= now - 14d`).
- Handle null `published` from Perplexity: keep, let the ranker drop stale ones
  (recency filter already bounds the window).
- Budget: sector run uses its own `PerplexityClient(scope="sector")`.

### Topicality (`src/topicality.py`)
- Keep the healthcare gate; add a **per-sector keyword gate** (story must hit at
  least one sector keyword) so e.g. the diabetes run doesn't leak generic health
  news. Build from the Sector Keywords tab.

### Scorer (`src/scorer.py`)
- Reuse clustering + historical dedup, but pass `stream="sector:<key>"` and a
  **14-day** dedup window. Content-similarity score becomes audit-only (fine —
  it already doesn't gate).

### Ranker (`src/ranker.py`)
- Parameterize the bucket set: sector mode uses **event-type buckets** instead
  of the 8 priority buckets.
- No top-5 highlight section; raise `per_bucket_max`; include lower tiers
  (a $1.8M pre-seed should surface). Order groups: raises → M&A →
  clinical/regulatory → policy/legal/enforcement → partnerships → launches.
- Reuse the S/A/B/C tiering + one-liner generation as-is.

### Slack (`src/slack_client.py`)
- New `build_sector_blocks()` for the flat bullet style (see example below).
- One message per sector; optional two-line header
  (`Mental Health · Aug 1–14 · 14 items`).
- Keep URL validation + `unfurl=false`.

### Orchestrator (`src/main.py`)
- `--mode sector` (or a `sector_main.py`) + a runner that loops all 7 sectors,
  each: build plans → fetch → score(stream) → rank(event buckets) →
  post message. Per-sector idempotency via `has_sent_digest_for_date(stream=)`.

### Scheduling (`deploy/` systemd)
- New timer `OnCalendar=*-*-01,15 08:00` (Asia/Kolkata) → one sector run.

### Docs / admin
- Update `docs/EDITING.md`, `HOW_IT_WORKS.md`, `CLAUDE.md`.
- Admin-UI sector editor: **deferred to v2**.

## Target output format (from the reference example)

```
Mental Health / Behavioral Health · Aug 1–14

Raises
• Vanna Health raised $17M.
• HMNC Brain Health raised a $50M Series B to advance two depression drugs.
• Wellbees raised a $3.6M Series A.
• June Health raised a $1.8M pre-seed.

M&A
• PsychPlus acquired Koa Health.
• Merakey acquired I Am Boundless — two behavioral-health non-profits, >$1B combined revenue.

Clinical & Regulatory
• Definium Therapeutics reported positive Phase 3 results for LSD-based depression drug DT120.
• NRx received FDA expanded access for NRX-101 alongside TMS in treatment-resistant depression.

Policy, Legal & Enforcement
• Vermont banned AI from independently providing mental health services.
• DOJ enforcement action totaled $208M in behavioral-health billing fraud (incl. $31.8M TMS fraud).
• States froze behavioral-health Medicaid enrollment (Utah, Maryland, Arizona).

Partnerships & Launches
• Grow Therapy + Stanford launched a research partnership on AI clinical safety standards.
• Akron Children's launched a 24/7 mental-health support tool.
• New Mexico launched a free (AI-free) teen wellness app.
• Sensible Care launched on-demand virtual therapy "SC Now".
```

## Phases

- **Phase 0** — stream-scoped dedup (storage + scorer + ranker params). Isolated, unit-testable.
- **Phase 1** — `sectors.xlsx` + loaders + `build_sector_plans` + per-sector topicality gate. Deterministic, no API cost.
- **Phase 2** — sector ranker (event buckets) + Slack formatter. Validate via `--dry-run` on Everbright.
- **Phase 3** — `main` sector mode + all-7 runner + secondary-geo sweep.
- **Phase 4** — bi-weekly systemd timer + docs. (Admin UI → v2.)

## Open items to confirm before Phase 1
- Exact keyword lists per sector (seed from portco + competitors, then tune).
- Watchlist competitor names per portco.
- Shared sector channel id (Slack).
- Target story count per sector (default ~15).
