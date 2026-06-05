# Signal Agent — Release Notes

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
