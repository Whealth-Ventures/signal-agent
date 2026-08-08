#!/bin/bash
# Answer "is the box running what main says it should?" — in one command.
#
# This exists because guessing at it went wrong. On 2026-08-08 the box was
# checked 34 seconds after a merge, looked stale, and was declared evidence that
# Jenkins was dead. Jenkins was mid-build; it finished 2 minutes later. Two
# unnecessary manual deploys followed, which repointed latest.tgz at commits
# carrying no version bump and left the box's VERSION behind the release tag.
#
# The tell for "Jenkins ran" is the `chore: bump version to X [skip ci]` commit
# it pushes to main — NOT the state of the box a moment after merging.
#
# Usage: scripts/deploy-status.sh
set -uo pipefail

REGION=ap-south-1
IID=i-0803d6edfc54c1bb0
cd "$(dirname "$0")/.."

git fetch -q origin 2>/dev/null

HEAD_SHA=$(git rev-parse --short origin/main)
HEAD_SUBJ=$(git log -1 --format=%s origin/main)
HEAD_AUTHOR=$(git log -1 --format=%an origin/main)
RELEASED=$(git show origin/main:VERSION 2>/dev/null || echo "?")

echo "main    ${HEAD_SHA}  ${HEAD_SUBJ}"
echo "VERSION ${RELEASED}"
echo

# Has Jenkins processed the tip? Its bump commit lands ON TOP of the merge, so a
# tip authored by Jenkins means the pipeline finished.
if [ "$HEAD_AUTHOR" = "Jenkins CI" ]; then
  echo "jenkins ✅ built the tip (bump commit is HEAD)"
else
  AGE=$(( $(date -u +%s) - $(git log -1 --format=%ct origin/main) ))
  if [ "$AGE" -lt 240 ]; then
    echo "jenkins ⏳ tip is ${AGE}s old and not yet bumped — STILL BUILDING."
    echo "           Wait for the 'Jenkins CI' bump commit. Do NOT deploy by hand."
  else
    echo "jenkins ⚠️  tip is $((AGE / 60))m old with no bump commit on top."
    echo "           Check https://jenkins.xponentiate.com before concluding anything —"
    echo "           a [skip ci] commit or a non-bumping PR title also looks like this."
  fi
fi
echo

DEPLOYED=$(aws ssm send-command --region "$REGION" --instance-ids "$IID" \
  --document-name AWS-RunShellScript --comment "deploy-status" \
  --parameters '{"commands":["cat /opt/signal-agent/repo/VERSION 2>/dev/null || echo unknown"]}' \
  --query Command.CommandId --output text 2>/dev/null)
if [ -n "${DEPLOYED:-}" ]; then
  sleep 6
  BOX=$(aws ssm get-command-invocation --region "$REGION" --command-id "$DEPLOYED" \
        --instance-id "$IID" --query StandardOutputContent --output text 2>/dev/null | tr -d '[:space:]')
  echo "box     VERSION ${BOX:-unreachable}"
  if [ "${BOX:-}" = "$RELEASED" ]; then
    echo "        ✅ in sync with main"
  else
    echo "        ⚠️  DRIFT: box=${BOX:-?} released=${RELEASED}"
    echo "           Usually means someone deployed by hand. Let Jenkins redeploy"
    echo "           (run the job with BUMP=none) rather than patching it again —"
    echo "           latest.tgz must point at a commit that carries a version bump,"
    echo "           or rollback-by-version can't resolve it."
  fi
else
  echo "box     unreachable (no AWS creds?)"
fi
