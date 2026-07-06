#!/bin/bash
# Daily digest run. Launched by signal-agent.timer as the `signal` user; the
# systemd unit injects the runtime env (API keys, FEEDBACK_S3_BUCKET, etc.) via
# EnvironmentFile=/opt/signal-agent/shared/agent.env.
#
# Before running it refreshes ONLY the data surface (inputs/ + prompts/) from
# GitHub, so tuning/prompt edits made in the admin UI take effect the next
# morning WITHOUT shipping unreviewed code. Code is deployed only via deploy.sh.
set -euo pipefail

source /etc/signal-agent.env
export PATH=/usr/local/bin:/usr/bin:/bin:$PATH
export GIT_ASKPASS=/usr/local/bin/sa-git-askpass.sh

REPO="$APP_DIR/repo"
cd "$REPO"

echo ">> refreshing inputs/ + prompts/ from origin/$BRANCH"
git fetch --prune origin "$BRANCH" || true
git checkout "origin/$BRANCH" -- inputs prompts || echo "WARN: data refresh skipped"

echo ">> running digest (post-at ${DIGEST_POST_AT:-immediate})"
.venv/bin/python src/main.py --post-at "${DIGEST_POST_AT:-}"

# Feedback loop — only when the S3 events store is configured. Non-fatal.
if [ -n "${FEEDBACK_S3_BUCKET:-}" ]; then
  echo ">> feedback: pull + aggregate"
  .venv/bin/python src/feedback_puller.py || echo "WARN: feedback_puller failed"
  .venv/bin/python src/feedback_aggregator.py || echo "WARN: feedback_aggregator failed"
fi

# Nightly DR backup of the SQLite + Chroma state to S3. Non-fatal.
if [ -n "${FEEDBACK_BUCKET:-}" ]; then
  ts="$(date -u +%F)"
  tar -czf /tmp/sa-data.tgz -C "$REPO" data 2>/dev/null \
    && aws s3 cp /tmp/sa-data.tgz "s3://$FEEDBACK_BUCKET/state/data-$ts.tgz" --region "$REGION" \
    || echo "WARN: state backup skipped"
  rm -f /tmp/sa-data.tgz
fi

echo ">> run-digest done"
