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

### 1. Close the admin → box deploy loop  ·  priority: HIGH
**Problem.** The admin UI commits input/prompt edits to GitHub, but the production
box never reads GitHub at runtime — it runs the `inputs/` + `prompts/` baked into
the last deployed **S3 artifact** (push model). The Jenkins job that builds that
artifact on push to `main` is **not firing** (last auto-built artifact predates the
Keywords/Content work). Net effect: **edits made in the admin UI do not reach the
running digest** until someone manually builds + deploys an artifact.

**Fix (recommended).** Add a GitHub Actions job on push to `main` that does what
`Jenkinsfile` does: package the tree → upload to
`s3://…/artifacts/signal-agent/<sha>.tgz` (+ `latest.tgz`) → SSM
`sa-fetch.sh <key>` + `deploy.sh`. This closes the loop without depending on
Jenkins being alive. (Alternatives: fix the Jenkins webhook; or deploy manually
after each edit — tedious.)

### 2. `sa-fetch.sh` doesn't propagate deletions  ·  priority: MEDIUM
**Problem.** `sa-fetch.sh` extracts the artifact *over* the box's repo dir and
never removes files absent from the tarball, so files deleted in a commit linger
on the box (had to `rm` orphaned modules by hand after the last deploy).

**Fix.** Reset the repo dir on deploy — e.g. extract into a fresh dir and swap, or
`rsync --delete` from a clean extract. Preserve `data/` (state) and the venv.

### 3. `infra/README.md` is out of date  ·  priority: MEDIUM
**Problem.** It states inputs are "pulled fresh from origin/main each run" — false;
the box never talks to GitHub (`deploy/run-digest.sh` confirms). It also describes
the **admin UI running on EC2** (`signal-admin.xponentiate.com`, "No Vercel", a
"Decommission Vercel" step) — but the admin actually runs on **Vercel**
(`signal-agent-admin`, 2070Health team).

**Fix.** Decide the real admin host (Vercel is what's live today) and reconcile the
doc: correct the input-propagation claim and the EC2-vs-Vercel narrative.

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
