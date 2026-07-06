#!/bin/bash
# Build + restart signal-agent from the artifact already extracted at
# $APP_DIR/repo. PUSH model: Jenkins uploads a workspace tarball to S3 and the
# SSM command runs sa-fetch.sh (download + extract) then this script. The box
# never talks to GitHub.
#
# NOTE: no `set -x` — this handles secret JSON and must not echo it to the SSM
# command output / CloudWatch.
set -euo pipefail

source /etc/signal-agent.env
export PATH=/usr/local/bin:/usr/bin:/bin:$PATH

REPO="$APP_DIR/repo"
test -f "$REPO/requirements.txt" || { echo "ERROR: no artifact at $REPO — did sa-fetch.sh run?"; exit 1; }

echo ">> materializing env files from Secrets Manager"
AGENT_JSON="$(aws secretsmanager get-secret-value --region "$REGION" --secret-id "$AGENT_SECRET" --query SecretString --output text)"
ADMIN_JSON="$(aws secretsmanager get-secret-value --region "$REGION" --secret-id "$ADMIN_SECRET" --query SecretString --output text)"

umask 027
{
  echo "$AGENT_JSON" | jq -r 'to_entries[] | "\(.key)=" + (.value | tostring | @json)'
  echo "FEEDBACK_S3_BUCKET=\"$FEEDBACK_BUCKET\""
  echo "AWS_REGION=\"$REGION\""
  echo "AWS_DEFAULT_REGION=\"$REGION\""
  echo "DIGEST_POST_AT=\"$DIGEST_POST_AT\""
} > "$APP_DIR/shared/agent.env"

{
  echo "$ADMIN_JSON" | jq -r 'to_entries[] | "\(.key)=" + (.value | tostring | @json)'
  echo "FEEDBACK_S3_BUCKET=\"$FEEDBACK_BUCKET\""
  echo "AWS_REGION=\"$REGION\""
  echo "PORT=\"$ADMIN_PORT\""
  echo "NODE_ENV=\"production\""
} > "$REPO/admin/.env.production"
umask 022

echo ">> building agent venv"
runuser -u "$APP_USER" -- bash -c "
  set -euo pipefail
  cd '$REPO'
  test -d .venv || python3.11 -m venv .venv
  .venv/bin/pip install --quiet --upgrade pip
  .venv/bin/pip install --quiet -r requirements.txt
"

echo ">> building admin (next build)"
runuser -u "$APP_USER" -- bash -c "
  set -euo pipefail
  cd '$REPO/admin'
  npm ci --no-audit --no-fund
  npm run build
"

echo ">> installing systemd units"
install -m 644 "$REPO/deploy/signal-admin.service" /etc/systemd/system/signal-admin.service
install -m 644 "$REPO/deploy/signal-agent.service" /etc/systemd/system/signal-agent.service
install -m 644 "$REPO/deploy/signal-agent.timer"   /etc/systemd/system/signal-agent.timer
# Keep the schedule in sync with the Terraform-provided config.
sed -i "s#^OnCalendar=.*#OnCalendar=$DIGEST_ONCALENDAR#" /etc/systemd/system/signal-agent.timer

chown -R "$APP_USER:$APP_USER" "$APP_DIR"

systemctl daemon-reload
systemctl enable --now signal-agent.timer
systemctl restart signal-admin.service

echo ">> deploy OK"
systemctl --no-pager status signal-admin.service | head -5 || true
systemctl --no-pager list-timers signal-agent.timer || true
