# [minor] PROD Release: Q&A bot finds stories by topic, not just exact words

**`search_stories` gets an `about` field that matches a topic by meaning, using the embeddings every story already has.** No ticket. This is the "semantic search" item from the plan's "Later" list ([docs/2026-09-25-news-archive-plan.md](docs/2026-09-25-news-archive-plan.md)).

## Why

`query` is an exact-word match (`LIKE` on title, summaries and body). A topic question misses stories that use other words: "hospital cyberattacks" doesn't find "ransomware" or "data breach".

## What changes

- **`about` in [src/qa.py](src/qa.py).**
  - The topic is embedded with `config.EMBEDDING_MODEL`, the same model as the stored story vectors (`stories.embedding`, written by the digest).
  - Every story that passes the other filters gets a cosine score. Stories under `ABOUT_MIN_SIMILARITY = 0.35` are dropped, and the rest are ordered closest first.
  - **The closest matches decide which stories come back; `sort` only orders them.** In a live test, sorting every match by date first let loose matches crowd out close ones, and "hospital cyberattacks" missed the Veradigm breach.
  - `sort` gains `relevance`, the default when `about` is set. Without `about`, nothing changes: same SQL, same order, same limit.
- **Threshold, measured on 1 October 2026** over 2,267 labelled stories:
  - On-topic stories scored 0.40 to 0.69.
  - Off-topic probes topped out at 0.28 ("football transfer news") and 0.32 ("cryptocurrency prices").
- **Coverage:** all 3,053 labelled stories on prod have an embedding, so nothing needs backfilling.
- **Cost and speed:**
  - One embeddings call per topic search, effectively free at text-embedding-3-small prices.
  - An unfiltered topic search takes about 150 ms. That's fine until roughly 100k stories (`ponytail:` note in `_about`).
- **Prompt** ([prompts/qa_system.md](prompts/qa_system.md)):
  - Topics go in `about`, exact names in `query`. When nothing is found, the bot tries `about` instead of `query`.
  - A "biggest" list uses no `min_magnitude`. In testing, the model added `min_magnitude: S` itself, got one result, and leaked "S-magnitude" into the answer.

## Tested

- `tests/test_qa.py` `AboutTest`:
  - closest first, with off-topic and no-embedding stories dropped
  - `sort` reorders the matches, and never trades a close match for a newer loose one
  - other filters still apply
  - `_dispatch` passes the embedder, and `about` without one is a tool error
- Full suite: 363 passed. The 2 `test_config` failures only happen locally, because there's no `.env`.
- Live against a local copy of the archive:
  - "Weight-loss drug pricing": the Lilly/Novo Medicare stories, matched by `about`.
  - "Hospital cyberattacks or patient data breaches": the Veradigm and Aesto Health breaches, after the "closest matches decide" fix.
  - "Latest on nurse shortages": 5 on-topic stories, newest first.
  - "Biggest AI in healthcare funding rounds in September": Angle Health $600M, Anew Labs $290M, Tandem Health $100M, then Implicity and Evvy at $40M. This was after the `min_magnitude` prompt fix.
  - "Biggest healthcare IPO stories this month": unchanged. That question uses filters only, with no `about`.

## Deploy

No new secrets, dependencies or migrations. Jenkins restarts the bot service on deploy.
