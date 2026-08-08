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

### 2. `sa-fetch.sh` doesn't propagate deletions  ·  priority: MEDIUM
**Problem.** `sa-fetch.sh` extracts the artifact *over* the box's repo dir and
never removes files absent from the tarball, so files deleted in a commit linger
on the box (had to `rm` orphaned modules by hand after the last deploy).

**Fix.** Reset the repo dir on deploy — e.g. extract into a fresh dir and swap, or
`rsync --delete` from a clean extract. Preserve `data/` (state) and the venv.

### 3. Expose the Sector Agent prompts in the admin UI  ·  priority: LOW
**Problem.** The admin **Prompts** page edits only `ranker_system.md` and
`magnitude_rubric.md`. The weekly Sector Agent's two prompts
(`prompts/sector_system.md`, `prompts/sector_impact_rubric.md`) have never been
exposed, so tuning them means a git edit. Now that Prompts is the *only* admin
page, the gap is more visible.

**Fix.** Add the two files to `app/api/prompts/route.ts` and the page's textarea
list — they're plain markdown, same as the existing two.

### 4. Remove the dead feedback infrastructure  ·  priority: LOW
**Problem.** The Slack-reaction feedback feature was removed from the code, but its
**Terraform still provisions live AWS resources**: the S3 "feedback" bucket and its
wiring (`infra/s3.tf`, `ssm.tf`, `locals.tf`, `ec2.tf`, `Jenkinsfile`).

**Fix.** Remove those resources deliberately via Terraform **if** the bucket is
truly unused. Note: the same bucket is currently also used for nightly state
backups (`run-digest.sh`) — keep a bucket for that, or repoint the backup first.

### 5. `RELEASE_NOTES.md` history mentions the removed Suggestions feature  ·  priority: LOW
The v1.2.0 notes still advertise the Suggestions/feedback loop. Left as historical
record; trim or annotate if it confuses readers.
