#!/usr/bin/env bash
# Self-check for resolve-release-artifact.sh. `aws` is stubbed on PATH, so the key
# matching — the only part that can be wrong — is tested without touching S3.
set -uo pipefail
cd "$(dirname "$0")/.."

STUB_DIR=$(mktemp -d)
trap 'rm -rf "$STUB_DIR"' EXIT
cat > "$STUB_DIR/aws" <<'STUB'
#!/usr/bin/env bash
cat "$FAKE_S3"
STUB
chmod +x "$STUB_DIR/aws"
export PATH="$STUB_DIR:$PATH"
export FAKE_S3="$STUB_DIR/keys.txt"

# list-objects-v2 --output text returns keys tab-separated on one line.
printf 'artifacts/svc/1.0.1-aaaaaaaaaaaa.tgz\tartifacts/svc/1.0.2-bbbbbbbbbbbb.tgz\tartifacts/svc/1.0.10-cccccccccccc.tgz\tartifacts/svc/latest.tgz\n' > "$FAKE_S3"

pass=0 fail=0
check() { # check <name> <version> <expected-rc> <expected-substring>
  local name="$1" ver="$2" want_rc="$3" want="$4" out rc
  out=$(./scripts/resolve-release-artifact.sh bkt svc "$ver" 2>&1); rc=$?
  if [ "$rc" = "$want_rc" ] && printf '%s' "$out" | grep -q -- "$want"; then
    pass=$((pass+1)); echo "  ok   $name"
  else
    fail=$((fail+1)); echo "  FAIL $name (rc=$rc want=$want_rc)"; echo "$out" | sed 's/^/       /'
  fi
}

check "version resolves to its key"      1.0.2  0 "artifacts/svc/1.0.2-bbbbbbbbbbbb.tgz"
check "leading v accepted"               v1.0.1 0 "artifacts/svc/1.0.1-aaaaaaaaaaaa.tgz"
check "1.0.1 does not match 1.0.10"      1.0.1  0 "1.0.1-aaaaaaaaaaaa"
check "1.0.10 resolves on its own"       1.0.10 0 "artifacts/svc/1.0.10-cccccccccccc.tgz"
check "missing version fails"            9.9.9  1 "no artifact in s3://bkt/artifacts/svc/"
check "missing version lists what exists" 9.9.9 1 "1.0.2"
check "non-semver rejected"              latest 2 "is not a semver version"

# latest.tgz must never be selectable as a rollback target.
out=$(./scripts/resolve-release-artifact.sh bkt svc 1.0.2 2>&1)
if printf '%s' "$out" | grep -q 'latest'; then
  fail=$((fail+1)); echo "  FAIL latest.tgz leaked into the result: $out"
else
  pass=$((pass+1)); echo "  ok   latest.tgz excluded"
fi

# Two artifacts claiming one version must stop, not guess.
printf 'artifacts/svc/2.0.0-aaaaaaaaaaaa.tgz\tartifacts/svc/2.0.0-bbbbbbbbbbbb.tgz\n' > "$FAKE_S3"
check "ambiguous version refuses"        2.0.0  1 "matches 2 artifacts"

echo
echo "resolve-release-artifact: $pass passed, $fail failed"
[ "$fail" -eq 0 ]
