# [minor] Collapse near-duplicate stories from different publications

Found by the 1.5.0 verification dry run, not by a test. Closes FEEDBACK #14.

## The problem

`collapse_duplicates` (1.5.0) catches one article served under several URLs —
BioSpectrum's `/news/16/28410/<slug>` vs `/news/101/28410/<slug>`. It matches on
normalised title, normalised URL, or host + slug.

None of those can catch **two publications reporting the same event**, because
all three differ legitimately.

Observed in the 3 Sept India dry run. MediBuddy's appointment of Shalabh
Shrivastava produced three separate stories:

| Publication | Fetched | Story |
|---|---|---|
| Entrackr | 2 Sept 11:52 | `364f723d` |
| BioSpectrum India | 3 Sept 02:22 | `0048e23b` |
| Express Healthcare | 3 Sept 08:45 | `35328298` |

**Two of them took two of the five headline slots** in the same digest.

## Why the existing clustering missed it

`scorer.cluster_signals` already does order-independent connected-components
clustering at `cluster_similarity_threshold` (0.85) — but only over the signals
of a **single scoring run**, and it does not re-cluster against stories from
previous days. Each of these three arrived in a different run.

Measured pairwise cosines on the box:

```
Entrackr    ↔ BioSpectrum   0.8924   > 0.85
Entrackr    ↔ Express       0.8831   > 0.85
BioSpectrum ↔ Express       0.8453   < 0.85   (caught transitively via the chain)
```

So the data was already sufficient to merge all three. Nothing was looking.

## The fix

`collapse_near_duplicates` runs the **same** `cluster_signals` pass over the
**candidate pool** rather than a single run's signals, immediately after the
exact-identity collapse. Highest `relevance_score` in each cluster survives.

- **No new API calls.** The vectors are already stored in `stories.embedding`;
  `storage.load_story_embeddings` reads them back (chunked at 500 to stay under
  SQLite's variable limit).
- **Day-agnostic**, which is the whole point: it compares whatever is in the
  pool, regardless of which run produced each story.
- **Never drops what it can't compare** — a story with no stored embedding is
  always kept.
- **Transitive**, so the sub-threshold pair above still merges via the chain.
- Every drop is logged as `near_duplicate_dropped` with both stories and the
  threshold, matching the audit the exact-identity pass already emits.

`Story` deliberately still doesn't carry its embedding; it would ride through
every layer for no reason.

## Testing

301 tests pass.

- `collapse_near_duplicates`: the real MediBuddy geometry (a fixture built to
  reproduce the 0.8924 / 0.8831 / 0.8453 straddle, asserted before the
  collapse), orthogonal stories untouched, a story without an embedding never
  dropped, no-embeddings-at-all is a no-op, and input order preserved.
- `storage.load_story_embeddings`: only ids that have one, empty and unknown
  ids, and 1,200 ids to exercise the chunking.

Two `test_config` cases fail locally for want of a `.env`
(`OPENAI_API_KEY`, `SLACK_WEBHOOK_URL`). Pre-existing and environmental; CI
supplies them.

## Deploy notes

No env, schema, dependency, infra or `tuning.xlsx` change. Threshold defaults to
the existing `cluster_similarity_threshold`, so it is already tunable from
SharePoint without a deploy.

The one behaviour to watch: this is a **quality-for-quantity** trade. A digest
that previously carried two tellings of one story will now carry one, and the
freed slot goes to the next-best story. If it ever collapses two genuinely
distinct stories, `near_duplicate_dropped` in `data/logs/ranker_<date>.jsonl`
names both and the threshold, and raising `cluster_similarity_threshold` is a
sheet edit.
