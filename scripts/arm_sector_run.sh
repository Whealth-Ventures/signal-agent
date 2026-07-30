#!/usr/bin/env bash
# Arm a ONE-OFF local sector-digest run at a target IST time today (detached).
# Survives terminal/session close via nohup. Logs to data/logs/.
#
#   scripts/arm_sector_run.sh 11:30            # real post at 11:30 IST today
#   scripts/arm_sector_run.sh 11:30 --test     # [TEST]-marked post at 11:30 IST
#   scripts/arm_sector_run.sh 11:30 --dry-run  # dry-run at 11:30 IST (no Slack)
#
# NOTE: the recurring cadence (1st & 15th) is a separate schedule — this only
# fires once, today. See docs/SECTOR_DIGEST_PLAN.md.
set -euo pipefail
cd "$(dirname "$0")/.."

TARGET="${1:-11:30}"
shift || true
EXTRA="$*"

SECS=$(python3 - "$TARGET" <<'PY'
import sys, datetime, zoneinfo
hh, mm = map(int, sys.argv[1].split(":"))
ist = zoneinfo.ZoneInfo("Asia/Kolkata")
now = datetime.datetime.now(ist)
t = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
if t < now:
    t += datetime.timedelta(days=1)
print(int((t - now).total_seconds()))
PY
)

mkdir -p data/logs
LOG="data/logs/sector_local_$(date -u +%Y%m%d_%H%M%S).log"
echo "Arming sector run in ${SECS}s (target ${TARGET} IST, args: '${EXTRA}')."
echo "Log: $LOG"

nohup bash -c "sleep ${SECS}; cd '$(pwd)'; ./scripts/run_sector_local.sh ${EXTRA} >> '${LOG}' 2>&1; echo '>> done '\$(date -u +%FT%TZ) >> '${LOG}'" >/dev/null 2>&1 &
echo "Armed (pid $!)."
echo "Watch it:  tail -f '${LOG}'"
echo "Cancel it: kill $!"
