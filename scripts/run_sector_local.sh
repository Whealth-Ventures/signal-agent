#!/usr/bin/env bash
# Local launcher for the bi-weekly sector digest.
#   scripts/run_sector_local.sh --pdf           # render ONE combined PDF (no Slack) → data/exports/
#   scripts/run_sector_local.sh --pdf --pdf-out ~/Desktop/sectors.pdf
#   scripts/run_sector_local.sh                 # all sectors, post to Slack
#   scripts/run_sector_local.sh --dry-run       # no delivery; Slack blocks written to data/logs/
#   scripts/run_sector_local.sh --sector diabetes --pdf
#
# --pdf needs OPENAI + PERPLEXITY (+ ANTHROPIC) keys in .env, but NO Slack config.
set -euo pipefail
cd "$(dirname "$0")/.."

if [ ! -f .env ]; then
  echo "ERROR: .env not found. Copy .env.example to .env and fill in keys." >&2
  exit 2
fi

PY="${PYTHON:-python3}"
echo ">> sector digest run $(date -u +%FT%TZ)  args: $*"
exec "$PY" src/main.py --mode sector "$@"
