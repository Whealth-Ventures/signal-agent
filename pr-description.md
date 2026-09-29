# [major] PROD Release: Slack Q&A bot that answers from the news archive

**Phase B of the news archive: `@signal_agent <question>` or a DM gets an answer in a thread, searched from the labelled archive.** No ticket. The setup was done on 29 September 2026 ([docs/2026-09-29-qa-bot-setup.md](docs/2026-09-29-qa-bot-setup.md)), and the plan is in [docs/2026-09-25-news-archive-plan.md](docs/2026-09-25-news-archive-plan.md).

## What changes

- **New `src/qa.py`**, the brain, with no Slack code:
  - Two fixed tools over `agent.db`, opened read-only. The model never writes SQL.
  - `search_stories` filters by category, events, geo, company, dates and minimum magnitude, plus 1 to 3 keywords matched against title, summaries and body. It sorts by importance (magnitude, then deal size) or by recency.
  - `get_story` returns one story's labels and up to 6,000 characters of the article.
  - Tool loop on OpenAI `gpt-4.1` (`config.QA_MODEL`), up to 5 rounds.
- **New `src/bot.py`**, the Slack side, adapted from salesforce-sage:
  - Socket Mode, handling `app_mention` and DMs.
  - Event-id dedupe, with the thread's history passed in as chat turns.
  - A placeholder message that the answer replaces, then markdown converted to Slack mrkdwn, split into 3,800-character chunks.
- **New `deploy/signal-agent-bot.service`.** `deploy.sh` installs it, enables it, and restarts it on every deploy.
  - It exits 0 without `SLACK_APP_TOKEN`, and `Restart=on-failure` means an unconfigured box just idles.
- **New prompt `prompts/qa_system.md`.** The date, the archive's coverage and each category's event values are added in code.
- **Config:** `SLACK_APP_TOKEN` (optional), `QA_MODEL`, `QA_SYSTEM_PROMPT`.
- **Dependency:** `slack-bolt>=1.21`.
- **Enricher label tweaks, from Slack testing:** `ipo_filing` means filings to list shares only, and an `amount_usd` under $10,000 is rejected. These apply to new labels; existing ones are handled at read time.

## Money in the article's own currency, compared in USD

- **The labeller now copies an amount exactly as written**, as `{value, unit, currency}`. For example, "₹4,800 crore" becomes `{4800, crore, INR}`. It doesn't convert anything.
- **`enricher.parse_money` does the conversion in code.** It uses a units table (lakh, crore, million, billion…) and a fixed rates table (`PER_USD`, 13 currencies, approximate). It stores `amount_usd` for ranking and `amount_text` for display. Valuations work the same way.
- **Answers show both:** `₹4,800 Cr (~$545M)`, `€1.5B (~$1.74B)`. Dollars appear in one form (`$3.35B`, not `$3,350M`).
- **Why this changed:** when the model did the conversion, it left AGS Health's "₹4,800 crore" with no amount and stored Electra's $350M as `350`. Re-labelling 289 deal stories locally with the new format gave 80 dollar amounts, 33 rupee, 5 euro, 1 yuan and 1 yen, and Electra's $350M is right.
- **Labels made before this change** only have `amount_usd`. They display as `~$446M`.

## Guarantees, each added after a live test showed it was needed

| Problem seen live | Fix |
|---|---|
| The IPO answer missed Electra, Eclat and others (it searched `ipo` only), then said "no other IPOs" | `events` takes a list; the prompt maps IPO questions to `["ipo", "ipo_filing"]` and forbids "no other X" without a broad search |
| ADARx and AbbVie's tavapadon listed twice, despite being told to merge | The search merges duplicates itself, by greedy leader selection as in the digest's `collapse_near_duplicates`: same category, same event family, published within 10 days, **and** story-embedding cosine ≥ 0.70 → one result with `more_links` |
| A follow-up ("which are Indian?") answered from the thread and said "none", but Eclat is Indian | `tool_choice="required"` on round 1, so every question searches |
| A follow-up lost its link | Links already in the thread's history are trusted |
| "Hospital deals in July" didn't say the archive starts 14 September | `archive_covers_from` is on every search result |
| An em-dash and ISO dates in answers | Em-dashes replaced in code; the prompt asks for "14 September" dates |
| **Slack test:** Electra listed twice, once as "sets terms" and once as "prices" | An IPO filing and the IPO itself merge as one event (`_EVENT_FAMILY`) |
| **Slack test:** Electra's $350M stored as `350` | Amounts under $10,000 are units slips: dropped when read, and rejected by the enricher from now on |
| **Slack test:** "Which are Indian?" listed a Morepen drug (ANDA) filing | The prompt says never pad with loosely related results; the enricher's `ipo_filing` now excludes drug and regulatory filings |
| **Slack test:** answers ended with "Let me know if…" despite the prompt | A closing offer on the last line is stripped in code |
| **Re-label test:** Manipal's debt repayment "using IPO proceeds" ranked as the #1 IPO | `ipo` now means the company's own IPO pricing, opening or listing, not a later use of IPO money; re-labelled, it's `other_healthcare` |

- **Every URL in an answer must have come from a tool result, or from the thread's history.** Anything else is unlinked (`keep_known_links`).
- **Each question is logged to `data/logs/qa_<date>.jsonl`:** question, the searches with their filters, tokens, cost, latency and any error.
- **An OpenAI failure gets a plain message in the thread.** Out of credits says so specifically, and never a traceback.

## Changes after review

- 🔴 **Invented links in any form are unlinked.** That covers `[text](url)`, `<url|text>` (the Slack form the bot's own history is in), `<url>`, `(url)` and bare URLs. A final pass checks every URL left in the text. **Only the bot's own earlier answers seed the allowed set.** Links people post in the thread aren't citable.
- 🔴 **Merging is decided by embeddings, gated by the labels and dates.** The old key (first word of the lead company) merged General Atlantic with General Catalyst, and two unrelated Lilly approvals.
  - Measured on prod: real duplicates score 0.65 to 0.89 (ADARx across outlets 0.84 and 0.89, Electra's filing and pricing 0.73). Different news reaches 0.72, but in a different category.
  - So a merge needs the same category **and** event family, within 10 days, **and** cosine ≥ 0.70. A story with no stored embedding is never merged.
  - Checked on real embeddings: ADARx and Electra each appear once.
- **Money bounds:** NaN is rejected, and so is any total over $1 trillion (a double-scaled slip). `get_story` returns only the cleaned money fields, never the raw `amount_usd 350`.
- **`$999.6M` displays as `$1B`,** not `$1e+03M`.
- **Date filters are parsed, not string-compared.** `2026/09/15` works, and anything unparseable is ignored, as the docstring promised.
- **A rejected Slack token exits cleanly** instead of restart-looping. The unit also has a start limit (5 tries in 10 minutes), and `deploy.sh` clears it and only warns if the bot won't restart. The bot can never fail a deploy.
- **New `enricher.py --retag`:** it re-labels already-labelled stories in place, with no fetch. Old labels stay until each new one is written, so nothing drops out of the bot's search. There's no 30-day retry window to fall out of.

## Safety

- **The digest is untouched.** The bot is its own process and only reads `agent.db`, in `mode=ro`.
- **The digest path gains one import-time change:** `config.py` loads `prompts/qa_system.md`, which ships in this PR.
- **Slack app changes are done:** Socket Mode, the two events, the four scopes, and the reinstall. The bot token is unchanged (same fingerprint on the box). The three leftover events from the removed feedback loop were deleted.

## Tests

- `tests/test_qa.py`, 19 tests, plus one enricher test for the $10,000 amount floor:
  - search filters and ordering, duplicate merging, non-healthcare only when asked, bad values ignored
  - the tool loop with a fake OpenAI client
  - invented links unlinked, and thread links reused
  - round 1 forced to search, and coverage stamped on results
  - em-dashes replaced
  - Slack handling: history as chat turns, the placeholder replaced, the billing message, help on an empty mention, and a clean exit when unconfigured
- **Live, on a local copy of the prod archive (rebuilt from Neon, 2,565 labelled stories), with real OpenAI:**
  - biggest IPOs this month → ADARx $535M, Electra $325M, Eclat $300M, and a $150M SPAC
  - the follow-up "which are Indian?" searched again → Eclat
  - FDA approvals in the last two weeks → 5 items, duplicates merged
  - July hospital deals → says the archive starts 14 September
  - about 1 cent a question
- **Tested in Slack on 29 September** in `#signal-agent-bot-test` and a DM: 6 questions, about 4 seconds and 1 to 2 cents each, no errors. The four fixes in the table's last rows came from that session.
- `parse_money` and `money_display`: rupees, dollars, other currencies, units slips, unknown units or currencies, and labels from before the change.
- **Review tests:**
  - invented links in all five forms
  - links people post aren't citable
  - one event from several outlets merges, including a filing, while different companies sharing a first word, one company's different events, and a same-event story 25 days later stay apart
  - NaN and $1T+ amounts rejected, and `get_story` never returns raw money
  - `$999.6M` shows as `$1B`, and dates are parsed
  - a rejected token exits cleanly
  - `--retag` re-labels in place without fetching
- The full suite passes locally (357 passed, 3 skipped). The two `test_config` env checks fail only because this checkout has no `.env`.

## Before merge

- **`SLACK_APP_TOKEN` must be in `signal-agent/prod/agent-env`, with no quote marks.** The deploy writes it into the box's env file.
- **Stop any local copy of the bot before the deploy.** Two Socket Mode connections split the messages between them.

## After merge

- **Re-label the archive with the new rules**, one time: about 2,560 stories, roughly $1.20. It applies the money format, the `ipo` and `ipo_filing` definitions, and the amount bounds. It needs a go-ahead, since it writes to the prod DB:

  ```bash
  sudo systemd-run --unit=signal-enrich-retag --collect --uid=signal --gid=signal \
    -p EnvironmentFile=/opt/signal-agent/shared/agent.env -p WorkingDirectory=/opt/signal-agent/repo \
    /bin/bash -c '.venv/bin/python src/enricher.py --retag --days 30 --geo india; .venv/bin/python src/neon_sync.py'
  ```

  `--retag` re-labels in place, so the bot keeps answering throughout. `--days` must reach back to 14 September, so use `--days 45` if this runs after 14 October.

**Need from you:** review and merge.
