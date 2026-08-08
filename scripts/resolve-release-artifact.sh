#!/usr/bin/env bash
# Resolve a release VERSION to the one S3 artifact key that carries it.
#
# The ECR sibling of this script is scripts/resolve-release-image.sh. This repo
# deploys a source tarball rather than a container image, so the version index is
# S3: artifacts/<service>/<version>-<sha>.tgz, written by every release.
#
#   usage: resolve-release-artifact.sh <bucket> <service> <version>
#   stdout: the resolved S3 key (nothing else — it is meant for $(...))
#
# The operator types 1.0.2 — never a key, never a sha. Exactly one match is
# required: zero means that version was never released (or its artifact aged out
# of the bucket's lifecycle rules) and the rollback must not proceed; more than one
# means two artifacts claim one version, which a human needs to look at.
#
# The trailing "-" in the prefix is what stops 1.0.1 from also matching
# 1.0.10-<sha>. Do not "simplify" it away.
set -euo pipefail

BUCKET="${1:?usage: resolve-release-artifact.sh <bucket> <service> <version>}"
SERVICE="${2:?usage: resolve-release-artifact.sh <bucket> <service> <version>}"
VERSION="${3:?usage: resolve-release-artifact.sh <bucket> <service> <version>}"

VERSION="${VERSION#v}"

case "$VERSION" in
  [0-9]*.[0-9]*.[0-9]*) : ;;
  *) echo "ERROR: '$VERSION' is not a semver version (expected e.g. 1.0.2)" >&2; exit 2 ;;
esac

PREFIX="artifacts/${SERVICE}/"

# latest.tgz is a moving pointer, not a release — it must never be a rollback
# target, or "roll back" would mean "redeploy whatever shipped last".
ALL=$(aws s3api list-objects-v2 --bucket "$BUCKET" --prefix "$PREFIX" \
        --query 'Contents[].Key' --output text 2>/dev/null | tr '\t' '\n' | grep -v '/latest\.tgz$' || true)

MATCHES=$(printf '%s\n' "$ALL" | grep -E "^${PREFIX}${VERSION}-[0-9a-f]+\.tgz$" || true)
COUNT=$(printf '%s' "$MATCHES" | grep -c . || true)

if [ "$COUNT" -eq 0 ]; then
  echo "ERROR: no artifact in s3://$BUCKET/$PREFIX carries version $VERSION." >&2
  echo "Versions available to roll back to:" >&2
  printf '%s\n' "$ALL" | sed -n "s|^${PREFIX}\([0-9][0-9.]*\)-[0-9a-f]*\.tgz$|  \1|p" | sort -u >&2
  exit 1
fi

if [ "$COUNT" -gt 1 ]; then
  echo "ERROR: version $VERSION matches $COUNT artifacts:" >&2
  printf '%s\n' "$MATCHES" >&2
  echo "One version must identify one artifact. Investigate before deploying." >&2
  exit 1
fi

printf '%s\n' "$MATCHES"
