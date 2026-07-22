#!/bin/bash
# Weekly Sector Agent run. Launched by signal-agent-sector.timer as the `signal`
# user; the systemd unit injects the runtime env (API keys, SLACK_CHANNEL_ID_SECTOR,
# etc.) via EnvironmentFile=/opt/signal-agent/shared/agent.env.
#
# Posts at 08:00 Asia/Kolkata: sector_main.py resolves DIGEST_POST_AT ("08:00")
# in the India timezone and holds to it, so the timer only needs to fire before.
#
# PUSH model (same as run-digest.sh): the box runs the inputs/ + prompts/ that
# shipped in the last deploy. Portfolio edits made in the admin UI take effect on
# the NEXT deploy. State (incl. data/db/sector.db) is captured by the daily
# run-digest.sh backup, which tars the whole data/ dir — no separate backup here.
set -euo pipefail

source /etc/signal-agent.env
export PATH=/usr/local/bin:/usr/bin:/bin:$PATH

REPO="$APP_DIR/repo"
cd "$REPO"

echo ">> running sector digest (post-at ${DIGEST_POST_AT:-immediate})"
.venv/bin/python src/sector_main.py --post-at "${DIGEST_POST_AT:-}"

echo ">> run-sector done"
