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

### 1. Close the deploy loop for `prompts/` and code  ·  priority: MEDIUM
**Was HIGH; now narrowed.** `inputs/` no longer has this problem — it is pulled
from SharePoint at the top of every run (`src/sharepoint_sync.py`), so workbook
and content edits reach the digest with no deploy.

**What's left.** `prompts/` and the code itself still ship only in the S3
artifact (push model), and the Jenkins job that builds it on push to `main` is
**not firing**. So a prompt edit in the admin UI still doesn't reach the box until
someone manually builds + deploys.

**Fix (recommended).** Add a GitHub Actions job on push to `main` that does what
`Jenkinsfile` does: package the tree → upload to
`s3://…/artifacts/signal-agent/<sha>.tgz` (+ `latest.tgz`) → SSM
`sa-fetch.sh <key>` + `deploy.sh`. (Alternatives: fix the Jenkins webhook; or
deploy manually after each prompt edit — now rare enough to be tolerable.)

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

### 4. A degraded digest looks exactly like a healthy one  ·  priority: HIGH
**Problem.** Perplexity's account hit `insufficient_quota` and returned 401 on
**every** call for at least 8 days (2026-08-01 → 08-08, 28/28 calls failing daily).
Nobody noticed, because the digest kept posting on schedule — built from RSS
alone, with the ranker degraded to the score-based fallback (no magnitude tiers,
no LLM one-liners, no LLM bucket assignment). The Slack post carried no
indication anything was wrong.

**Fix.** Make the failure visible. Options, cheapest first: (a) a line in the
Slack post when `perplexity_calls == 0` or `used_fallback` is true; (b) a
non-zero exit from `run-digest.sh` on a fully-failed sweep, so the systemd unit
records a failure; (c) alert on the 401 rate in the perplexity log.

The same blind spot applies to the SharePoint sync — it fails soft by design, so
a stale-inputs run also looks healthy. `WARN: sharepoint sync failed` is in the
log but nothing surfaces it.

### 5. Remove the dead feedback infrastructure  ·  priority: LOW
**Problem.** The Slack-reaction feedback feature was removed from the code, but its
**Terraform still provisions live AWS resources**: the S3 "feedback" bucket and its
wiring (`infra/s3.tf`, `ssm.tf`, `locals.tf`, `ec2.tf`, `Jenkinsfile`).

**Fix.** Remove those resources deliberately via Terraform **if** the bucket is
truly unused. Note: the same bucket is currently also used for nightly state
backups (`run-digest.sh`) — keep a bucket for that, or repoint the backup first.

### 6. `RELEASE_NOTES.md` history mentions the removed Suggestions feature  ·  priority: LOW
The v1.2.0 notes still advertise the Suggestions/feedback loop. Left as historical
record; trim or annotate if it confuses readers.
