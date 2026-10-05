# News archive and Q&A: plan

**Goal: let people ask Signal Agent questions about past news, not just read the daily digest.**

The target question: *"What are the biggest IPO stories this month? Give me a two-line summary and their links."*

Decided 25 September 2026.

## Decisions

| Question | Decision | Why |
|---|---|---|
| Where the data lives | SQLite, as a new `story_details` table in `data/db/agent.db` | The bot runs on the same box. Neon's free tier (0.5 GB) would fill in about 6 months. SQLite 3.40 on the box has FTS5, WAL is on, and the nightly S3 backup already covers `data/`. |
| Article text | Full body, capped at 20,000 characters | Answers need more than the ~300-character summary. |
| How bodies are fetched | Reuse `headline_rewriter._fetch_excerpts` | It already fetches 80% of digest articles (256 of 322, last 20 prod runs). No new library. |
| Tagging model | OpenAI `gpt-4.1-mini`, the key already used for embeddings | Bedrock Claude isn't set up in the W Health AWS account yet. |
| Backfill | Last 30 days (~4,900 stories) | |
| Q&A surface | Same Slack app, same box, a separate service | Same pattern as salesforce-sage. |
| Neon | A one-way read copy, added 28 September 2026 (`src/neon_sync.py`) | For live browsing in DBeaver. SQLite stays the source of truth. Bodies older than 12 months are dropped in Neon only, so the 0.5 GB free tier lasts about 4 years. |

## Phase A: bodies and tags (this branch, `feat/subhanu-news-archive`)

- **`src/enricher.py`** runs after every digest post, as its own process, from `deploy/run-digest.sh`. It can't delay a digest, and a failure only prints `WARN: enrich failed`.
- **Pass 1, fetch.** Every story first seen in the last 2 days without a `story_details` row gets its body. A failed fetch still gets a row (`body_status='failed'`), so it isn't retried.
- **Pass 2, tag.** Untagged stories from the last 30 days go to OpenAI 10 at a time, with 4 calls in parallel. Each batch is saved as it lands. A failed call leaves its stories untagged, and a later run retries them, so an OpenAI outage of up to 30 days heals on its own.
- **Healthcare check first.** The model answers `healthcare: true/false`, and, when the article body was fetched, the digest's own lexicon gate (`topicality.is_healthcare`, on title, summary and body) can veto a yes. Without a body the model decides alone: a title like "Ultrahuman raises $12M Series B" has no healthcare word for the lexicon to find. A non-healthcare story is `not_healthcare`, tier C, with no facts. In the first live test the model alone filed 26 non-healthcare IPOs (NSE, steel, fintech) under Venture & IPO; with the check, none got through.
- **Every story gets the same common labels:** `category` (one of the 8, `other_healthcare`, or `not_healthcare`), `magnitude` (S/A/B/C, the ranker's own rubric, so "biggest" means what it means in the digest), `companies`, `geo`, and a two-sentence `summary`.
- **Each category also gets its own facts**, stored as JSON in `facts`, so a story only has the fields its kind of news has. Defined in `enricher.CATEGORY_FIELDS`:

  | Category | Facts |
  |---|---|
  | Venture & IPO | event (ipo, ipo_filing, funding_round, fund_raise), round, amount_usd, valuation_usd, investors, exchange |
  | PE & Strategics | event (acquisition, buyout, take_private, stake, platform, exit, fund_raise), buyer, target, amount_usd |
  | Hospital & Health System M&A | event (acquisition, merger, affiliation, divestiture, closure), buyer, target, amount_usd, facilities |
  | Physician Practice & MSO Roll-ups | event (acquisition, platform_launch, add_on, recapitalization), platform, target, specialty, sponsor, amount_usd |
  | FDA & Regulatory | event (approval, rejection, clearance, label_change, warning_letter, recall, guidance), regulator, product, indication |
  | Phase 3 / Hot Therapeutic Areas | event (trial_readout, trial_start, launch), therapy_area, drug, trial_phase, outcome |
  | US Medicare | event (final_rule, proposed_rule, payment_update, legislation, enforcement), agency, program, effective_date |
  | AI in Healthcare | event (product_launch, partnership, deployment, research, policy), use_case, product, customers |
  | Other healthcare (fits none of the 8) | none |
  | Not healthcare | none (always tier C) |

- **Money is copied as written and converted in code.** The labeller returns `{value, unit, currency}`, and `enricher.parse_money` converts it with fixed rates, storing `<field>_usd` for ranking and `<field>_text` (for example "₹4,800 Cr") for display. Changed on 29 September, after the model's own conversions lost rupee amounts and slipped units.
- **Deals always go in a deal category**, even when the company is an AI or drug company. So an AI scribe's funding round is Venture & IPO, and money fields only ever sit on deal stories.
- **Validation.** Off-list values fall back to `other` or null instead of failing the story. Any ids the model invents are ignored.
- **Audit trail.** `data/logs/enrich_<date>.jsonl` gets one line per call (tokens, latency, error) and a `run_done` line with the estimated cost.

### Rollout

1. Add `DATABASE_URL` (Neon, direct connection) to the agent secret, then merge. Jenkins deploys in about 100 seconds and installs the one new dependency, `psycopg[binary]`, from `requirements.txt`. No new unit file.
2. The next digest run (India 02:20 UTC or US 11:50 UTC) enriches the last 2 days automatically.
3. Check the `run_done` line in `data/logs/enrich_<date>.jsonl`.
4. Backfill 30 days. Run it between digest runs, so the two don't compete for the DB:

   ```bash
   sudo systemd-run --unit=signal-enrich-backfill --uid=signal \
     -p EnvironmentFile=/opt/signal-agent/shared/agent.env \
     -p WorkingDirectory=/opt/signal-agent/repo \
     /opt/signal-agent/repo/.venv/bin/python src/enricher.py --days 30
   journalctl -u signal-enrich-backfill -f
   ```

### Checking it answers the target question

```sql
SELECT s.canonical_title, s.canonical_url, d.summary, d.magnitude, d.facts
FROM story_details d JOIN stories s ON s.id = d.story_id
WHERE d.category = 'venture_ipo'
  AND json_extract(d.facts, '$.event') IN ('ipo', 'ipo_filing')
  AND s.published_at >= '2026-09-01'
ORDER BY COALESCE(instr('SABC', d.magnitude), 9), json_extract(d.facts, '$.amount_usd') DESC
LIMIT 10;
```

## Browsing the archive in DBeaver (Neon copy)

Neon gets a copy at the end of every run, so it matches prod whenever no run is in progress.

1. **Neon console → Connect**, choosing the **direct** connection (host without `-pooler`), gives the host, database, user and password.
2. **DBeaver → Database → New Database Connection → PostgreSQL:**
   - Host: the Neon host. Port: `5432`. Database: from Neon. Username and password: from Neon.
   - **SSL tab:** Use SSL, mode `require`.
   - **General → Security:** tick **Read-only connection**. Edits wouldn't reach prod anyway, and the next sync overwrites them.
3. **Browse:** schema `public` holds the India and US daily data, and schema `sector` holds the weekly sector data. The tables are `stories`, `signals`, `digests`, `digest_stories` and `story_details` (article bodies and labels; `facts` and `companies` are `jsonb`).
4. **Disconnect when done.** An open connection can keep Neon awake, and the free tier has 100 CU-hours a month.

The target question, in Postgres:

```sql
SELECT d.magnitude, s.canonical_title, s.canonical_url, d.summary,
       (d.facts->>'amount_usd')::numeric AS amount_usd
FROM story_details d JOIN stories s ON s.id = d.story_id
WHERE d.category = 'venture_ipo' AND d.facts->>'event' IN ('ipo', 'ipo_filing')
  AND s.published_at >= date_trunc('month', now())
ORDER BY array_position(ARRAY['S','A','B','C'], d.magnitude), amount_usd DESC NULLS LAST;
```

## Phase B: the Q&A bot (built 29 September 2026, `feat/subhanu-qa-bot`)

Setup steps are in [2026-09-29-qa-bot-setup.md](2026-09-29-qa-bot-setup.md).

- **`src/bot.py`**, a long-running `signal-agent-bot.service`. It uses slack-bolt Socket Mode, so it needs no public URL. It answers @mentions and DMs in threads.
- **The model gets fixed search tools and never writes SQL.**
  - `search_stories(query, category, event, geo, company, date_from, date_to, min_magnitude, limit)`
  - `get_story(id)` for the full body
- **The search index is built in one step.** `story_fts` (FTS5) is filled from `story_details` with a single `INSERT ... SELECT`, so Phase A doesn't need it.
- **Sources come from rows the search actually returned**, so a link can't be invented. This is salesforce-sage's approach.
- **The bot opens the DB read-only** (`mode=ro`).
- **Merge stories about the same event in an answer.** Different outlets covering one event are separate stories (ADARx's IPO appeared twice in the live test), so the answer groups them and cites every link.
- **Slack app changes** (in the Slack admin, before Phase B ships):
  1. Enable Socket Mode. Create an app-level token with `connections:write`. That token becomes `SLACK_APP_TOKEN`.
  2. Subscribe to the bot events `app_mention` and `message.im`.
  3. Add the scopes `app_mentions:read` and `im:history`.
  4. Reinstall the app to the workspace.
  5. Add `SLACK_APP_TOKEN` to the agent secret in Secrets Manager.

### Deal-size filter: self-review before merge (30 September 2026)

PR #21 adds `min_amount` / `max_amount` to `search_stories`, after the bot listed Marengo's $40M round (about ₹350 Cr) under "up to ₹200 Cr". A critical pass over the first cut found 1 real problem, now fixed.

| # | Problem | Fix |
|---|---|---|
| 1 | **A bound the code couldn't read was silently dropped, bringing the Marengo bug back.** `enricher.parse_money` returns None for a unit outside `MONEY_UNITS` (`cr`, `crores`) or a total under $10k. The enums in the tool schema are advisory, because tools aren't sent in strict mode. On the local archive, `max_amount {200, cr, INR}` returned all 10 Indian rounds instead of 7. | `search_stories` raises `ValueError` for a bound it can't read. `_dispatch` hands that back as a tool error, so the model corrects the bound and searches again. Other bad filter values are still ignored. |
| 2 | **A closing offer right after bold text survived.** `_CLOSING_OFFER` only matched after `.`, `!` or `?`, not after `**`. | Added `*` to the lookbehind. |

Checked, no change needed:

- **An exact-boundary deal is kept.** Lavni's ₹200 Cr fund and a ₹200 Cr bound go through the same formula, so they compare equal under `<=`.
- **Excluding deals with no amount costs little.** 6 of 56 funding rounds in the local copy (11%) have no amount. The prompt tells the model to say so.

Deliberately not done:

- **Accepting unit aliases** (`cr`, `mn`, `bn`, `lac`). The error path already covers them and any other misspelling. Aliases alone would still drop a new typo silently.
- **A valuation filter.** Nobody has asked for it. Add it the same way if "companies valued over $1B" comes up.

## Cost (OpenAI list prices; check the pricing page)

| Item | Estimate |
|---|---|
| Tagging, per story (measured: 80 real stories, 56.7k tokens in, 8.4k out, $0.036) | ~$0.00045 |
| 30-day backfill (~4,900 stories) | ~$2 one-off |
| Ongoing tagging (~4,900 stories a month) | ~$2 a month |
| Phase B answer (~15k tokens in, ~500 out, `gpt-4.1`) | ~$0.03 to $0.04 a question |

The same OpenAI key powers the digest's embeddings. If its credit runs out, the digest breaks, the way the Perplexity outages did. Turn on auto-recharge or a budget alert for it.

## Later

- **Bedrock Claude in the W Health account (873448587721).** Enable Anthropic model access, and add `bedrock:InvokeModel` to the instance role in `infra/iam.tf`. It also gives the ranker a second vendor.
- **`sector.db`.** Same enricher, pointed at the sector DB, once the daily DB works.
- ~~**Semantic search**~~ shipped on 1 October 2026 as `search_stories(about=...)` (`feat/subhanu-qa-semantic-search`), over the embeddings already in `stories.embedding`.
