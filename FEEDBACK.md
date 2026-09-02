# FEEDBACK — open items to fix next

This file is the running backlog of known issues and recommended improvements for
Signal Agent. It is the **first place to look** before starting new work.

**The rule (see `CLAUDE.md`):**
1. Start by reading this file and addressing its open items.
2. When an item is fixed, **remove it from here** and **record it in `RELEASE_NOTES.md`**
   (what changed, in plain language), then push.

Keep items concrete: what's wrong, why it matters, and the suggested fix.

---

## What I'd recommend next

### 1. ~~Close the admin → box deploy loop~~ · RESOLVED, was wrong
**This item was stale and actively misleading — deleted rather than carried.**

It claimed the Jenkins job "is not firing" and that admin edits never reach the
box without a manual deploy. Both halves are now false:

- **Jenkins fires reliably.** Verified 2026-08-08: PR #10 merged 07:10:11 →
  `chore: bump version to 1.2.0` at 07:12:18 (+ tag `v1.2.0`); PR #11 merged
  08:34:59 → `1.2.1` at 08:37:14. Build-to-deploy is ~2-3 minutes. The webhook
  (`jenkins.xponentiate.com/github-webhook/`) returns 200 on every push. The
  WH-313 release pipeline landed after this item was written.
- **`inputs/` doesn't need a deploy at all** — it's pulled from SharePoint at the
  top of every run (`src/sharepoint_sync.py`).

So only `prompts/` and code need a deploy, and Jenkins does that on merge.

**The trap this left behind:** believing Jenkins was dead led to hand-built
artifacts that repoint `latest.tgz` at un-bumped commits, breaking
rollback-by-version. If the box looks stale after a merge, wait for the
`Jenkins CI` bump commit before concluding anything — see the shipping rule in
`CLAUDE.md`.

### 2. `sa-fetch.sh` doesn't propagate deletions  ·  priority: HIGH *(was MEDIUM)*
**Problem.** `sa-fetch.sh` extracts the artifact *over* the box's repo dir and
never removes files absent from the tarball, so files deleted in a commit linger
on the box.

**Raised to HIGH — this now breaks deploys, not just tidiness.** The Aug 2026
SharePoint deploy failed outright: the box still carried
`admin/app/api/{feedback,slack,suggestions}` and `admin/lib/feedback-store.ts`
from the long-removed Suggestions feature. They had compiled silently for months;
the moment that commit dropped `exceljs` and `@aws-sdk/client-s3` from
`package.json`, `next build` failed on the orphans and `deploy.sh` aborted. Any
future dependency removal will do the same. (Those particular orphans are gone
now — the deploy reset the source dirs by hand.)

Worth noting the orphans were *live*: the removed Suggestions endpoints were
still reachable on the running admin the whole time.

**Fix.** Reset the code dirs on deploy. The manual workaround that unblocked the
Aug deploy was `rm -rf src tests scripts deploy docs infra prompts admin/app
admin/lib admin/.next` before extracting — preserving `data/` (SQLite + Chroma),
`.venv`, `admin/node_modules`, and `inputs/` (the SharePoint cache; keeping it
means a sync failure still has last-known-good). Fold that into `sa-fetch.sh` —
note it lives in `infra/user_data.sh.tftpl`, so the running box's copy at
`/usr/local/bin/sa-fetch.sh` needs updating too, not just the template.

### 3. Expose the Sector Agent prompts in the admin UI  ·  priority: LOW
**Problem.** The admin **Prompts** page edits only `ranker_system.md` and
`magnitude_rubric.md`. The weekly Sector Agent's two prompts
(`prompts/sector_system.md`, `prompts/sector_impact_rubric.md`) have never been
exposed, so tuning them means a git edit. Now that Prompts is the *only* admin
page, the gap is more visible.

**Fix.** Add the two files to `app/api/prompts/route.ts` and the page's textarea
list — they're plain markdown, same as the existing two.

### 4. A stale-inputs run still looks exactly like a healthy one  ·  priority: MEDIUM  *(ranker half shipped 2026-09-02)*
**The ranker half is fixed** — a fallback run now carries a warning line in the
Slack post (see `RELEASE_NOTES.md`). What remains is the SharePoint sync: it
fails soft by design, so a run on stale inputs looks completely healthy.
`WARN: sharepoint sync failed` goes to the journal and nothing surfaces it.

**Fix.** The sync runs as its own process before `main.py`, so it can't reach
the Block Kit builder. Cheapest route: have `sharepoint_sync.py` touch a
sentinel file (e.g. `inputs/.sync_failed`) on failure and delete it on success,
then have `main.py` read that and add a line to the post the same way
`used_fallback` now does.

Still open from the original item: (b) a non-zero exit from `run-digest.sh` on a
fully-failed sweep so the systemd unit records a failure, and (c) an alert on the
401 rate in the perplexity log.

### 5. Every story tagged [GLOBAL] in both daily digests  ·  priority: HIGH  ·  target: 1 September 2026 release
**Problem.** Since at least 29–30 Aug 2026, every story in both the India and US
digests renders as `[GLOBAL]`, and digests are short (~7 stories vs the 15–25
target), heavy on RSS sources (STAT+).

**Diagnosis so far (code trace, box not yet checked).** Geo is not decided
per-story: it's inherited from the fetch plan (`main.py` stamps
`raw["geo"] = plan.geography` on each Perplexity signal), majority-voted per
cluster (`scorer.pick_geo`), and RSS signals carry **no geo** → `None` →
rendered `[GLOBAL]` (`slack_client._GEO_TAG`) and routed to BOTH channels
(`ranker.filter_by_geo` keeps `None`). So an all-GLOBAL digest means no
Perplexity-derived signals survived — RSS-only pipeline. Almost certainly a
recurrence of item #4's failure mode (Perplexity `insufficient_quota` 401s,
1–8 Aug) — which is also why nobody was alerted.

**CONFIRMED on the box, 31 Aug 2026 (read-only via SSM, i-0803d6edfc54c1bb0):**
- Every Perplexity call returns **401 `insufficient_quota`** ("exceeded your
  current quota… check plan and billing") — India, US, and sector runs alike.
- **Last successful Perplexity signal: 21 Aug 2026 11:52 UTC.** Zero since
  22 Aug — 10 days of silent RSS-only digests. Recent stories are ~85–100%
  `geo=NULL` → rendered `[GLOBAL]`, sent to both channels.
- Exact repeat of item #4's outage (31 Jul–7 Aug gap in the same table; quota
  topped up 8 Aug, burned through again in 14 days at 100–220 signals/day).
- **Bonus finding:** the SharePoint sync failed on the 31 Aug run too —
  `FileNotFoundError` renaming
  `inputs/content/articles_blog/…nivaan.md.tmp` → `.md` — so inputs are also
  stale. Separate bug in `sharepoint_sync.py`'s tmp-rename step; investigate
  alongside.

**Fix.** Top up / fix billing on the Perplexity account (the account is
burning its quota in ~2 weeks — consider a bigger plan or auto-recharge), then
ship item #4's visibility fix in the same release so the next silent
degradation can't run for 10 days unnoticed. Look at the sharepoint tmp-rename
error too.

**RESOLVED 1 Sept 2026 (Perplexity half):** payment restored the account
(key unchanged — Perplexity reports a suspended account as `invalid_api_key`).
Verified by a `[TEST]` India run: 82 perplexity signals, correct [IND]/[GLOBAL]
tags, posted to the channel. **BUT the same test exposed item #9 — the
Anthropic org is disabled too**, so the run shipped with the score-based
fallback (no tiers, junk stories slip in). Geo fixed; ranking still degraded.

### 9. Anthropic organization disabled — ranker (and 1 Sept headline rewrite) dead  ·  priority: HIGH
**Problem.** Every Claude call since at least 1 Sept returns
`organization_on_hold`: "This organization has been disabled. An organization
admin can appeal at https://console.anthropic.com/appeal"
(`data/logs/anthropic_2026-09-01.jsonl`, both the 02:21 UTC scheduled run and
the 06:04 UTC test run). The ranker silently drops to the score-based fallback
— digest ships untier'd, and the new headline-rewrite step will no-op the same
way. `ranker_provider=anthropic`, model `claude-sonnet-4-5`, key present — the
account itself is the blocker, only an org admin can resolve it (billing or
appeal at console.anthropic.com).

### 11. Claude ranking is capped at half the tokens it needs  ·  priority: HIGH  ·  BLOCKS reverting to `ranker_provider=anthropic`
**Problem.** `anthropic_max_tokens_rank` is **4096**; the ranking response needs
about **7000**. So the JSON is truncated mid-object, `_extract_json` returns
`None`, and `parse_ranked` reports `parse_fallback=True` — which surfaces as
`used_fallback=True` with an **empty `call_error`**, indistinguishable in the
console from a vendor outage.

**Evidence (box call logs, read 2026-09-02).** Anthropic rank calls 8–13 Aug:
`completion_tokens=4096` on **10 of 11 calls**, i.e. pinned at the cap. Perplexity
`sonar-reasoning-pro` on the same prompt: 5,693–7,651 completion tokens across
22 runs, **0 fallbacks**. So Claude ranking has never actually worked — it
silently degraded from the day PR #11 switched to it (8 Aug), and from ~14 Aug
the org/billing failures (#9) masked it entirely.

**Fix.** Set `anthropic_max_tokens_rank` to **8192** in `tuning.xlsx` Settings —
spreadsheet only, no deploy. **Do this before anyone flips `ranker_provider`
back to `anthropic`**, or the 2 Sept digest comes straight back. Verify by
checking the next Claude rank call logs `completion_tokens` below the cap.

Worth doing alongside: have the ranker log a distinct warning when
`completion_tokens >= max_tokens`, so truncation can never again look like a
vendor error.

### 10. SharePoint sync Monday race (`.tmp` rename FileNotFoundError)  ·  priority: LOW  ·  DEFERRED to next sector-agent work
**Root cause (confirmed 1 Sept 2026).** The India daily and sector timers both
fire Mon 02:20 UTC; both run `sharepoint_sync.py` into the same `inputs/`, and
both write the same `.tmp` path per file — the loser's `os.replace` hits
`FileNotFoundError` (failed on every Monday in Aug 2026; victim alternates).
Impact is mild: the loser falls back to last-known-good inputs and the winner
completes the mirror, so inputs stay fresh. Interleaved writes to the same
`.tmp` are a latent corruption risk.

**Scoped fix (not written):** blocking `fcntl.flock` on `inputs/.sync.lock`
around `sync()` — ~6 lines, stdlib, `[patch]`. Deliberately deferred: pick up
alongside any sector-agent work. Only the India-daily/sector pair collides;
the US run (11:50 UTC) never does.

### 7. Remove the dead feedback infrastructure  ·  priority: LOW
**Problem.** The Slack-reaction feedback feature was removed from the code, but its
**Terraform still provisions live AWS resources**: the S3 "feedback" bucket and its
wiring (`infra/s3.tf`, `ssm.tf`, `locals.tf`, `ec2.tf`, `Jenkinsfile`).

**Fix.** Remove those resources deliberately via Terraform **if** the bucket is
truly unused. Note: the same bucket is currently also used for nightly state
backups (`run-digest.sh`) — keep a bucket for that, or repoint the backup first.

### 8. `RELEASE_NOTES.md` history mentions the removed Suggestions feature  ·  priority: LOW
The v1.2.0 notes still advertise the Suggestions/feedback loop. Left as historical
record; trim or annotate if it confuses readers.
