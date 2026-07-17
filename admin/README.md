# Signal Agent — Admin UI

A small Next.js app for tuning the [`signal-agent`](https://github.com/Whealth-Ventures/signal-agent) daily healthcare digest. Magic-link auth, no DB, auto-commits to the agent repo on save.

## What it edits

- **Sources**: `inputs/voices.xlsx` in the agent repo — the publications, top voices, org pages, and PE/VC firms the agent watches. Add a publication (e.g. ET, VCCircle) with its URL and the fetcher auto-discovers its RSS feed. Five tabs matching the xlsx sheets; parse/serialize preserve the exact column layout the agent's positional loaders (`src/query_planner.py`) depend on, so edits are round-trip safe (`node lib/voices.check.mjs`).
- **Tuning**: `inputs/tuning.xlsx` in the agent repo — every numeric knob, regex booster, priority bucket, and source tier. Edited as form fields (4 tabs matching the 4 xlsx sheets).
- **Prompts**: `prompts/ranker_system.md` and `prompts/magnitude_rubric.md` — edited as plain textareas.

Each save creates a commit on the agent's `main` branch. The next scheduled cron run (10:00 IST) picks up the change.

## Deploy checklist

### 1. Create a GitHub repo for this admin UI

```bash
cd signal-agent-admin
git init
git add .
git commit -m "Initial admin UI"
# Create empty repo at github.com/<you>/signal-agent-admin, then:
git remote add origin git@github.com:<you>/signal-agent-admin.git
git push -u origin main
```

### 2. Create a fine-grained GitHub Personal Access Token

The admin UI needs to read and write files in the `signal-agent` repo.

- Go to GitHub → Settings → Developer settings → Personal access tokens → **Fine-grained tokens** → "Generate new token"
- **Repository access**: Only select repositories → pick `signal-agent`
- **Permissions** → Repository permissions:
  - **Contents**: Read and write
  - **Metadata**: Read-only (auto-selected)
- Generate, copy the `github_pat_...` token. You'll paste it into Vercel env vars in step 5.

### 3. Choose the shared login

There's a single shared username + password — anyone who knows it can sign in. You
set both values in Vercel env vars (next steps); nothing is stored in the repo.
Pick a username and a strong password now.

### 4. Import to Vercel

- Go to [vercel.com](https://vercel.com) → Add New → Project → Import the `signal-agent-admin` repo.
- Framework preset: **Next.js** (auto-detected).
- Don't deploy yet — set env vars first (next step).

### 5. Set env vars in Vercel

In the project settings → Environment Variables, add:

| Name | Value |
|---|---|
| `GITHUB_TOKEN` | the `github_pat_...` from step 2 |
| `GITHUB_OWNER` | `Whealth-Ventures` (or whoever owns the agent repo) |
| `GITHUB_REPO` | `signal-agent` |
| `GITHUB_BRANCH` | `main` |
| `AUTH_SECRET` | a random 32+ char string (e.g. `openssl rand -hex 32`) |
| `ADMIN_USER` | the shared login username, e.g. `admin` |
| `ADMIN_PWD` | a strong shared password |

Set them for all environments (Production / Preview / Development). `ADMIN_USER`
and `ADMIN_PWD` are the only credentials — keep them out of the repo and share
them only with people who should have access.

### 6. Deploy

- Trigger a deploy in Vercel (will happen automatically after env vars are set on next push, or click "Redeploy").
- Once green, visit the deployment URL → enter the shared username + password → you're in.

To rotate access (e.g. someone leaves), change `ADMIN_PWD` in Vercel and redeploy —
everyone signs in again with the new password. No per-person allowlist to maintain.

## Local development

```bash
cp .env.example .env.local
# Fill in real values (same GITHUB_TOKEN; set ADMIN_USER / ADMIN_PWD to anything for local use)
npm install
npm run dev
```

Visit `http://localhost:3000` and sign in with the `ADMIN_USER` / `ADMIN_PWD` from `.env.local`.

## How it works

- **Auth**: a single shared login. `app/api/auth/login` checks the submitted username + password against the `ADMIN_USER` / `ADMIN_PWD` env vars (constant-time compare) and, on success, `lib/auth.ts` sets a 24-hour signed JWT session cookie. No DB, no email, no per-person allowlist.
- **GitHub I/O**: `lib/github.ts` reads/writes files via Octokit. SHA round-trip prevents concurrent-edit overwrites — if the file changed since you loaded it, the write fails.
- **XLSX**: `lib/xlsx.ts` parses 4 sheets into JSON on read, serializes JSON back to xlsx on save. Schema mirrors `src/tunables.py` in the agent — keep them in sync.
- **Validation**: server-side. Invalid regex / invalid geo / empty sub-buckets / empty prompts are rejected before the commit.
- **Audit**: each save is its own commit; the shared login name is recorded in the commit author name and message. `git log inputs/tuning.xlsx` and `git log prompts/` show the history. (The committer email is always the Vercel project owner so Hobby-plan deploys aren't blocked.)

## Auto-deploy on save

The box runs the inputs/prompts from the **last deploy** and never pulls from
GitHub (push model). So a commit alone doesn't go live. When these env vars are
set (in the admin secret → materialized into `.env.production` by `deploy.sh`),
every successful save also pings Jenkins to run the deploy pipeline, so the
change is live in a few minutes:

| Name | Value |
|---|---|
| `DEPLOY_TRIGGER_URL` | Jenkins build URL, e.g. `https://<jenkins>/job/signal-agent/job/main/build` (multibranch) or a `buildByToken` URL. **Unset ⇒ auto-deploy off** — saves still commit; you deploy manually. |
| `DEPLOY_TRIGGER_TOKEN` | optional — appended as `?token=…` (Jenkins "Trigger builds remotely" token, or buildByToken). |
| `DEPLOY_TRIGGER_AUTH` | optional — `user:apiToken` for Basic auth (Jenkins API-token auth is CSRF-crumb exempt). |

Jenkins side: either enable **Trigger builds remotely** on the job (set the same
token in `DEPLOY_TRIGGER_TOKEN`), or create a Jenkins **API token** for a user
with Build permission and put `user:token` in `DEPLOY_TRIGGER_AUTH`. The trigger
is fire-and-forget: if it fails, the save still succeeds (the commit is durable)
and the UI says so — you can deploy manually.

`node lib/deploy.check.mjs` checks the URL/token/auth assembly.

## Adding new tunables

When you add a new knob to `inputs/tuning.xlsx` (Settings sheet) in the agent repo:

1. Add a row to the xlsx with name/value/description.
2. Wire it up in `src/tunables.py` and `src/config.py` in the agent.
3. The admin UI picks it up automatically — no changes needed here.

When you add a new **sheet** or **column**, update `lib/xlsx.ts` and the matching UI table in `app/tuning/page.tsx`.

## Costs

- Vercel: free tier covers this easily (single-digit requests/day from a few users).
- GitHub: free.

Total: $0.
