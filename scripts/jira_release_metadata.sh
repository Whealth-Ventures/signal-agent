#!/usr/bin/env bash
#
# WH-355 — fill a release's Jira version metadata and attribute every ticket in it.
#
# WH-313 made Jenkins create the version. It stopped there: four fields were left
# blank and only the ticket named in the PR *title* got a Fix Version, so the ones
# named in the PR body shipped with none (WH-330 shipped in everhope_nextjs PR #1722
# and carried an empty fixVersions for days).
#
# Those extra tickets are declared by the author on a `Tickets:` line. The first cut
# of this script scanned the whole PR body instead and mis-attributed six tickets on
# its own first release — see the note above the trailer parsing below.
#
# Of the four blank fields, three are writable and are filled here. The fourth —
# the big "release notes" rich-text area on the release page — has NO API. That is
# not a permissions problem and not worth another look:
#   * the Version schema has no release-notes property at all, and
#   * Atlassian's own OpenAPI spec says of VersionRelatedWork.relatedWorkId:
#     "For the native release note related work item, this will be null, and Rest
#     API does not support updating it."
#   * feature request ECO-447, open and unanswered since Sept 2024.
# It stays a paste-by-hand field. Do not build on an undocumented endpoint.
#
# Everything here is bookkeeping and runs AFTER a successful deploy. The caller
# wraps it so a failure can never fail the build; inside, every write is reported
# with its HTTP code rather than trusted.
#
# Required env:
#   JIRA_BASE_URL JIRA_EMAIL JIRA_API_TOKEN   Jira credentials
#   FIX_VERSION                               e.g. everhope_nextjs-1.6.1
#   GIT_SUBJECT                               HEAD commit subject
# Optional env:
#   PR_JSON        path to the cached merge-PR response (default .jira-pr.json)
#   PR_TITLE       used when PR_JSON is absent
#   IN_DEV_STATUS  status a ticket enters when work starts (default "In Dev")
#   DRY_RUN=1      resolve and print, write nothing

set -euo pipefail

API="${JIRA_BASE_URL}/rest/api/3"
PR_JSON="${PR_JSON:-.jira-pr.json}"
IN_DEV_STATUS="${IN_DEV_STATUS:-In Dev}"
DRY_RUN="${DRY_RUN:-0}"

jira() { curl -sS --max-time 30 -u "$JIRA_EMAIL:$JIRA_API_TOKEN" -H 'Content-Type: application/json' "$@"; }

# ---------------------------------------------------------------------------
# 1. Where the ticket numbers are
#
# The PR body is the point of this ticket: a release bundles several tickets and
# only one of them reaches the title. The body is already on disk — the bump
# resolver saved the whole PR response rather than throwing everything but .title
# away — so this costs no extra API call.
#
# The GitHub *Release* page lists the same PRs, but it is written by a workflow on
# `on: create` a few seconds AFTER Jenkins pushes the tag, and this runs
# immediately after that push. Reading it would be a race against our own pipeline.
# ---------------------------------------------------------------------------
if [ -f "$PR_JSON" ]; then
  title=$(jq -r '.title // empty' "$PR_JSON" 2>/dev/null || true)
  body=$(jq -r '.body // empty' "$PR_JSON" 2>/dev/null || true)
else
  title="${PR_TITLE:-}"
  body=""
fi

# A whole key only: in "WH-123/124" the 124 is not a ticket, and \b...\b already
# refuses it because a key must start with a letter.
keys_in() { printf '%s\n' "$1" | grep -oE '\b[A-Z][A-Z0-9]*-[0-9]+\b' || true; }

# Extra keys come ONLY from an explicit `Tickets:` line, never from body prose.
#
# The first release under this script proved why prose cannot be used. Scanning the
# whole body attributed SIX tickets that did not ship to everhope_nextjs-1.8.1,
# everhope-nutrition-1.3.1 and ai-interviewer-1.4.1 — among them WH-123, which
# appeared nowhere but inside the sentence explaining that "WH-123/124" counts only
# the first key. A body that MENTIONS a ticket (an illustration, a reference to
# earlier work, the cause being fixed) is indistinguishable from one that SHIPPED
# it, so the author states the list explicitly or it is not read at all.
# Two rules, each learned from a release that got this wrong:
#
#  1. Fenced code blocks are quoting, not instruction. A PR that DOCUMENTS this
#     convention necessarily contains a `Tickets:` example — and 1.8.2/1.3.2
#     obeyed exactly that, attributing WH-330/331/332 from the sample block in the
#     PR body that introduced the trailer.
#  2. Last one wins, like a git trailer. The real list is the one at the end; an
#     earlier occurrence is illustration.
#
# Same failure every time: text written to DESCRIBE a mechanism gets read AS the
# mechanism. Anything parsed out of human prose has to assume the prose is talking
# about the parser.
body=$(printf '%s\n' "$body" | awk '/^[[:space:]]*```/ { fence = !fence; next } !fence')
trailer=$(printf '%s\n' "$body" | grep -iE '^[[:space:]]*Tickets[[:space:]]*:' | tail -1 || true)

TITLE_KEYS=$(keys_in "$title $GIT_SUBJECT" | awk '!seen[$0]++')
TRAILER_KEYS=$(keys_in "$trailer" | awk '!seen[$0]++')
ALL_KEYS=$(printf '%s\n%s\n' "$TITLE_KEYS" "$TRAILER_KEYS" | grep -v '^[[:space:]]*$' | awk '!seen[$0]++' || true)

if [ -z "$ALL_KEYS" ]; then
  echo "Jira: no ticket key in the PR title, commit subject or a 'Tickets:' line — nothing to tag."
  exit 0
fi

if [ -z "$TRAILER_KEYS" ]; then
  echo "Jira: no 'Tickets:' line in the PR body — attributing the title's ticket only."
  echo "      Add e.g. 'Tickets: WH-330, WH-331' to the PR body to attribute the rest."
fi

# One project per release. Derived from the title key so a passing mention of
# another project's ticket in the body cannot drag the version into it — and so a
# legacy string like "EH-WEB-1" is filtered out rather than 404-ing on every write.
#
# awk NR==1, not `head -1`: head exits at the first line, upstream takes SIGPIPE,
# and pipefail then fails the pipeline for succeeding.
FIRST_KEY=$(printf '%s\n' "${TITLE_KEYS:-$ALL_KEYS}" | awk 'NR==1')
JIRA_PROJECT="${FIRST_KEY%%-*}"
ALL_KEYS=$(printf '%s\n' "$ALL_KEYS" | grep -E "^${JIRA_PROJECT}-[0-9]+$" || true)

echo "Jira: $FIX_VERSION -> $(printf '%s' "$ALL_KEYS" | tr '\n' ' ')"

# ---------------------------------------------------------------------------
# 2. Read each ticket once — summary, reporter and changelog in a single GET.
# ---------------------------------------------------------------------------
descr_parts=""; start_date=""; created_min=""; driver=""

for k in $ALL_KEYS; do
  issue=$(jira "$API/issue/$k?expand=changelog&fields=summary,reporter,created" || true)
  [ -z "$issue" ] && { echo "  WARN: could not read $k" >&2; continue; }

  # Every assignment below is a full `if`, not `[ x ] && y=z`: under `set -e` that
  # form aborts the script whenever the test is false, which here is the ordinary
  # case (a ticket with no In Dev transition, say) rather than an error.
  summary=$(printf '%s' "$issue" | jq -r '.fields.summary // empty')
  if [ -n "$summary" ]; then
    descr_parts="${descr_parts}${descr_parts:+ + }${summary} (${k})"
  fi

  # Start date is the day work actually began, not the ship date: the FIRST time
  # any ticket in this release entered In Dev.
  #
  # ponytail: the embedded changelog is capped, so a ticket with a very long
  # history could have its In Dev entry truncated away and fall through to the
  # created-date branch below — earlier than the truth, never later. Swap to the
  # paginated /issue/{k}/changelog endpoint if that ever actually bites.
  d=$(printf '%s' "$issue" | jq -r --arg s "$IN_DEV_STATUS" '
        [.changelog.histories[]?
         | select(any(.items[]?; .field=="status" and .toString==$s))
         | .created[:10]] | min // empty')
  if [ -n "$d" ] && { [ -z "$start_date" ] || [ "$d" \< "$start_date" ]; }; then
    start_date="$d"
  fi

  c=$(printf '%s' "$issue" | jq -r '.fields.created[:10] // empty')
  if [ -n "$c" ] && { [ -z "$created_min" ] || [ "$c" \< "$created_min" ]; }; then
    created_min="$c"
  fi

  # The driver is the person who reported the ticket named in the PR title —
  # the one accountable for this release, not for whatever else rode along.
  if [ "$k" = "$FIRST_KEY" ]; then
    driver=$(printf '%s' "$issue" | jq -r '.fields.reporter.accountId // empty')
  fi
done

if [ -z "$start_date" ] && [ -n "$created_min" ]; then
  start_date="$created_min"
  echo "  no In Dev transition on any ticket — start date falls back to the earliest created ($start_date)"
fi

# Jira renders the version description as one unformatted blob, so it stays a
# single line. Cap it: the field takes 16KB but nothing readable needs that.
description="$descr_parts"
if [ ${#description} -gt 250 ]; then
  description="${description:0:247}..."
fi

# ---------------------------------------------------------------------------
# 3. The version. Never created under any name but the tag's, never renamed,
#    never marked released — shipped is not the same as accepted.
# ---------------------------------------------------------------------------
REL_DATE=$(date -u +%Y-%m-%d)
meta=$(jq -nc --arg d "$description" --arg s "$start_date" --arg dr "$driver" \
        '{description:$d, startDate:$s, driver:$dr} | with_entries(select(.value != ""))')

if [ "$DRY_RUN" = "1" ]; then
  echo "DRY_RUN keys: $(printf '%s' "$ALL_KEYS" | tr '\n' ' ')"
  echo "DRY_RUN meta: $meta"
  exit 0
fi

vid=$(jira "$API/project/$JIRA_PROJECT/versions" \
      | jq -r --arg n "$FIX_VERSION" 'map(select(.name==$n))[0].id // empty')

if [ -z "$vid" ]; then
  # Unreleased, with today as the planned date. CI knows the code deployed; it does
  # not know the release was accepted, so releasing it stays a human decision. The
  # date is set because a blank one is what the jira-alerts rule flags, and an
  # unreleased version past its date reads as overdue — the nudge that makes the
  # sign-off happen.
  vid=$(jira -X POST -d "$(jq -nc --arg n "$FIX_VERSION" --arg p "$JIRA_PROJECT" --arg d "$REL_DATE" \
          --argjson m "$meta" '$m + {name:$n, project:$p, released:false, releaseDate:$d}')" \
        "$API/version" | jq -r '.id // empty')
  if [ -n "$vid" ]; then
    echo "  version created unreleased, dated $REL_DATE (id=$vid) — $meta"
  fi
else
  # The version already exists: a re-run, a second repo, or one a human made. Fill
  # ONLY what is still blank. Overwriting here would quietly undo the hand-written
  # values this ticket exists to stop people typing.
  echo "  version exists (id=$vid)"
  cur=$(jira "$API/version/$vid?expand=driver" || true)
  if ! printf '%s' "$cur" | jq -e . >/dev/null 2>&1; then cur='{}'; fi
  fill=$(jq -nc --argjson m "$meta" --argjson c "$cur" \
          '$m | with_entries(select(($c[.key] // "") == ""))')
  if [ "$fill" != "{}" ]; then
    code=$(jira -o /dev/null -w '%{http_code}' -X PUT -d "$fill" "$API/version/$vid")
    echo "  filled blank fields $fill (HTTP $code)"
  else
    echo "  all three fields already set — left alone"
  fi
fi

if [ -z "$vid" ]; then
  echo "WARN: Jira version '$FIX_VERSION' is missing and could not be created." >&2
  echo "      The account needs Administer Projects on $JIRA_PROJECT. Skipping." >&2
  exit 0
fi

# ---------------------------------------------------------------------------
# 4. Fix Version onto every ticket. `add`, never a replacing set: one ticket can
#    ship from two repos and has to carry both versions.
# ---------------------------------------------------------------------------
for k in $ALL_KEYS; do
  code=$(jira -o /dev/null -w '%{http_code}' -X PUT \
           -d "$(jq -nc --arg id "$vid" '{update:{fixVersions:[{add:{id:$id}}]}}')" \
           "$API/issue/$k")
  echo "  $k -> $FIX_VERSION (HTTP $code)"
done
