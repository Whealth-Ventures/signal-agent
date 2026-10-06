#!/usr/bin/env bash
# Run one AWS CodeBuild build on the commit checked out here, and wait for it.
#
#   scripts/codebuild-run.sh <project> [NAME=VALUE ...]
#
# Ships `git archive HEAD` (tracked files only: no .git, no .env, no workspace
# leftovers) to s3://$CODEBUILD_BUCKET/source/<project>/<sha>.zip, starts
# <project> with that zip as its source and each NAME=VALUE as a build env var,
# then polls until the build ends. Exits 0 only on SUCCEEDED; otherwise prints
# the tail of the build's log so the reason is in the Jenkins console, and
# exits 1.
#
# HEAD, not the remote branch: in the release pipeline HEAD is the version-bump
# commit, which is what the image must be built from.
#
# The Whealth projects, bucket and Jenkins permission live in the whealth-ci
# terraform stack (state: xponentiate-terraform-state/whealth-ci/). Same pattern
# as everhope (Everhope/jenkins_infra/HANDOFF.md).
set -euo pipefail

project=${1:?usage: codebuild-run.sh <project> [NAME=VALUE ...]}
shift
region=${AWS_REGION:-ap-south-1}
bucket=${CODEBUILD_BUCKET:-whealth-codebuild-873448587721}

sha=$(git rev-parse HEAD)
key="source/${project}/${sha}.zip"
zip=$(mktemp -t cbsrc.XXXXXX)
trap 'rm -f "$zip"' EXIT
git archive --format=zip -o "$zip" HEAD
aws s3 cp "$zip" "s3://${bucket}/${key}" --region "$region" --only-show-errors

# JSON, not the CLI's name=..,value=.. shorthand: a value with a comma in it
# would be split silently by the shorthand parser.
envs='[]'
for kv in "$@"; do
  envs=$(jq -c --arg n "${kv%%=*}" --arg v "${kv#*=}" '. + [{name: $n, value: $v, type: "PLAINTEXT"}]' <<<"$envs")
done

id=$(aws codebuild start-build --region "$region" --project-name "$project" \
  --source-location-override "${bucket}/${key}" \
  --environment-variables-override "$envs" \
  --query build.id --output text)
echo "CodeBuild ${id} started on ${sha:0:12}"
echo "  https://${region}.console.aws.amazon.com/codesuite/codebuild/projects/${project}/build/${id/:/%3A}/?region=${region}"

last=''
while :; do
  read -r status phase < <(aws codebuild batch-get-builds --region "$region" --ids "$id" \
    --query 'builds[0].[buildStatus,currentPhase]' --output text)
  [ "$phase" != "$last" ] && echo "  phase: ${phase}" && last=$phase
  [ "$status" != IN_PROGRESS ] && break
  sleep 10
done

if [ "$status" = SUCCEEDED ]; then
  echo "CodeBuild ${id}: SUCCEEDED"
  exit 0
fi

echo "CodeBuild ${id}: ${status}" >&2
read -r group stream < <(aws codebuild batch-get-builds --region "$region" --ids "$id" \
  --query 'builds[0].logs.[groupName,streamName]' --output text)
if [ -n "$stream" ] && [ "$stream" != None ]; then
  echo "--- last 80 log lines (${group}) ---" >&2
  aws logs get-log-events --region "$region" --log-group-name "$group" --log-stream-name "$stream" \
    --limit 80 --query 'events[].message' --output text | tr '\t' '\n' >&2 || true
fi
exit 1
