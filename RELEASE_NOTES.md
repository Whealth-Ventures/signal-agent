# Signal Agent — Release Notes

## The same news from two different publications no longer takes two slots (2026-09-03)

The previous release stopped one article appearing twice when a publisher served
it under several URLs. It could not stop **two outlets reporting the same event**
from becoming two stories, because the headlines, links and slugs are all
genuinely different — nothing on the surface says they are the same news.

Found in a dry run the same day: MediBuddy's appointment of Shalabh Shrivastava
was reported by Entrackr, BioSpectrum India and Express Healthcare across three
different mornings. Two of the three took **two of the five headline slots** in
one digest.

Before the digest is built, every candidate story is now compared against the
others by meaning rather than by text, and repeats are collapsed to the
strongest one. This works no matter which day each version arrived, which is
what the previous check couldn't do: the stories were compared only against
others fetched in the same run.

No extra cost. The comparison reuses the story descriptions the agent already
computes for its own de-duplication.


## Better story choice, honest geo tags, and a visible warning when the digest is degraded (2026-09-02)

Answers the 2 Sept feedback on the India digest: too much `[GLOBAL]`, a missing
Ultrahuman funding story, and an opinion piece filed under Venture & IPO.

### The India/US/Global tag now reflects where the news happened
Previously a story's geography was inherited from **how we found it**, never
from what it said. A search result took the geography of the query plan that
surfaced it, so anything found by a Global plan was Global regardless of
content. An RSS story carried no geography at all, so it came out
unclassified, rendered `[GLOBAL]`, and was sent to **both** channels. On
2 September that was 105 of 160 stories.

**The ranker now decides.** It already reads every candidate, so it returns the
geography alongside the tier and category, judged from the story itself and
explicitly not from the nationality of the publication that reported it. An
Indian outlet covering a US hospital merger is now tagged US; a US outlet
covering an Indian funding round is tagged India.

Two fallbacks sit behind it, in order: the publication's own `Geography` from
`voices.xlsx` (newly stamped onto every RSS item — a live sweep turned 130
unclassified signals into 103 India and 27 US), then the query plan's
geography. Both are proxies for where we *found* the story rather than where it
happened, so they now only fill gaps.

Practical effect: the India digest stops calling Indian news global, stops
carrying US news, and the US digest stops receiving Indian coverage.

### The best story of the day can no longer be pushed out by a newer, weaker one
Story ordering put publish time first and relevance score last, so within a
category a fresher story always beat a stronger one. With only 1–2 slots per
category, that dropped real news: on 2 Sept an Ultrahuman funding exclusive
(the day's second-highest scored story, caught by three separate sources) and a
$22M Series B (the highest scored) both missed the digest, beaten by items
published a few hours later. Ordering is now magnitude tier, then score, then
recency.

### "Venture & IPO" is no longer the dumping ground
A story that the ranker didn't categorise used to land in whichever bucket sat
first in the sheet, which is Venture & IPO. That is how an opinion piece about
the American Diabetes Association was published as venture news. The catch-all
is now an explicit `default_bucket` setting, and a bucket can be marked
display-only (no geographies) so "Other healthcare news" can exist as an honest
destination without spending any search budget on it. The ranker can also assign
it directly, so it no longer has to force non-deal news into a deal category.

### A degraded digest now says so
When ranking falls back, the post carries a warning line: no magnitude tiers, no
categories, no written summaries, story choice less reliable than usual.
Previously this was printed only to the server console, which is how ten days of
RSS-only digests (22–31 Aug) shipped without anyone noticing. Closes the ranker
half of `FEEDBACK.md` #4.

### The same story can no longer appear twice
A publisher often serves one article under several URLs (BioSpectrum India puts
the same piece at `/news/16/28410/…` and `/news/101/28410/…`), so URL matching
missed it and both copies competed as separate stories. On 3 Sept, AIG Hospitals
and Luma Fertility each appeared twice, costing 4 of 15 slots.

Repeat tellings are now collapsed on the **candidate pool**, before categories
are filled, so the freed slot goes to the next real story rather than being
lost, and the ranker never sees two copies to rank separately. Three tests of
sameness: normalised title, normalised URL, and host plus final URL segment.

### Social and AI-generated "news roundup" sources can be blocked
Search results sometimes cite AI-generated daily-roundup pages on social
platforms. Those recycle weeks-old headlines under a fresh date, so an old story
looks new and the digest links to a video post instead of the publication. Ten
such stories shipped between 22 July and 3 September, including an "Even
Healthcare Series B" item that was actually reported on 23 July and resurfaced
on 2 September through a Facebook repost.

There is now a **`blocked_domains`** setting in `tuning.xlsx`: a semicolon-
separated host list, matched across subdomains, enforced at the single point
every story passes through, so a blocked host cannot enter by any route. Adding
or removing a host is a SharePoint edit with no deploy.

### Headline rewrite hardening
Follow-ups from the review of the previous release. One malformed article URL
used to abort all 25 article fetches and silently disable the rewrite for the
whole run (`httpx.InvalidURL` is not an `httpx.HTTPError`). An over-length
headline was discarded entirely rather than trimmed at a word boundary. The
headline length cap now follows the same `tuning.xlsx` setting as the rest of the
digest, the rewrite's cost is included in the run total, and an unparseable
response is logged with its body so it can be told apart from "the model kept
every headline".


## Headline rewrite — one-liners written from the article body (2026-09-01)

Digest one-liners used to be written by the ranker from the fetched title plus
a ≤500-char snippet — it never read the article. When the source headline was
vague ("Care has gone continuous. Operating system hasn't"), the digest line
was equally opaque.

**Now: a post-selection rewrite pass reads the winning articles.** After
ranking and geo filtering, `src/headline_rewriter.py` fetches the ~15–25
stories that actually made the digest, extracts a body excerpt, and makes ONE
further LLM call (same Claude/Perplexity vendor selection as the ranker) that
rewrites each one-liner from the article body — 5–10 words, newsroom sentence
case, attribution for op-eds, no invented facts (prompt:
`prompts/headline_system.md`).

- **Fail-soft everywhere**: a fetch or LLM failure keeps the ranker's
  one-liners; the digest never blocks on a rewrite.
- **Cost**: one extra ranker-class LLM call per geo run (counts against the
  Perplexity budget only when the ranker is on the Perplexity path).
- **Audit log**: `data/logs/headlines_<date>.jsonl` records every rewrite.
- **Opt-out**: `--skip-headline-rewrite`.
- Verified live on Perplexity `sonar-reasoning-pro` (1 Sept 2026): vague
  op-ed and mismatched-slug articles all rewritten correctly from body text.

## v1.5.0 — "SharePoint is the source of truth for inputs" (2026-08-07)

Until now, the agent's inputs lived as files committed into the repo — a copy of
what was already in SharePoint. Changing a keyword or a tuning number meant
downloading the workbook, editing it, committing it, and waiting for a deploy.

**Now: edit the file in SharePoint and you're done.** Every run mirrors the
SharePoint inputs folder down before it starts, so the change is live on the next
morning's digest. No download, no commit, no deploy.

This covers everything under `inputs/`: `keywords.xlsx`, `voices.xlsx`,
`tuning.xlsx`, `portfolio.xlsx`, `portfolio_context.md`, and the whole
`content/` corpus. A file deleted in SharePoint disappears from the agent too.

### The input files are gone from the repo
`inputs/` is no longer in git at all — it's a local cache the sync fills in.
There is now exactly one copy of each input, in SharePoint, so the two can't
drift and there's no question about which is authoritative.

Practical effects: a fresh checkout must sync before it can run anything, and
tests that read the real workbooks skip themselves until it has. CI pulls the
same SharePoint credentials the production box uses, so it keeps testing against
the real inputs.

### The admin UI is now prompts-only
Its **Keywords, Sources, Tuning, Portfolio, and Content corpus** pages have been
removed — SharePoint owns those files, and anything the UI wrote would be
overwritten within a day. Leaving them in place would have meant edits vanishing
silently, which is worse than not having the page.

**Prompts** remains, and still works exactly as before: it commits to the repo
and applies on the next deploy. The LLM prompts are not in SharePoint.

### If SharePoint is down
The digest still ships. A failed sync prints a warning and the run continues on
the last successfully-synced files, so an Azure or SharePoint outage can't take
out the 08:00 post. The flip side: if a SharePoint edit doesn't show up in a
digest, look for `WARN: sharepoint sync failed` in that run's log.

### Setup (one-time, needs a tenant admin)
Access is an Azure AD app registration with app-only Microsoft Graph permission
scoped to the single SharePoint site (`Sites.Selected` — it can read that one
site and nothing else in the tenant). `scripts/grant_sharepoint_access.py`
performs the site-level grant, which can only be done over the Graph API, not in
the portal. Credentials live in AWS Secrets Manager alongside the other keys.
With them unset the sync is a no-op, which is how local development runs.

## v1.4.0 — "Sector Agent" (2026-07-22)

A **third agent** joins the two daily geo digests: a **weekly, portfolio-focused
Sector Agent** that posts to a third Slack channel ("Signal Agent Sector").

### 1. What it does
Every Monday at 08:00 IST it sweeps the news for each of the ~16 W Health /
2070 Health portfolio companies and surfaces only developments with a **material
business impact** — positive or negative — on that company: sector/industry
shifts, regulation & reimbursement, macro moves, and **direct-competitor** actions
(funding, launches, M&A, pricing, exits), in the company's geography or globally.
It deliberately **ignores each company's own PR** — this is the world *around* the
portfolio, not the portfolio's own announcements. The digest is grouped by company,
with a ↑ / ↓ / ↔ marker showing the direction of impact on each one.

### 2. New editable input — Portfolio
The company list lives in `inputs/portfolio.xlsx` (Company, Sector, What they do,
Geo, Website) and is editable from a new admin **Portfolio** page, just like
Keywords/Sources/Tuning. Edit a company's description or add/remove companies there;
the next weekly run uses it.

### 3. Built to not disturb the daily digests
The Sector Agent is its own entrypoint (`python src/sector_main.py`) with its own
timer and its **own database** (`data/db/sector.db`), so its stories can never
appear in the daily India/US digests and vice versa. It reuses the daily
pipeline's fetch, dedup, and Slack machinery under the hood. Requires
`SLACK_CHANNEL_ID_SECTOR` and the bot invited to the new channel.

## v1.3.0 — "Everything in the Panel" (2026-07-18)

The admin panel now edits **every** input to the agent, and we retired the
suggestions experiment.

### 1. All inputs are now editable from the admin UI
Previously only Sources, Tuning, and Prompts were in the panel. Two big ones were
missing — now they're in:
- **Keywords** — the ~2,240 search terms (Bucket / Sub-bucket / Keyword / Geo) that
  drive every day's research. A flat, filterable table; add, edit, or remove rows.
- **Content corpus** — the firm's own published pieces that define "sounds like us"
  for relevance scoring. Browse, edit, add, or delete them.

So the full set — **Keywords, Sources, Tuning, Prompts, Content** — is now UI-driven.
Every save still writes back to the same Excel / Markdown files in the repo; the UI
is just a friendlier editor over them.

### 2. The Suggestions feature was removed
The Slack 👍/👎 → automatic tuning-suggestions loop has been retired end to end
(the Suggestions page, the reaction pipeline, and its data). We'll revisit
auto-improvement separately later.

### 3. Fresh admin deployment
The admin panel now lives on its own Vercel project (`signal-agent-admin`, 2070Health)
and redeploys automatically when the repo changes.

> **Heads-up on how edits go live:** an admin save commits to the repo immediately,
> but the running digest picks up input/prompt changes only on the next **deploy**
> to the agent box (it runs the inputs baked into the last deploy, not GitHub
> directly). Automating that so edits go live on the next morning's digest is the
> top item in `FEEDBACK.md`.

## v1.2.0 — "Sharper Signal" (2026-06-05)

A batch of improvements to the digest itself, when it lands, the tuning page, and
the thumbs-up/down feedback loop. Please try them and tell us what feels off.

### 1. The digest is more consistent and better organized
- **No more thin days.** Some mornings had ~10 stories, others ~25. It now aims
  for a steady **18–22** — slow news days get topped up so the digest never feels
  empty, busy days stay tight.
- **Cleaner layout.** Fixed the double line under "Today's biggest stories" (now a
  single divider).
- **"Other healthcare news" is now grouped by topic** instead of one long list, so
  the long-tail is easier to skim.

### 2. It's built to land at 10:00 IST, on the dot
The digest is now fully assembled a few minutes early and held until exactly
**10:00 IST** to post — so arrival time no longer drifts with how long the run
takes. (For this to be reliable to the minute, the external 10:00 trigger needs to
be set up — see `docs/scheduling.md`.) A safety check also makes sure the digest
can never be sent twice in a day.

### 3. The tuning page is simpler
The Settings tab now shows only the handful of knobs that are meaningful to
adjust (how many stories, how long to avoid repeats, how many topics to explore,
headline length, etc.), each with a plain-English label. The technical internals
are tucked behind a **"Show advanced settings"** toggle. Priority Buckets and
Source Tiers are unchanged.

### 4. Your 👍 / 👎 are now visible — and feed back in
- The **Suggestions** page has a new **"Recent reactions"** panel that shows your
  Slack 👍/👎 within seconds of reacting, so you can confirm feedback is landing.
- The agent now pulls those reactions in every day and turns the contrast between
  liked and disliked digests into tuning suggestions automatically.
- The bar to suggest a change is lower now: **one upvoted and one downvoted**
  digest (was three of each).

**In short:** steadier, tidier digests that aim to land at 10:00 sharp, a tuning
page anyone can use, and feedback you can actually see. Have a play and let us know.

## v1.1.0 — "Simpler Sign-In" (2026-06-04)

A quick fix to how you log in to the tuning page.

### Sign-in no longer uses email links
Some people weren't receiving the magic-link email, so they couldn't get in at
all. We've removed email from sign-in entirely.

There's now **one shared username and password** for the admin page. Anyone on
the team can use it.

**How to log in now:**
- Go to **https://signal-agent-admin.vercel.app**
- Enter the shared **username and password** (ask Ashwin for it).
- That's it — no email, no waiting for a link.

Everything else on the page works exactly as before. If you're ever locked out,
the password can be changed centrally and you'll just sign in again with the new
one.

> Note: the magic-link / "enter your work email" steps in v1.0.0 below are now
> replaced by the username + password above.

## v1.0.0 — "First Signal" (2026-05-29)

The first major release of Signal Agent: a daily healthcare-news digest that
posts to Slack every morning, can be tuned without touching code, and learns
from your reactions.

A few upgrades landed over the last three days. Here's the plain-English
version — please try them out and tell us if anything feels off. We're now set
up to keep improving this quickly based on your feedback.

### 1. The digest is faster and stays on-topic
- The morning digest now builds in about **2–3 minutes** (was 7–10).
- Fixed a problem where general startup news (banking, fintech, edtech) was
  sneaking into what should be a **healthcare-only** digest. It now reliably
  drops anything that isn't healthcare.
  → _Flag any story that doesn't belong._

### 2. You can tune it yourself — no engineer needed
There's a simple web page where you can adjust how the agent picks and ranks
stories (which topics to prioritize, which sources to trust, etc.).

**How to log in and make a change:**
- Go to **https://signal-agent-admin.vercel.app**
- Enter your work email and click send — you'll get a **magic link** by email
  (no password). Click it to sign in.
- Open the **Tuning** page. It has 4 simple tabs: Settings, Boosters, Priority
  Buckets, Source Tiers.
- Change something small, hit **Save**. That's it — it saves automatically and
  the *next* morning's digest will reflect it.

### 3. Your 👍 / 👎 in Slack now trains the agent
- React to a digest in Slack with thumbs up or down.
- The agent compares the digests you liked vs. disliked and **suggests**
  specific improvements.
- Those suggestions show up on the same web page (the **Suggestions** tab),
  where you **Accept** (applies automatically) or **Reject** — nothing changes
  without your okay.
- Even a single 👍 now counts, so feedback helps right away.

**In short:** react in Slack to teach it, use the web page to fine-tune it, and
the daily digest should be quicker and more on-topic. Have a play and let us
know what works and what doesn't — if something's broken, we're now ready to
fix it fast.
