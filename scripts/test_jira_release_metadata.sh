#!/usr/bin/env bash
#
# WH-355 — runs scripts/jira_release_metadata.sh for real against a stubbed Jira.
#
# `curl` is shadowed by a shim on PATH, so this needs no network, no Jira token and
# no fixtures beyond the two files it writes itself. The point is the paths that
# never run in the happy case and therefore rot: a body-only ticket key, a checklist
# that must contribute nothing, a version that already exists with a field a human
# filled in.
#
# The PR fixture is real — everhope_nextjs #1722, whose title names WH-328 while the
# body names WH-330/331/332. That release is the reason this ticket exists: WH-330
# shipped in it and carried no Fix Version at all.

set -euo pipefail
cd "$(dirname "$0")/.."
SCRIPT="$PWD/scripts/jira_release_metadata.sh"
TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT
pass=0; fail=0

check() { # check <name> <haystack> <needle>
  if printf '%s' "$2" | grep -qF -- "$3"; then
    pass=$((pass + 1)); echo "  ok   $1"
  else
    fail=$((fail + 1)); echo "  FAIL $1"; echo "       wanted: $3"; echo "       got:    $2"
  fi
}
refute() {
  if printf '%s' "$2" | grep -qF -- "$3"; then
    fail=$((fail + 1)); echo "  FAIL $1"; echo "       should not contain: $3"
  else
    pass=$((pass + 1)); echo "  ok   $1"
  fi
}

# Prose deliberately name-drops tickets that did NOT ship — WH-901 as an illustration,
# WH-902 as prior work, WH-999 in a checklist. Only the `Tickets:` line counts.
# This is the everhope_nextjs-1.8.1 regression, frozen: that release attributed six
# such mentions, including one that existed only inside a sentence about parsing.
cat > "$TMP/pr.json" <<'JSON'
{
  "title": "[minor] PROD Release (WH-328): full-journey attribution",
  "body": "Promotes the journey epic to production. Builds on WH-902, and note that in\n\"WH-901/902\" only the first is a key. Legacy line EH-WEB-1 must be ignored.\n\nDeclare extra tickets like this:\n\n```\nTickets: WH-801, WH-802\n```\n\n## What ships\n- **Part B** — site-wide behaviour.\n- **Part C** — exit-CTA tracking.\n\nTickets: WH-330, WH-331, WH-332\n\n---\n\n## PR Checklist\n\n- [ ] Code conforms to standards (WH-999)\n- [ ] Approved by at least one reviewers\n"
}
JSON

# --- the stub -------------------------------------------------------------
# Answers only what the script asks for. VERSION_EXISTS / VERSION_DRIVER let a
# test choose between the create path and the already-exists path, and every write
# is appended to $TMP/calls so the assertions can read the real payloads.
mkdir -p "$TMP/bin"
cat > "$TMP/bin/curl" <<'SH'
#!/usr/bin/env bash
url="${@: -1}"; body=""; method=GET; writeout=""
while [ $# -gt 0 ]; do
  case "$1" in
    -d) body="$2"; shift 2;;
    -X) method="$2"; shift 2;;
    -w) writeout="$2"; shift 2;;
    *) shift;;
  esac
done
echo "$method $url $body" >> "$TMPDIR_CALLS"
# The real curl prints only -w when the body goes to -o /dev/null; mirroring that
# keeps the script's own "(HTTP 204)" log lines honest under test.
if [ -n "$writeout" ]; then echo "204"; exit 0; fi
case "$url" in
  *"/issue/WH-328?"*) echo '{"fields":{"summary":"Full-journey attribution","reporter":{"accountId":"acct-nikita"},"created":"2026-07-20T10:00:00.000+0530"},"changelog":{"histories":[{"created":"2026-07-25T09:00:00.000+0530","items":[{"field":"status","toString":"In Dev"}]}]}}';;
  *"/issue/WH-330?"*) echo '{"fields":{"summary":"Part B — site-wide behaviour","reporter":{"accountId":"acct-x"},"created":"2026-07-28T07:40:32.751+0530"},"changelog":{"histories":[{"created":"2026-07-28T15:04:51.534+0530","items":[{"field":"status","toString":"In Dev"}]}]}}';;
  *"/issue/WH-331?"*) echo '{"fields":{"summary":"Part C — exit CTA","reporter":{"accountId":"acct-x"},"created":"2026-07-28T07:41:00.000+0530"},"changelog":{"histories":[{"created":"2026-07-22T11:00:00.000+0530","items":[{"field":"status","toString":"In Dev"}]}]}}';;
  *"/issue/WH-332?"*) echo '{"fields":{"summary":"Part D — identity","reporter":{"accountId":"acct-x"},"created":"2026-07-29T07:41:00.000+0530"},"changelog":{"histories":[]}}';;
  *"/project/WH/versions"*) [ "${VERSION_EXISTS:-0}" = 1 ] && echo '[{"name":"testrepo-1.6.1","id":"9001"}]' || echo '[]';;
  *"/version/9001?expand=driver"*) echo "{\"id\":\"9001\",\"description\":\"\",\"startDate\":\"\",\"driver\":\"${VERSION_DRIVER:-}\"}";;
  *"/version"*) echo '{"id":"9002"}';;
  *) echo '{}';;
esac
SH
chmod +x "$TMP/bin/curl"

run() { # env overrides come from the caller: `GIT_SUBJECT=... run`
  : > "$TMP/calls"
  TMPDIR_CALLS="$TMP/calls" PATH="$TMP/bin:$PATH" \
  JIRA_BASE_URL=https://example.invalid JIRA_EMAIL=t JIRA_API_TOKEN=t \
  FIX_VERSION=testrepo-1.6.1 PR_JSON="$TMP/pr.json" \
  GIT_SUBJECT="${GIT_SUBJECT:-[minor] PROD Release (WH-328): full-journey attribution (#1722)}" \
  bash "$SCRIPT" 2>&1
}

echo "1. the title's ticket plus every ticket on the 'Tickets:' line — and nothing else"
out=$(run)
for k in WH-328 WH-330 WH-331 WH-332; do
  check "$k gets the Fix Version" "$(cat "$TMP/calls")" "PUT https://example.invalid/rest/api/3/issue/$k"
done
check "add, never overwrite" "$(cat "$TMP/calls")" '{"update":{"fixVersions":[{"add":{"id":"9002"}}]}}'
refute "prose mention is not a shipped ticket"  "$(cat "$TMP/calls")" "issue/WH-902"
refute "parsing example is not a ticket"        "$(cat "$TMP/calls")" "issue/WH-901"
refute "Tickets: inside a code fence is a sample, not a trailer" "$(cat "$TMP/calls")" "issue/WH-801"
refute "…and neither is the rest of it"         "$(cat "$TMP/calls")" "issue/WH-802"
refute "checklist ticket ignored"               "$(cat "$TMP/calls")" "issue/WH-999"
refute "other-project string ignored"           "$(cat "$TMP/calls")" "issue/EH-WEB-1"
refute "other-project string ignored(2)"        "$(cat "$TMP/calls")" "issue/WEB-1"

echo "2. the three writable fields are filled at create time"
create=$(grep -F "POST https://example.invalid/rest/api/3/version" "$TMP/calls")
check "start date = earliest In Dev, not ship date" "$create" '"startDate":"2026-07-22"'
check "driver = reporter of the PR-title ticket"    "$create" '"driver":"acct-nikita"'
check "description lists each change with its key"  "$create" 'Part B — site-wide behaviour (WH-330)'
check "version left unreleased"                     "$create" '"released":false'
check "name matches the tag"                        "$create" '"name":"testrepo-1.6.1"'
refute "no release-notes field invented"            "$create" 'releaseNotes'

echo "3. an existing version is filled in, never clobbered"
out=$(VERSION_EXISTS=1 VERSION_DRIVER=acct-human run)
put=$(grep -F "PUT https://example.invalid/rest/api/3/version/9001" "$TMP/calls" || true)
check "blank fields still get filled"      "$put" '"startDate"'
refute "a human's driver is left alone"    "$put" 'driver'
refute "version is never renamed"          "$put" '"name"'
refute "version is never released by CI"   "$put" '"released"'

echo "4. no 'Tickets:' line falls back to the title alone, and says so"
printf '{"title":"[patch] PROD Release (WH-328): x","body":"Follows WH-902. No trailer here."}' > "$TMP/pr.json"
out=$(GIT_SUBJECT="[patch] PROD Release (WH-328): x (#9)" run)
check "title ticket still attributed" "$(cat "$TMP/calls")" "issue/WH-328"
refute "prose ticket still ignored"   "$(cat "$TMP/calls")" "issue/WH-902"
check "tells the author how to fix it" "$out" "Tickets: WH-330, WH-331"

echo "5. no ticket key anywhere is a clean no-op, not a failure"
printf '{"title":"[patch] chore: deps","body":"nothing here"}' > "$TMP/pr.json"
out=$(GIT_SUBJECT="chore: deps" run)
check "says so and stops" "$out" "nothing to tag"
refute "writes nothing"   "$(cat "$TMP/calls")" "PUT"

echo
echo "passed $pass, failed $fail"
[ "$fail" -eq 0 ]
