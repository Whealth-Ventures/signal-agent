# Sector Digest — Enhanced Algorithm (v2)

**Status:** Design. Extends the shipped v1 (see [SECTOR_DIGEST_PLAN.md](SECTOR_DIGEST_PLAN.md)).

v1 ships today: per-sector keyword facets + sources sweep + watchlist naming →
Perplexity/RSS fetch → embedding dedup (stream-scoped) → Claude tiering into
event buckets with roll-ups + a prose summary → combined PDF.

v2 adds five upgrades the team agreed on:
1. **Structured event extraction** — facts, not text blobs.
2. **Geo-aware portco lens** — position each item relative to the portco, but
   only compare within the portco's own geography.
3. **Longitudinal store** — a durable, queryable events DB for momentum + future
   tooling.
4. **Retrieval model** — per sector: keywords + sources (newsletters/reports/
   established) + geo-tagged watchlist, plus direct feeds.
5. **Feedback loop** — specific user feedback that improves selection + voice
   over time.

---

## Inputs (`inputs/sectors.xlsx`)

| Tab | Columns | Change from v1 |
|---|---|---|
| Sectors | key, display, portco, primary_geo, secondary_geo, secondary_max_stories, target_story_count, notes | — |
| Sector Keywords | sector, keyword, geo | — |
| Sector Watchlist | sector, company, **geo** | **+ geo** (drives the geo-aware lens) |
| Sector Sources | sector, type (author/newsletter/report/**established**), name, url, rss_url | `established` type = high-trust anchor |

**Why geo on the watchlist:** the portco lens must be geographically honest. A
company is a *direct competitor* to the portco only when it operates in the
portco's `primary_geo`. Same-geo watchlist = competitor set; other-geo watchlist
= reference/benchmark set (e.g. US oncology names are context for Everhope in
India, never "competitors").

---

## Durable state (SQLite — the "rich DB")

Append-only, queryable later for dashboards or anything built on top.

- `stories` (existing) — deduped clusters.
- **`events` (new)** — one row per normalized development:
  `id, story_id, sector, geo, event_type, company, counterparty, amount_usd,
   round, investors_json, regulator, drug_product, trial_phase, event_date,
   source_url, source_trust, confidence, novelty, first_seen_run, digest_id`.
- **`feedback` (new)** — `item_id, sector, event_id, verdict, note, ts`
  where verdict ∈ {keep, drop, not_relevant, wrong_tier, wrong_bucket, great}.
- `digests` / `digest_stories` (existing, stream-scoped).

`events` is the substrate for momentum (below) and for future querying — keep it
append-only and stamped with the run so history is never overwritten.

---

## Pseudo-algorithm (per sector, bi-weekly)

```
# 1. PLAN  (deterministic; ~5–6 Perplexity plans/sector)
plans = keyword_facets(raises, clinical, policy, product) @ primary_geo
      + sources_sweep(name authors + newsletters + reports)
      + watchlist_sweep(name companies)                 # NEW: company-anchored
      + secondary_geo_sweep("biggest only")             # peds / onc
recency = last 14 days

# 2. FETCH  (concurrent, budget scope = 'sector')
signals = perplexity(plans)
        + rss(sources.rss_url)                          # NEW: direct feed pulls
        [+ primary_source_connectors(clinicaltrials, fda, funding_db)]  # optional
each signal tagged {sector, geo, source_trust}          # trust from source tier

# 3. EXTRACT + NORMALISE  (NEW — structured events)
for signal in signals:
    e = extract_event(signal)   # company, event_type, amount, round, investors,
                                # counterparty, regulator, drug, phase, geo, date
    e.canonical_key = (norm(company), event_type, amount_bucket, round)

# 4. DEDUPE  (structured first, then embedding)
cluster by canonical_key       # same company+amount+round == same event
then merge remaining near-dups by embedding cosine
drop events already sent in this sector's 14-day history (stream-scoped)
confidence = count(independent sources) for the cluster

# 5. GATE
keep if healthcare_gate AND sector_gate(keywords ∪ watchlist ∪ sources)
     AND within_last_14_days

# 6. JUDGE  (one Claude call, feedback-primed)
prompt = rubric
       + house_voice_exemplars(from feedback: accepted one-liners)   # NEW
       + negative_examples(from feedback: dropped/irrelevant)        # NEW
       + candidate events
returns per event:
    tier (S/A/B/C), event_bucket, one_liner (house voice, British spelling),
    novelty, portco_stance                                          # NEW
  + sector prose summary
  + roll-ups: merge related events into one bullet

  # PORTCO LENS  (GEO-AWARE)  — NEW
  stance(e) =
    'portco'            if e.company == sector.portco
    'direct_competitor' if e.geo == portco.primary_geo
                           and e.company in watchlist(geo == primary_geo)
    'reference'         if e.geo != portco.primary_geo   # benchmark, not rival
    'tailwind'|'headwind'|'neutral' otherwise
  # Implication lines in the summary only compare WITHIN portco.primary_geo.
  # Cross-geo items are framed as benchmarks ("US benchmark: ..."), never as
  # threats to the portco.

# 7. ORGANISE  (deterministic)
drop tier C
cap secondary-geo bullets to secondary_max_stories
enforce sector target_story_count (keep strongest by tier, then novelty, recency)
group by event_bucket; order within group by (tier, novelty, recency); per-group cap
momentum = compute_from_events_db(sector)   # NEW: deal count, $ raised vs prior
                                             # fortnight/quarter, streaks, new vs repeat

# 8. RENDER  (combined PDF, one page/sector)
header  = KPI line  ($ raised · #deals · #M&A · biggest)     # NEW, from events
        + momentum line                                       # NEW
summary = prose lead
body    = grouped bullets; each: [GEO] one_liner  <stance tag>  (link)
each rendered item gets a stable item_id                      # for feedback
[+ cross-sector themes page synthesised across all 7 summaries]  # optional

# 9. PERSIST + emit feedback template
append events (append-only), record digest, mark sent (stream-scoped)
write a feedback sheet: item_id ↔ one_liner ↔ (blank verdict column)
```

---

## Feedback loop (between runs) — how judging improves over time

Delivery is a PDF, so feedback is collected out-of-band and ingested next run:

- The team marks items in the emitted **feedback sheet** (or via Slack reactions
  if also posted there): keep / drop / not_relevant / wrong_tier / wrong_bucket /
  great. Stored in the `feedback` table, keyed by `item_id → event`.
- Next run uses it:
  - **Voice:** accepted one-liners become few-shot **house-voice exemplars**;
    `great` items are weighted highest.
  - **Selection:** `not_relevant` items train a negative gate (patterns /
    companies to suppress); `wrong_bucket`/`wrong_tier` correct the rubric priors.
  - **Sources:** each source gets a rolling **hit-rate** (how often its items are
    kept vs dropped) → updates `source_trust`, which feeds confidence + ordering.
  - **Thresholds:** per-sector inclusion target auto-tunes toward what the team
    actually keeps.

**Bonus quality signal:** for mental health, diff our items against Steve Duke's
*Hemingway Report* issue for the same fortnight — his coverage is a free ground
truth for recall. Gaps there tune keywords/sources for *all* sectors.

---

## What's already built (v1) vs new (v2)

| Piece | v1 (shipped) | v2 (new) |
|---|---|---|
| Keyword facets, sources sweep, watchlist naming | ✅ | company-anchored watchlist *sweep* |
| Gates, tiering, roll-ups, prose summary, stream dedup, PDF | ✅ | — |
| Watchlist geo | — | ✅ geo column |
| RSS pulls of source feeds | stubbed | ✅ wire it |
| Structured `events` table + extraction | — | ✅ |
| Geo-aware portco stance | — | ✅ |
| Novelty + corroboration confidence | — | ✅ |
| Momentum + KPI header (from events) | — | ✅ |
| Feedback table + learning loop | — | ✅ |
| Primary-source connectors, cross-sector themes, Steve benchmark | — | optional |

## Suggested build order

1. **Watchlist geo + geo-aware stance** (small; immediate product value).
2. **Structured `events` table + extraction** (foundation for 3–5).
3. **Momentum + KPI header** (rides on #2; the compounding "wow").
4. **Feedback table + ingestion + house-voice exemplars** (quality over time).
5. **RSS source pulls**, then optional connectors / themes / benchmark.
