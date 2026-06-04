# Signal Agent — Release Notes

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
