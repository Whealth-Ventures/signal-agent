# Signal Agent — Admin UI

A small Next.js app for tuning the [`signal-agent`](https://github.com/ashwinknan/signal-agent) daily healthcare digest. Magic-link auth, no DB, auto-commits to the agent repo on save.

## What it edits

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

### 3. Set up Resend for magic-link emails

- Sign up at [resend.com](https://resend.com) (free tier: 100 emails/day, 3000/month).
- Add and **verify a domain you own** (5-min DNS setup — TXT + MX records). The `from` address must be on a verified domain; Resend won't deliver from unverified senders.
- Generate an API key (Dashboard → API Keys → Create).

### 4. Import to Vercel

- Go to [vercel.com](https://vercel.com) → Add New → Project → Import the `signal-agent-admin` repo.
- Framework preset: **Next.js** (auto-detected).
- Don't deploy yet — set env vars first (next step).

### 5. Set env vars in Vercel

In the project settings → Environment Variables, add:

| Name | Value |
|---|---|
| `GITHUB_TOKEN` | the `github_pat_...` from step 2 |
| `GITHUB_OWNER` | `ashwinknan` (or whoever owns the agent repo) |
| `GITHUB_REPO` | `signal-agent` |
| `GITHUB_BRANCH` | `main` |
| `AUTH_SECRET` | a random 32+ char string (e.g. `openssl rand -hex 32`) |
| `RESEND_API_KEY` | from step 3 |
| `RESEND_FROM` | `Signal Agent <admin@yourdomain.com>` |
| `ALLOWED_EMAILS` | comma-separated whitelist, e.g. `engineering@2070health.com,partner@2070health.com` |
| `APP_URL` | leave blank — Vercel will use `VERCEL_URL` automatically once deployed |

Set them for all environments (Production / Preview / Development).

### 6. Deploy

- Trigger a deploy in Vercel (will happen automatically after env vars are set on next push, or click "Redeploy").
- Once green, visit the deployment URL → enter your email → check inbox → click link → you're in.

Adding a new editor later = adding their email to `ALLOWED_EMAILS` and redeploying (or use a Vercel "Edge Config" later if list churns often).

## Local development

```bash
cp .env.example .env.local
# Fill in real values (you can use the same GITHUB_TOKEN; magic links will go to real emails)
npm install
npm run dev
```

Visit `http://localhost:3000`. Note: magic links sent from local dev will still point at `http://localhost:3000` only if `APP_URL` is set in `.env.local`.

## How it works

- **Auth**: `lib/auth.ts` issues 15-minute signed JWT tokens for the magic link, and 24-hour signed JWT session cookies once verified. No DB; the whitelist lives in `ALLOWED_EMAILS`.
- **GitHub I/O**: `lib/github.ts` reads/writes files via Octokit. SHA round-trip prevents concurrent-edit overwrites — if the file changed since you loaded it, the write fails.
- **XLSX**: `lib/xlsx.ts` parses 4 sheets into JSON on read, serializes JSON back to xlsx on save. Schema mirrors `src/tunables.py` in the agent — keep them in sync.
- **Validation**: server-side. Invalid regex / invalid geo / empty sub-buckets / empty prompts are rejected before the commit.
- **Audit**: each save is its own commit with the editor's email as author. `git log inputs/tuning.xlsx` and `git log prompts/` show the history.

## Adding new tunables

When you add a new knob to `inputs/tuning.xlsx` (Settings sheet) in the agent repo:

1. Add a row to the xlsx with name/value/description.
2. Wire it up in `src/tunables.py` and `src/config.py` in the agent.
3. The admin UI picks it up automatically — no changes needed here.

When you add a new **sheet** or **column**, update `lib/xlsx.ts` and the matching UI table in `app/tuning/page.tsx`.

## Costs

- Vercel: free tier covers this easily (single-digit requests/day from a few users).
- Resend: free tier (100 emails/day) is overkill — you'll send <10 magic links per month.
- GitHub: free.

Total: $0.
