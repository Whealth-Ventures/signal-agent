#!/bin/bash
# Daily digest run for ONE geo channel. Launched by the per-geo systemd timers
# (signal-agent.timer → india, signal-agent-us.timer → us) as the `signal` user;
# the systemd unit injects the runtime env (API keys, channel IDs,
# FEEDBACK_S3_BUCKET, etc.) via EnvironmentFile=/opt/signal-agent/shared/agent.env.
#
#   run-digest.sh india   → researches India+Global, posts to Signal Agent India
#   run-digest.sh us      → researches US+Global,   posts to Signal Agent US
#   run-digest.sh both    → legacy single-channel behaviour
#
# Both geo runs post at 08:00 *local* time: main.py resolves DIGEST_POST_AT
# ("08:00") in each geo's timezone (India → Asia/Kolkata, US → America/New_York),
# so the timers only need to fire before that instant and the app holds to it.
#
# PUSH model: the box runs the inputs/ + prompts/ that shipped in the last
# deploy. Tuning/prompt edits made in the admin UI (which commits them to the
# repo) take effect on the NEXT deploy. The box never talks to GitHub.
set -euo pipefail

GEO="${1:-both}"

source /etc/signal-agent.env
export PATH=/usr/local/bin:/usr/bin:/bin:$PATH

REPO="$APP_DIR/repo"
cd "$REPO"

echo ">> running digest geo=$GEO (post-at ${DIGEST_POST_AT:-immediate})"
.venv/bin/python src/main.py --geo "$GEO" --post-at "${DIGEST_POST_AT:-}"

# Backup runs ONCE per day, on the India (or legacy 'both') pass — not again on
# the later US pass. Non-fatal.
if [ "$GEO" != "us" ]; then
  # Nightly DR backup of the SQLite + Chroma state to S3.
  if [ -n "${FEEDBACK_BUCKET:-}" ]; then
    ts="$(date -u +%F)"
    tar -czf /tmp/sa-data.tgz -C "$REPO" data 2>/dev/null \
      && aws s3 cp /tmp/sa-data.tgz "s3://$FEEDBACK_BUCKET/state/data-$ts.tgz" --region "$REGION" \
      || echo "WARN: state backup skipped"
    rm -f /tmp/sa-data.tgz
  fi
fi

echo ">> run-digest done (geo=$GEO)"
