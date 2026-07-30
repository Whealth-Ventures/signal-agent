#!/bin/bash
# Bi-weekly sector digest → posts 7 Block Kit messages (one per sector) to the
# #sector-agent Slack channel. Launched by signal-agent-sector.timer (every
# Tuesday 05:30 UTC = 11:00 IST) as the `signal` user; the systemd unit injects
# the runtime env (API keys, SLACK_CHANNEL_ID_SECTOR, etc.) via
# EnvironmentFile=/opt/signal-agent/shared/agent.env.
#
# The timer fires WEEKLY; `--min-days-between 13` makes the digest FORTNIGHTLY —
# the run no-ops unless ~2 weeks have passed since the last sector send, and
# self-heals a missed cycle. Pass --force (or --min-days-between 0) for an
# ad-hoc send; --dry-run / --test for a safe trial.
#
# PUSH model: the box runs the inputs/ + prompts/ that shipped in the last
# deploy; it never talks to GitHub.
set -euo pipefail

source /etc/signal-agent.env
export PATH=/usr/local/bin:/usr/bin:/bin:$PATH

REPO="$APP_DIR/repo"
cd "$REPO"

echo ">> running sector digest (fortnightly guard: 13d)"
.venv/bin/python src/main.py --mode sector --min-days-between 13 "$@"
echo ">> run-sector done"
