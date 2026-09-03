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

`collapse_near_duplicates` compares the **candidate pool** by embedding,
immediately after the exact-identity collapse, using **greedy leader
selection**: strongest `relevance_score` first, each leader absorbing every
remaining story that clears the threshold against *it*.

- **Not single-linkage**, deliberately. See review round 2 below: connected
  components over a 30-day pool chains near-threshold neighbours into merges
  that share nothing.
- **Every drop is a direct near-duplicate of its keeper**, which is what makes
  the audit trail actionable.
- **No new API calls.** The vectors are already stored in `stories.embedding`;
  `storage.load_story_embeddings` reads them back (chunked at 500 to stay under
  SQLite's variable limit).
- **Day-agnostic**, which is the whole point: it compares whatever is in the
  pool, regardless of which run produced each story.
- **Never drops what it can't compare** — a story with no stored embedding is
  always kept.
- Every drop is logged as `near_duplicate_dropped` with both stories, the
  measured similarity and the threshold.

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

## Review round 2

**Blocker: single-linkage chaining.** Reproduced exactly — 6 stories each 0.86
to their neighbour collapsed to one survivor with the endpoints at cosine
**-0.89**. Replaced connected components with **greedy leader selection**:
highest `relevance_score` first, each leader absorbing only stories that clear
the threshold against *it*. So a dropped story is always a near-duplicate of the
specific story that replaced it. Same counterexample now keeps 3 of 6, each drop
directly above threshold to its keeper.

The review's supporting point checks out and the design note was wrong: the real
scores are Entrackr 0.6196, Express 0.5767, BioSpectrum 0.5306, so **Entrackr is
both the score winner and the story the other two measure against** (0.8924 and
0.8831). The transitive 0.8453 link the docstring leaned on was never needed for
its own example. MediBuddy still collapses to Entrackr.

**Both collapse passes are now guarded**, keeping all candidates on failure. The
mixed-embedding-dimension trigger is real: `embedding_model` is a `tuning.xlsx`
setting, so a SharePoint edit with no deploy mixes dimensions inside the 30-day
window, and `run_pipeline` is `try`/`finally` with no `except`.

**The `scorer` import is gone**, since greedy leader selection needs only
numpy. `import ranker` drops from **1.08s to 0.172s**, which also removes the
`ranker → scorer` cycle risk the review noted.

**Both test gaps closed.** A `rank_stories` test now seeds embeddings and
asserts the pass fires at the call site (the previous fixtures all
short-circuited at `len(have) < 2`), plus a test that a raising collapse still
ships a digest. And the collapse tests no longer write production-shaped
`duplicate_dropped` / `near_duplicate_dropped` rows into
`data/logs/ranker_<date>.jsonl` — `_log` is patched, as is `_log_blocked` in the
storage test, which had the same problem.

**Title/body mismatch:** corrected to `[minor]` in both, per the review — this
adds a capability that did not exist before.

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
