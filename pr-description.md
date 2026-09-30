# [patch] PROD Release: Q&A bot filters by deal size, converted to USD in code

**Deal-size questions ("rounds up to ₹200 Cr") now filter in the search, not in the model's head.** No ticket. This came from the first question a teammate asked on 30 September 2026. The bot listed Marengo Asia Hospitals' $40M round (about ₹350 Cr) under "up to ₹200 Cr".

## What changes

- **`search_stories` takes `min_amount` / `max_amount`** in [src/qa.py](src/qa.py).
  - Each is `{value, unit, currency}`, as the user said it, e.g. `{200, crore, INR}`.
  - Code converts it with the existing `enricher.parse_money`, then filters on `amount_usd`. The model never does currency maths.
  - A bound excludes stories with no stated amount, and stored amounts outside $10k to $1T (a units slip).
  - A malformed bound is ignored, like every other bad filter value.
- **Prompt** ([prompts/qa_system.md](prompts/qa_system.md)): pass the limit as said, don't convert it, and mention in one line that undisclosed deals are left out.
- **Closing offers:** the style filter also drops a "let me know" / "if you want" sentence that ends the last paragraph. Before, it only caught one on its own line.
- Stale docstring on `search_stories` updated (merging is embedding-based since PR #20).

## Tested

- `tests/test_qa.py`: ₹200 Cr max drops a $300M deal; min drops no-amount stories; a USD range; a malformed bound; mid-paragraph closing offer.
- Full suite: 358 passed. The 2 `test_config` failures only happen locally, because there's no `.env`.
- Live against a local copy of the archive, with Gaurav's exact question: the search sent `max_amount {200, crore, INR}`. That returned 7 deals from ₹7.1 Cr to ₹200 Cr, with no Marengo and the note about undisclosed deals.
- "Indian funding rounds this month above $50M" correctly returned none. The only bigger items are a filing and two fund raises.

## Deploy

No new secrets, dependencies or migrations. Jenkins restarts the bot service on deploy.
