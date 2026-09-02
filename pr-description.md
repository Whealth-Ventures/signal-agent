# [minor] Headline rewrite: digest one-liners written from the article body

## What

New post-selection step that fixes vague digest one-liners (FEEDBACK #6). The
ranker writes one-liners from title + ≤500-char snippet and never reads the
article, so a vague source headline produced an equally vague digest line.

After ranking + geo filter, `src/headline_rewriter.py` now:

1. Fetches only the ~15–25 winning articles (concurrent, 12s timeout each).
2. Extracts a body excerpt: `<p>`-tag text after stripping
   script/style/nav/header/footer, 2,000 chars, strip-all-tags fallback.
3. Makes ONE further LLM call — same vendor selection as the ranker
   (`ranker._build_ranker_client`: Claude when configured + key set, else
   Perplexity `sonar-reasoning-pro`) — that rewrites each one-liner from the
   body: 5–10 words, ≤90 chars, newsroom sentence case, op-eds attributed,
   no invented facts. Prompt: `prompts/headline_system.md`.

## Behaviour guarantees

- **Fail-soft at every layer**: fetch failure, thin excerpt, LLM failure,
  malformed JSON, overlong or missing headline → that story keeps the
  ranker's one-liner; a whole-step exception returns the ranking unchanged.
  The digest can never be blocked by this step.
- **Opt-out**: `--skip-headline-rewrite` CLI flag.
- **Audit**: every rewrite logged to `data/logs/headlines_<date>.jsonl`.
- **Cost**: +1 ranker-class LLM call per geo run.

## Files

- `src/headline_rewriter.py` — new module.
- `prompts/headline_system.md` — new prompt.
- `src/main.py` — step 5c wiring after geo filter + `--skip-headline-rewrite`.
- `src/config.py` — `HEADLINE_SYSTEM_PROMPT` loader.
- `tests/test_headline_rewriter.py` — 8 offline tests (extraction, happy
  path, no-excerpt skip, LLM-failure fail-soft, overlong/missing keys,
  empty ranking).
- `FEEDBACK.md` / `RELEASE_NOTES.md` — item #6 moved to release notes; #5
  updated with the confirmed Perplexity RCA + resolution; new #9 (Anthropic
  org on hold) and #10 (SharePoint Monday sync race, deferred) recorded.
- `CLAUDE.md` / `HOW_IT_WORKS.md` — module list + pipeline docs updated.

## Testing

- 8/8 offline unit tests pass (`tests/test_headline_rewriter.py`).
- Live end-to-end on Perplexity `sonar-reasoning-pro` (1 Sept 2026, local):
  3 real articles fetched and rewritten — vague op-ed became a concrete
  thesis; rewrites picked up facts (₹49 crore, MFN Medicaid) present only in
  the article bodies; one article had a mismatched URL slug and was still
  read correctly.
- Live test on the Claude path is blocked by the Anthropic org hold
  (FEEDBACK #9); on that path the step currently fail-softs to the ranker's
  one-liners, which is the designed degradation.

## Deploy notes

- No env, schema, or infra changes. No new dependencies.
- With `ranker_provider=anthropic` and the org still on hold, this step
  no-ops safely. To activate before the org is restored, flip
  `ranker_provider` to `perplexity` in `tuning.xlsx` (Settings) on
  SharePoint — no deploy needed for that flip.
