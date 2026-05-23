# Signal Agent — Project Brief

## What this is
A daily healthcare news digest agent for a VC firm (W Health Ventures / 2070 Health).
Runs autonomously, delivers top 5 stories to a Slack channel at 10am IST every day.

## Inputs (already in place)
- `inputs/keywords.xlsx` — 3 tabs (India, US, Cross-Cutting), ~2,280 keywords across buckets/sub-buckets
- `inputs/voices.xlsx` — 5 tabs (Overview, India voices, US voices, Newsletters, Company Pages), ~225 sources
- `content/` — firm's own published content, used as the "taste profile" for relevance scoring. Subfolders: `2070_health/`, `articles_blog/`, `interviews_podcasts/`, `linkedin/`, `news_press/`

## Architecture (4 layers — DO NOT collapse into one big LLM call)
1. **Query planner** — clusters keywords into ~30-40 thematic Perplexity queries/day (NOT one query per keyword)
2. **Fetchers** — Perplexity API for open web (sonar-pro, recency=day) + RSS pulls for named voices/newsletters
3. **Dedupe + score** — embedding similarity to dedupe stories; relevance score = cosine sim vs `content/` corpus + deterministic boosters
4. **Ranker** — Perplexity sonar-reasoning call for final top 5 (NOT Claude API — keeps vendor count low)

## Modules to build (in this order)
1. `src/config.py` — load .env, paths, constants
2. `src/query_planner.py` — read both Excels, emit list of query plans
3. `src/perplexity_client.py` — API wrapper with retries, rate limiting
4. `src/rss_fetcher.py` — pull RSS feeds for named newsletters
5. `src/content_indexer.py` — one-time: embed content/ folder into local Chroma DB
6. `src/storage.py` — SQLite schema (signals, stories, digests tables)
7. `src/scorer.py` — dedupe via embeddings + relevance scoring
8. `src/ranker.py` — Perplexity API call for final top 5
9. `src/slack_client.py` — Block Kit formatter + Slack Incoming Webhook poster
10. `src/main.py` — orchestrator (this is what cron triggers)

## Key constraints
- Total Perplexity calls per day must stay under 60
- Must NOT repeat stories sent in the last 7 days (check `digests` table)
- Validate every URL with HEAD request before it ships in the Slack post
- Log every API call to `data/logs/` with timestamps and costs
- All state in SQLite at `data/db/agent.db` — no external DB

## Tech stack (use exactly these — do not substitute)
- Python 3.11+
- httpx for API calls (NOT requests — need async later)
- openpyxl for Excel reading
- chromadb for vector store (local, no cloud)
- OpenAI text-embedding-3-small for embeddings (cheapest, good enough)
- SQLite + sqlite3 stdlib (no ORM)
- Slack Incoming Webhook (modern, provisioned via a Slack App — not the legacy custom integration) for delivery
- python-dotenv for config

## What "done" looks like for v1
Running `python src/main.py` end-to-end:
- Reads inputs
- Hits Perplexity ~30-40 times
- Pulls RSS from voices file
- Stores everything in SQLite
- Outputs top 5 to console AND posts to Slack
- Completes in under 15 minutes
- Costs under $3 per run

## What to defer to v2
- n8n integration (run locally first)
- Feedback loop (thumbs up/down via Slack reactions)
- Web dashboard
- Multi-channel support