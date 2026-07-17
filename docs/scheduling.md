# Scheduling — two channels, each at 08:00 local time

The digest runs on the **EC2 box** (Whealth AWS account, `ap-south-1`), driven by
**systemd timers**. There is no GitHub Actions cron and no external pinger in the
live path — the box owns its own schedule. (See `infra/README.md` for the full
deployment picture.)

The agent posts **two geo-scoped digests to two channels** from the same app:

| Geo run | Content | Channel | Timer | Fires (UTC) | Posts at |
|---------|---------|---------|-------|-------------|----------|
| `india` | India + Global | Signal Agent India | `signal-agent.timer` | `02:20` | 08:00 Asia/Kolkata |
| `us`    | US + Global    | Signal Agent US    | `signal-agent-us.timer` | `11:50` | 08:00 America/New_York |

`Global` stories (all AI-in-Healthcare, Hot-TAs, cross-cutting) and unclassified
RSS items go to **both** channels; India-only and US-only stories go to their own
channel. Each channel gets its own deep sweep (see `docs/EDITING.md` → geo depth),
so each is a full digest — not the old single digest split in half.

Getting a sharp 08:00 delivery has two parts: a punctual trigger, and an in-app
hold so delivery time doesn't depend on how long the build takes.

**The timers (punctual trigger).** Each timer fires its `.service`, which runs
`deploy/run-digest.sh <geo>` → `python src/main.py --geo <geo> --post-at "$DIGEST_POST_AT"`.
`Persistent=true` means a run missed while the box was down fires on next boot.

**Hold-until-08:00 (sharp delivery).** The timer fires *before* 08:00 local. The
pipeline fetches/scores/ranks (~3–9 min), then holds until exactly 08:00 **in that
geo's timezone** before posting — `main.py` resolves `DIGEST_POST_AT="08:00"` in
`Asia/Kolkata` (india) or `America/New_York` (us, DST-aware via `ZoneInfo`). The
US timer fires at a fixed 11:50 UTC while 08:00 ET moves an hour across DST, so
the hold can be up to ~70 min; the cap (`POST_AT_MAX_WAIT_S`) is 90 min to cover
the winter gap. Set `DIGEST_POST_AT` empty to disable the hold and post now.

A double-send is prevented by the pipeline's **per-channel idempotency guard**
(`has_sent_digest_for_date(..., slack_channel=...)`): if today's digest for *that
channel* already went out, a second run skips it — the India and US channels are
tracked independently. (Re-send deliberately with `--force`.)

## Changing the time

Both the timer and the hold are stamped at deploy time from the Terraform config,
so change them there — editing the box by hand gets overwritten on the next
deploy.

- **Fire time** → `OnCalendar` in `deploy/signal-agent.timer` (India, from
  Terraform `digest_oncalendar_utc`) and `deploy/signal-agent-us.timer` (US, from
  `digest_oncalendar_us_utc`). deploy.sh stamps both. Keep each ~10 min before the
  *earliest* target instant in UTC (US = 08:00 EDT = 12:00 UTC → fire 11:50).
- **Post time / hold** → `DIGEST_POST_AT` (Terraform → `agent.env`), a single
  `HH:MM` (`08:00`) that `main.py` resolves in each geo's own timezone.
- **US timezone** → `DIGEST_TZ_US` env (default `America/New_York`); India uses
  `digest_tz` from `inputs/tuning.xlsx` (`Asia/Kolkata`).

## How to verify it's working

On the box:

```bash
systemctl list-timers signal-agent.timer signal-agent-us.timer   # next fire + last run
journalctl -u signal-agent.service -n 200 --no-pager       # last India run's logs
journalctl -u signal-agent-us.service -n 200 --no-pager    # last US run's logs
```

`run-digest.sh` logs `>> running digest geo=<geo>` on start and
`>> run-digest done (geo=<geo>)` on success. The pipeline's own logs are under
`data/logs/` in the app's repo checkout (`/opt/signal-agent/repo`).

## Run it off-schedule

```bash
sudo systemctl start signal-agent.service      # India now, respecting the hold
sudo systemctl start signal-agent-us.service   # US now, respecting the hold
```

For a test post that ignores the hold and doesn't touch dedup history, run
`main.py --geo india --test` (or `--geo us --test`) directly as the `signal` user
(see HOW_IT_WORKS.md → "Running it yourself").

## The legacy GitHub Actions workflow

`.github/workflows/daily-digest.yml` still contains a `schedule:` cron and a
`repository_dispatch` trigger from the pre-AWS setup. It is **not** the live
scheduler — the box never talks to GitHub for scheduling, and the digest fires
from the systemd timer above. Treat the workflow as legacy; if you want a single
source of truth, disable its `schedule:` trigger.
