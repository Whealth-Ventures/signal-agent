# Signal Agent — Admin UI

A small Next.js app for editing every input to the [`signal-agent`](https://github.com/Whealth-Ventures/signal-agent)
daily healthcare digest. Shared-login auth, no database. Each save serializes back
to the same file the agent reads and commits it to the agent repo via the GitHub API.

Lives in the `admin/` subdirectory of the agent repo (not a separate repo).

## What it edits

| Page | File in the agent repo | Notes |
|---|---|---|
| **Keywords** | `inputs/keywords.xlsx` | The ~2,240 keywords (Bucket / Sub-bucket / Keyword / Geo) the query planner clusters into each day's Perplexity searches. Flat editable table with a filter. |
| **Sources** | `inputs/voices.xlsx` | Publications, top voices, org pages, PE/VC firms. Add a publication with its URL and the fetcher auto-discovers its RSS feed. Five tabs matching the xlsx sheets. |
| **Tuning** | `inputs/tuning.xlsx` | Every numeric knob, regex booster, priority bucket, and source tier. Four tabs matching the four xlsx sheets. |
| **Prompts** | `prompts/ranker_system.md`, `prompts/magnitude_rubric.md` | The two LLM prompts, edited as plain textareas. |
| **Content corpus** | `inputs/content/**/*.md` | The firm's own published pieces — the "taste profile" the agent scores relevance against. Browse / edit / add / delete. |

Parse and serialize preserve the exact layout the agent's positional loaders
(`src/query_planner.py`, `src/tunables.py`) depend on, so edits are round-trip safe
(`node lib/keywords.check.mjs`, `node lib/voices.check.mjs`). The xlsx / md files
stay the single source of truth — the UI is only an editor over them.

## How a save reaches the agent

Each save is its own commit on the agent repo's `main` branch (`git log inputs/`
and `git log prompts/` show the history; the shared-login name is recorded in the
commit author name + message for audit).

**Important — a commit does not automatically reach the running agent.** The
production box does not read GitHub at runtime; it runs the `inputs/` + `prompts/`
baked into the **last deployed S3 artifact** (push model — see the agent repo's
`deploy/` + `Jenkinsfile`). So an admin save takes effect only after a new
artifact is built and deployed to the box. Automating that (build on push →
deploy) is the top open item in the repo-root **`FEEDBACK.md`**; until it lands,
input/prompt edits reach the box only on the next deploy.

## Deployment

Deployed as the Vercel project **`signal-agent-admin`** under the **2070Health** team:

- **Root Directory** = `admin` (the app is a subdirectory of the agent repo).
- **Git-connected** to `Whealth-Ventures/signal-agent` → every push to `main`
  auto-redeploys. Since in-app saves commit to that repo, saves also redeploy the UI.
- `vercel.json` pins `"framework": "nextjs"` — required because the project was
  created via CLI (which skips framework auto-detection).
- **Deployment Protection must be OFF** for this project. The 2070Health team
  enables "Vercel Authentication" (SSO) on new projects by default, which walls off
  the shared-login UI behind team login. Disable it under Settings → Deployment
  Protection (or via the API: `PATCH /v9/projects/<id>` `{"ssoProtection":null}`).

Manual deploy (if ever needed): from the repo root, `vercel deploy --prod --scope 2070health`.

### Environment variables

Set these in the Vercel project (Settings → Environment Variables), all environments:

| Name | Value |
|---|---|
| `GITHUB_TOKEN` | Fine-grained PAT with **Contents: Read & Write** on `Whealth-Ventures/signal-agent` |
| `GITHUB_OWNER` | `Whealth-Ventures` |
| `GITHUB_REPO` | `signal-agent` |
| `GITHUB_BRANCH` | `main` |
| `AUTH_SECRET` | Random 32+ char string (`openssl rand -hex 32`) — signs the session JWT |
| `ADMIN_USER` | Shared login username |
| `ADMIN_PWD` | Shared login password (strong) |
| `GIT_COMMIT_EMAIL` | Author email recorded on commits (audit) |

To rotate access, change `ADMIN_PWD` and redeploy — everyone signs in again with the
new password. No per-person allowlist to maintain.

## How it works

- **Auth**: single shared login. `app/api/auth/login` checks the submitted username +
  password against `ADMIN_USER` / `ADMIN_PWD` (constant-time compare) and, on success,
  `lib/auth.ts` sets a 24-hour signed JWT session cookie. `middleware.ts` gates every
  route except `/login` and `/api/auth/login`.
- **GitHub I/O**: `lib/github.ts` reads/writes/deletes files via Octokit. The SHA
  round-trip prevents concurrent-edit overwrites — if a file changed since you loaded
  it, the write fails.
- **XLSX**: `lib/xlsx.ts` / `lib/voices.ts` / `lib/keywords.ts` parse each workbook to
  JSON on read and serialize JSON back on save, preserving the sheets/columns the
  agent loads positionally.
- **Validation**: server-side. Invalid regex, invalid geo, empty prompts, and paths
  outside `inputs/content/` are rejected before the commit.

## Local development

```bash
cp .env.example .env.local   # fill in a real GITHUB_TOKEN; set ADMIN_USER/ADMIN_PWD to anything
npm install
npm run dev
```

Visit `http://localhost:3000` and sign in with the `ADMIN_USER` / `ADMIN_PWD` from `.env.local`.

## Adding new tunables

When you add a knob to `inputs/tuning.xlsx` (Settings sheet) in the agent repo: add
the row, wire it up in `src/tunables.py` + `src/config.py`, and the UI picks it up
automatically. When you add a new **sheet** or **column**, update `lib/xlsx.ts` and the
matching table in `app/tuning/page.tsx`.

## Costs

Vercel free/team tier covers this easily (single-digit requests/day). GitHub: free.
