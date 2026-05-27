# How Signal Agent Works

A robot that reads healthcare news every morning and posts a briefing to Slack — the kind of briefing a junior analyst might write, but done by 10am every day, automatically.

This document is the orientation: what controls what, how the agent works in plain English, and where to look when something seems off. No code knowledge required.

---

## Where everything lives (and what you can change)

The repo has a few top-level folders. You only ever edit two of them.

### `inputs/` — everything you can change

This is your control room. Open `inputs/` and you'll see four things:

| What | What it controls | Edit how |
|---|---|---|
| `inputs/keywords.xlsx` | The topics the agent searches for (~2,240 keywords organized into themes). | Open in Excel, edit rows, save. |
| `inputs/voices.xlsx` | The people and publications the agent trusts. Marking someone as "Tier 1" makes their posts count more. | Open in Excel, edit rows, save. |
| **`inputs/tuning.xlsx`** | **Every dial and threshold the agent uses.** How aggressive should dedup be? How many stories to show? What weight to give a funding mention? Four sheets, one row per knob, plain English descriptions. | Open in Excel, change a cell, save. |
| `inputs/content/` | The firm's own writing — articles, podcasts, LinkedIn posts. The agent reads this to figure out what kinds of stories "sound like yours". Add a new article and the agent's taste sharpens. | Drag a markdown file into the right subfolder. |

### `prompts/` — the agent's instructions to the AI

The agent sends two written instructions to the AI on every run. They live as plain markdown files.

| What | What it controls |
|---|---|
| `prompts/ranker_system.md` | The agent's "voice" when it asks the AI to pick stories. Edit this to change tone, framing, or what kind of editor you want the AI to act as. |
| `prompts/magnitude_rubric.md` | The cheat-sheet the AI uses to decide which stories are big news, noteworthy, minor, or skip. Move "FDA approval" from "biggest news" to "noteworthy" and FDA stories will get less prominence. |

### The rest (don't edit unless you're changing how the agent works)

- `src/` — the code.
- `data/` — the agent's working files. SQLite database of every story it's ever seen, a "math fingerprint" cache of your content, and one log file per module per day. **Logs in `data/logs/` are your first stop when something looks off.**
- `scripts/` — utility scripts. Most useful: `build_default_tuning_xlsx.py` regenerates `inputs/tuning.xlsx` if it gets into a bad state.
- `docs/` — these files, plus:
  - `docs/EDITING.md` — a quick "I want to change X, where do I go?" index
  - `docs/TUNING.md` — knob-by-knob detail for `inputs/tuning.xlsx`
  - `docs/scheduling.md` — how the 10am IST cron is set up
- `tests/` — automated tests for the code.

---

## How the agent works, in 4 steps

### Step 1 — Plan the searches

The agent reads your keywords (~2,240 of them) and groups them into about 34 search questions. Why 34 and not 2,240? Each search costs money. Asking 2,240 separate questions would burn the daily budget in minutes. So the agent clusters related keywords into themes — "everything India + venture capital" becomes one search, "everything US + FDA news" becomes another.

There are four kinds of searches:

- **8 priority categories** — Venture & IPO, PE & Strategics, Hospital M&A, MSO Roll-ups, FDA & Regulatory, Phase 3 / Hot Therapeutic Areas, US Medicare, AI in Healthcare. Each runs every day. Some are India-focused, some US-focused, some global, depending on the category. That's ~13 search questions total.
- **Long-tail categories** — the smaller themes that don't fit the 8 priorities. There are too many to run every day, so the agent picks 18 different ones each day on a 14-day rotation. By the end of two weeks, every long-tail theme has been covered.
- **Named voices** — 2 searches that ask the AI: "what has [list of your Tier-1 healthcare voices] posted in the last 24 hours?" One for India, one for US.
- **PE/VC firms** — 1 search that asks about deal news from the firms on your "New Additions" tab.

That's 34 well-phrased questions ready to send out.

This step is 100% deterministic — give it the same Excels and it produces the same 34 questions, every time. No AI involved here.

### Step 2 — Run the searches

The agent fires those 34 questions at Perplexity (an AI-powered search engine). Each question gets back a list of stories: title, link, summary. The agent runs 5 of these questions at the same time, so the whole batch finishes in roughly 30 seconds instead of several minutes.

In parallel, the agent also pulls **RSS feeds** (auto-published news feeds) from the trusted publications listed in your voices spreadsheet. RSS catches stories from sources you already trust without burning the Perplexity budget on them.

By the end of Step 2, the agent has 100–300 raw stories. Lots of duplicates, lots of noise — that's expected.

### Step 3 — Dedupe and score

**Dedupe first.** The same story often shows up in five outlets with different headlines ("FDA approves Alzheimer's drug" vs "Eli Lilly's Kisunla gets nod from FDA"). The agent converts each headline+summary into a "math fingerprint" and compares them. Any two stories whose fingerprints are more than 85% similar get collapsed into one. The agent also drops any story that already shipped in any digest in the last 30 days.

**Then score.** For each surviving story, the agent compares its fingerprint to the firm's own writing (in `inputs/content/`). The closer the match, the higher the base score. On top of that, the agent adds bonuses and penalties:

- Story mentions a Tier-1 voice from your spreadsheet: **+0.10**
- Story is from a trusted publication you've curated: **+0.08**
- Story mentions a firm from your "New Additions" tab: **+0.08**
- Funding round / M&A / regulatory language: **+0.05**
- Product launch or leadership-move language: **+0.03**
- Looks like a listicle ("10 Best Healthcare Startups"): **−0.10**
- Looks like an opinion column ("Opinion: Why Medicare needs reform"): **−0.05**

Final score is one number per story, roughly between 0 and 1. These can all be re-weighted in `inputs/tuning.xlsx` → Boosters sheet.

### Step 4 — Pick what goes in the briefing

The agent sends every scored story to Perplexity's reasoning model (a stronger AI, used only once per run) along with the rubric from `prompts/magnitude_rubric.md`. The AI rates each story:

- **Biggest news** (always include)
- **Noteworthy** (include if there's room)
- **Minor mention** (include only if a category would otherwise be empty)
- **Skip** (drop entirely)

The AI also writes a one-line headline for each story it keeps — newsroom style, max ~120 characters.

The agent then assembles the Slack post:
- The 5 highest-rated stories get pulled into a **"Today's biggest stories"** section at the top.
- The rest go into per-category sections (Venture & IPO, PE & Strategics, etc.). Categories that have no stories are hidden.
- Anything from voices/firms/RSS without a category goes into an **"Other healthcare news"** section at the bottom.

Before posting, the agent checks every link actually works (no 404s) — broken links get dropped, not shipped. Then it posts to Slack.

If the AI call in Step 4 fails for any reason, the agent falls back to "treat everything as Noteworthy, score-order within each category" — so the digest always ships, even if the smart ranking is unavailable. The post will say `(used score-based fallback...)` when this happens.

---

## When something looks off

| What you noticed | Probably means | Where to fix it |
|---|---|---|
| The briefing is too India-light or too US-light | Keyword balance is uneven | `inputs/keywords.xlsx` — add or rebalance keywords with the right Geo |
| Same story keeps appearing day after day | The 30-day "don't repeat" window is set wrong, or the similarity bar is too high | `inputs/tuning.xlsx` → Settings → `dedup_window_days` or `historical_dedup_threshold` |
| Stories feel "generic" rather than investor-relevant | The AI's instructions are too vague | `prompts/ranker_system.md` — sharpen the criteria you care about |
| Listicles keep slipping through | The penalty is too soft | `inputs/tuning.xlsx` → Boosters → `listicle` → make the weight more negative (e.g. -0.20) |
| Big stories you knew about are missing | The source isn't in your trusted feeds AND the keyword cluster missed it | `inputs/voices.xlsx` → Newsletters & Publications tab — add the source |
| Wrong category is getting too much weight | The magnitude rubric is rating it too highly | `prompts/magnitude_rubric.md` — move the relevant criteria from "biggest news" to "noteworthy" |
| Top section feels too short / too long | The top-summary size needs adjustment | `inputs/tuning.xlsx` → Settings → `top_summary_size` (default 5) |
| The whole briefing is too short / too long | The ceiling on total stories is too low / high | `inputs/tuning.xlsx` → Settings → `max_digest_items` (default 40) |
| Want to test changes without polluting the Slack channel | Use a flag | `python src/main.py --test` posts with a `[TEST]` marker and doesn't write to the 30-day dedup history. `--dry-run` skips Slack entirely and writes the post to disk. |

For a knob-by-knob reference of `inputs/tuning.xlsx`, see [`docs/TUNING.md`](docs/TUNING.md).

For a one-page "where do I edit X?" index, see [`docs/EDITING.md`](docs/EDITING.md).

---

## Running it yourself

| Command | What it does |
|---|---|
| `python src/main.py` | Full run — fetches, scores, ranks, posts to Slack |
| `python src/main.py --test` | Same, but Slack post is marked `[TEST]` and doesn't enter the 30-day dedup history |
| `python src/main.py --dry-run` | Skip Slack entirely; the Slack-formatted post gets written to `data/logs/dry_run_digest_<date>.json` |
| `python src/main.py --max-plans 3` | Run only the first 3 of 34 search questions (fast iteration) |
| `python src/main.py --skip-rss` | Skip the RSS pull (saves ~30s) |
| `python src/main.py --skip-url-validation` | Skip the link-check before posting |

If a full run takes much longer than 2–3 minutes, check `data/logs/pipeline_<date>.jsonl` for the bottleneck. Each module also writes its own daily log (e.g., `data/logs/perplexity_<date>.jsonl`) — that's where every API call, every cost, every story considered is recorded.
