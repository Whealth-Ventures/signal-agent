# Rollout checklist — 2 & 3 September digest feedback

Companion to [`2026-09-02-feedback-plan.md`](2026-09-02-feedback-plan.md), which
holds the diagnosis. This is the running list of what to do, in what order, and
who does it.

Ordering is not cosmetic. Two steps are hard-blocked and marked ⚠️.

---

## Status at a glance

| Step | Owner | State |
|---|---|---|
| 0. Flip `ranker_provider` → `perplexity` | Subhanu | ✅ done 2 Sept, verified in the canonical file |
| A. Headline rewrite (PR #14) | Subhanu | ✅ merged, deployed as 1.3.0 |
| B. Geo / ordering / bucket / visibility (PR #15) | Subhanu | ✅ merged, deployed as 1.4.0 |
| C. Story-derived geo, dedupe, blocklist (PR #16) | Subhanu | ⬜ review round 2 addressed, awaiting merge |
| D. SharePoint sheet edits | Subhanu | ⬜ two safe now, one blocked on C |
| E. Dry run verification | Claude | ⬜ blocked on C + D |
| F. Geo backfill | Claude | ⬜ optional, needs explicit go-ahead |

---

## C. Ship PR #16

- [ ] Commit the staged work (the branch already carries `353c130`)
- [ ] `git push -u origin feat/subhanu`
- [ ] `gh pr create --base main --head feat/subhanu --title "[minor] Fix the 2 Sept digest feedback: geo tags, story ordering, bucket catch-all, degraded-run visibility" --body-file pr-description.md`
- [ ] `gh pr merge <n> --squash`   *(no `--delete-branch`; we keep branches)*
- [ ] `scripts/deploy-status.sh` until the Jenkins bump to **1.5.0** appears

Do not deploy by hand. Jenkins takes about 100 seconds from merge.

---

## D. SharePoint edits — `inputs/tuning.xlsx`

All three are SharePoint-only. No deploy, live on the next run.

### Safe at any time — `Settings` tab

- [ ] Row 31: `default_bucket` = `other_healthcare`
- [ ] Row 32: `blocked_domains` =
      `facebook.com; fb.com; fb.watch; instagram.com; x.com; twitter.com; linkedin.com; reddit.com; youtube.com; youtu.be`

Old code ignores unknown setting names, so these are harmless before the deploy.

### ⚠️ Only after 1.5.0 is on the box — `Priority Buckets` tab

- [ ] Row 10: `other_healthcare` | `Other healthcare news` | `(catch-all)` | *geos blank*

**Why blocked:** the code on the box before this release *rejects* a bucket with
blank `geos`, and `config` parses `tuning.xlsx` at import time on every run. Add
this row early and every digest crashes.

`sub_buckets` cannot be left empty (the loader requires a value) but the value
is inert — geo-less buckets are excluded from `_priority_sub_bucket_names`, so
even a name that collides with a real Master Keywords sub-bucket is harmless.

### Optional cleanup

- [ ] Delete `Newsletters & Publications` row 32 (`AHealthcareZ`). It has
      produced zero signals in its entire history — feed discovery never worked
      on its `/c/` YouTube channel URL — and blocking `youtube.com` makes it
      permanently inert.

### Not yet — only before switching back to Claude

- [ ] `Settings` row 29: `anthropic_max_tokens_rank` `4096` → `8192`

**Required before anyone sets `ranker_provider` back to `anthropic`.** Claude
ranking has never actually worked: 10 of 11 calls between 8 and 13 August came
back pinned at exactly 4096 completion tokens, truncating the JSON mid-object
and silently falling back. See FEEDBACK #11.

---

## E. Verification — Claude, after C and D

- [ ] Dry run on the box:
      `sudo -u signal /opt/signal-agent/repo/.venv/bin/python /opt/signal-agent/repo/src/main.py --dry-run --geo india --max-plans 0`
      Costs 2 Perplexity calls, writes no digest row, posts nothing to Slack.
      Confirms in one pass: ranker-supplied geo, duplicate collapse, the
      blocklist, the `other_healthcare` bucket and the headline rewrite.
- [ ] Check `data/logs/ranker_<date>.jsonl` for `geo_resolution`
      (`llm_supplied` should be close to `candidates` — a low number means the
      model is ignoring the new field) and for `duplicate_dropped` entries
- [ ] Check `data/logs/blocked_<date>.jsonl` for the dropped social URLs
- [ ] Next morning's 02:20 UTC run: geo mix, no duplicate stories, no social
      URLs, `Other healthcare news` populated, and **whether the post cleared
      its 02:30 deadline**

---

## F. Geo backfill — Claude, needs explicit go-ahead

- [ ] `python scripts/backfill_story_geo.py` (dry run, prints the plan)
- [ ] `python scripts/backfill_story_geo.py --apply`

**Optional now.** PR #16 makes the ranker re-derive geo every run, so a NULL
row only matters when the ranker also omits that story's geo. Worth doing so the
fallback layer is sound, but off the critical path.

This is the only prod-DB write in the whole sequence: 1,186 rows, one column,
`WHERE geo IS NULL` guarded, revert snapshot written before it touches anything.
Verified plan: 804 → India, 374 → US, 8 → Global, none left NULL.

Undo is `--revert data/logs/geo_backfill_<ts>.json`.

---

## Open decision: the digest nearly posted late

FEEDBACK #12. The 3 September ranking call took **331.8 seconds** against a
nominal 120s timeout, so it retried. The post landed at 02:30:07 against an
02:30:00 hold — **seven seconds of margin**. Nothing in these releases touches
it, and a late or missing digest is a worse failure than a slightly wrong one.

PR #16 nudges it both ways: duplicate collapse shrinks the candidate pool, but
`geo` adds a field to every story in the response.

- [ ] Pick one:
  1. **Move the timer earlier**, 02:20 → 02:05 UTC. One systemd file, buys 15
     minutes, no code. *Recommended.*
  2. **Cap the rank call's retries** so a slow vendor fails fast into the score
     fallback. Now that the fallback announces itself in Slack, a
     degraded-but-punctual digest is an honest outcome.
  3. **Shrink the rank prompt** (71 candidates on 3 Sept). Most effective, most
     work.

---

## Still open in FEEDBACK.md, not in these releases

| # | Item | Priority |
|---|---|---|
| 2 | `sa-fetch.sh` doesn't propagate deletions — breaks deploys on dependency removal | HIGH |
| 4 | SharePoint sync failures still invisible in Slack (ranker half shipped) | MEDIUM |
| 11 | `anthropic_max_tokens_rank` 4096 → 8192, blocks reverting to Claude | HIGH |
| 12 | Ranking latency / 7-second posting margin | MEDIUM |
| 13 | Cost reporting reads `$0.00` on the Perplexity path | LOW |
| 14 | Cross-day clustering misses near-duplicates (titles that differ slightly) | LOW |
| 15 | A partial ranker response is visible but not repaired | MEDIUM |
| 3 | Sector Agent prompts not exposed in the admin UI | LOW |
| 7 | Dead feedback infrastructure still provisioned in Terraform | LOW |
| 10 | SharePoint sync Monday race — deferred to the next sector-agent work | LOW |

---

## Once E confirms it

Worth telling Ashwin and Shirish that all three of their points are addressed,
and worth crediting the Facebook find to Shirish — "the sources still don't
seem to be correct" turned out to be right in a way none of us had spotted: an
AI-generated Facebook roundup had been recycling weeks-old headlines into the
digest since 22 July.
