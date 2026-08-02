// =============================================================================
// WH-313 — release naming helpers: bump resolution + Jira key extraction.
//
// NO `=~` (find operator) anywhere: it returns a java.util.regex.Matcher, which is
// NOT serializable, and Jenkins CPS serializes every local at each step boundary —
// a Matcher alive across an `sh` call killed xponentiate-nextjs main #3. `==~`
// (match operator) is fine: it returns a boolean. `replaceAll`/`split` are fine
// too: they return String/String[] and retain no Matcher.
//
// @NonCPS would also solve it, but it cannot be compile-checked outside Jenkins
// ("unable to resolve class NonCPS"), so plain string operations are used instead.
// =============================================================================

// Pull the PR number out of a merge commit subject. Both shapes are in use:
//   merge commit : "Merge pull request #1688 from Whealth-Ventures/chore/..."
//   squash merge : "fix(reports): reportlab missing from the image (#78)"
String prNumberFrom(String subject) {
  if (!subject) return null
  def s = subject.trim()

  final String MERGE_PREFIX = 'Merge pull request #'
  if (s.startsWith(MERGE_PREFIX)) {
    def rest = s.substring(MERGE_PREFIX.length())
    def end = rest.indexOf(' ')
    def num = (end < 0) ? rest : rest.substring(0, end)
    return (num ==~ /^[0-9]+$/) ? num : null
  }

  if (s.endsWith(')')) {
    def open = s.lastIndexOf('(#')
    if (open >= 0) {
      def num = s.substring(open + 2, s.length() - 1)
      return (num ==~ /^[0-9]+$/) ? num : null
    }
  }
  return null
}

// Map a PR title to a semver bump.
//
// Priority:
//   1. [major] / [minor] / [patch]   <- THE convention (WH-313). Mandatory on PRs
//      to main, enforced by .github/workflows/pr-title-bump.yml. This form is used
//      because the release PRs that actually merge to main are
//      "PROD Release (WH-XXX): ..." — which carries the Jira key but no bump — and
//      the team had already started writing "[Minor] PROD Release ..." by hand.
//   2. MAJOR: / MINOR: / PATCH:      <- equivalent colon form
//   3. conventional commits          <- feat: -> minor, fix:/chore: -> patch,
//                                       feat!: / feat(api)!: -> major. Kept for
//                                       repos that use it (ai-interviewer: 21/30).
// Returns null when the title carries no signal. The caller decides what that
// means; this never guesses.
String bumpFromTitle(String title) {
  if (!title) return null
  def t = title.trim()

  // 1. leading [bump] marker
  if (t.startsWith('[')) {
    def close = t.indexOf(']')
    if (close > 1) {
      def tag = t.substring(1, close).trim().toLowerCase()
      if (tag == 'major' || tag == 'minor' || tag == 'patch') return tag
    }
  }

  if (t.contains('BREAKING CHANGE')) return 'major'

  // Everything before the first ':' is the type.
  def colon = t.indexOf(':')
  if (colon < 0) return null
  def type = t.substring(0, colon).trim().toLowerCase()

  // 2. explicit colon form
  if (type == 'major' || type == 'minor' || type == 'patch') return type

  // 3. conventional commits; a trailing '!' marks a breaking change
  boolean breaking = type.endsWith('!')
  if (breaking) type = type.substring(0, type.length() - 1).trim()

  def paren = type.indexOf('(')
  if (paren >= 0) {
    if (!type.endsWith(')')) return null
    type = type.substring(0, paren).trim()
  }
  if (!(type ==~ /^[a-z]+$/)) return null

  if (breaking) return 'major'
  if (type == 'feat') return 'minor'
  if (type in ['fix', 'chore', 'docs', 'refactor', 'perf', 'test', 'style', 'build', 'ci', 'revert']) {
    return 'patch'
  }
  return null
}

// Every Jira key in a piece of text, deduped, first-seen order preserved.
//
// A release routinely names several tickets — nutrition's real titles include
// "(also WH-310, WH-297, WH-295)" — and each of them needs the Fix Version, so
// this collects all of them rather than just the first.
//
// replaceAll returns a String (no Matcher escapes), so this stays CPS-safe.
List<String> jiraKeysFrom(String text) {
  if (!text) return []
  def seen = new LinkedHashSet<String>()
  text.replaceAll('[^A-Za-z0-9-]', ' ').tokenize(' ').each { tok ->
    // strip trailing/leading dashes a split can leave behind
    def k = tok.toUpperCase()
    while (k.startsWith('-')) { k = k.substring(1) }
    while (k.endsWith('-')) { k = k.substring(0, k.length() - 1) }
    if (k ==~ /^[A-Z][A-Z0-9]*-[0-9]+$/) seen.add(k)
  }
  return new ArrayList<String>(seen)
}

// owner/repo from the git remote, so nothing is hardcoded per repo.
String slugFrom(String remoteUrl) {
  if (!remoteUrl) return null
  def u = remoteUrl.trim()
  def i = u.indexOf('github.com')
  if (i < 0) return null
  def rest = u.substring(i + 'github.com'.length())
  if (rest.startsWith(':') || rest.startsWith('/')) rest = rest.substring(1)
  if (rest.endsWith('.git')) rest = rest.substring(0, rest.length() - 4)
  rest = rest.trim()
  def parts = rest.tokenize('/')
  return (parts.size() == 2 && parts.every { it }) ? "${parts[0]}/${parts[1]}".toString() : null
}

// signal-agent CI/CD — validate on every branch/PR, deploy on main via SSM.
//
// Requirements on the Jenkins node:
//   - python3 (3.11 preferred), node 20, npm, aws CLI v2, jq
//   - AWS access to the Whealth account (873448587721): attach the Terraform
//     output `jenkins_deploy_policy_arn` to the Jenkins instance role, OR bind a
//     credentials pair with id 'aws-whealth' (uncomment the withAWS/withCredentials).
//
// Deploy = SSM Run Command -> the box pulls this exact commit from GitHub,
// builds, and restarts services. No artifact copy; the box is the build host.

pipeline {
  agent any

  options {
    timestamps()
    disableConcurrentBuilds()
    timeout(time: 30, unit: 'MINUTES')
    buildDiscarder(logRotator(numToKeepStr: '30'))
  }

  // WH-313. Semantic version bump, decided HERE rather than by hand-editing the
  // version in a PR. 'none' rebuilds the current version (the old behaviour, and
  // the right choice for a re-deploy or a rollback).
  //
  // Deliberately a choice, not derived from commit messages: whether a change is
  // breaking is a judgement call someone should make explicitly, and a `feat!:`
  // prefix is easy to forget and impossible to correct after the fact.
  parameters {
    choice(
      name: 'BUMP',
      choices: ['auto', 'none', 'patch', 'minor', 'major'],
      description: '''How to version this release.

auto  (default) — derive from the merged PR's title. MAJOR:/MINOR:/PATCH: prefix wins;
                  otherwise the conventional-commit prefix is used (feat: -> minor,
                  fix:/chore:/docs: -> patch, feat!: -> major). No signal -> patch.
                  This is what every webhook-triggered deploy uses, so releases merged
                  through a PR are versioned with nobody selecting anything.
none            — rebuild the CURRENT version; no bump, no commit, no tag. Use for a
                  re-deploy or a rollback.
patch/minor/major — explicit override, wins over the PR title. Use for a direct push
                  that never went through a PR.'''
    )
  }

  environment {
    AWS_REGION = 'ap-south-1'
    PROJECT    = 'signal-agent'
    APP_ENV    = 'prod'
    // Resolved HERE, not read as $BUMP inside a shell: Jenkins registers
    // `parameters {}` only at the END of a run, so on the FIRST build after this
    // parameter is added the variable does not exist in the environment and a
    // `set -u` shell aborts on the expansion (exactly how everhope_nextjs main #5
    // died on ALLOW_RETAG). ?: gives the safe default on that first run.
    //
    // That default MUST be 'auto', not 'none'. A build triggered by branch
    // indexing carries no ParametersAction at all — not even the declared
    // defaults — so params.BUMP is null on every automatic deploy even though
    // the parameter is registered on the job. Defaulting to 'none' there meant
    // Xponentiate-strapi main #4 rebuilt 0.1.0 and never bumped: it silently
    // disabled versioning for exactly the webhook-driven releases this versions.
    BUMP        = "${params.BUMP ?: 'auto'}"
    GIT_CRED_ID = 'github-signal-agent'

  }

  stages {

    // WH-313. Breaks the bump recursion: 'Bump version' pushes a commit, GitHub
    // fires the webhook, and this job builds again. Without this gate that second
    // run would bump again and loop forever.
    //
    // A webhook rebuild of a bump commit is a no-op — the artifact for that version
    // was already built by the run that made the commit. Abort NOT_BUILT instead of
    // rebuilding identical bytes. Only the AUTOMATIC path is blocked: a human
    // pressing Build is a deliberate re-deploy, and getBuildCauses() lets it through.
    stage('Skip CI for bump commits') {
      steps {
        script {
          // SUBJECT line only (%s), and an anchored match on the exact shape this
          // pipeline writes. A `contains('[skip ci]')` over the full message (%B) is
          // wrong: a squash merge folds the PR body into the commit, so any commit whose
          // description merely MENTIONS [skip ci] — including the PR that introduced this
          // very stage — gets blocked. That misfired on ai-interviewer build #13.
          def subject = sh(returnStdout: true, script: 'git log -1 --pretty=%s').trim()
          def isBumpCommit = subject ==~ /^chore: bump version to [0-9]+\.[0-9]+\.[0-9]+ \[skip ci\]$/
          def manual = currentBuild.getBuildCauses().any {
            it._class?.contains('UserIdCause') || it._class?.contains('ReplayCause')
          }
          if (isBumpCommit && !manual) {
            currentBuild.result = 'NOT_BUILT'
            error("Skipping: HEAD is a CI bump commit ([skip ci]) and this build was triggered automatically. The release it names was already built.")
          }
          if (isBumpCommit) {
            echo "HEAD is a bump commit, but this build was started manually — continuing as an explicit re-deploy."
          }
        }
      }
    }


    stage('Agent — tests') {
      environment {
        // tests/test_config.py preflights that a configured env exists (on a
        // dev machine that's the real .env). CI has no secrets by design —
        // real values live in Secrets Manager and reach the box at deploy
        // time — so provide correctly-shaped placeholders here.
        OPENAI_API_KEY     = 'ci-placeholder'
        PERPLEXITY_API_KEY = 'ci-placeholder'
        SLACK_WEBHOOK_URL  = 'https://hooks.slack.com/services/CI/PLACEHOLDER/ci'
      }
      steps {
        sh '''
          set -eu
          PY="$(command -v python3.11 || command -v python3)"
          "$PY" -m venv .venv
          . .venv/bin/activate
          pip install --quiet --upgrade pip
          pip install --quiet -r requirements.txt pytest
          pytest -q
        '''
      }
    }

    stage('Admin — typecheck + build') {
      steps {
        dir('admin') {
          sh '''
            set -eu
            npm ci --no-audit --no-fund
            npm run typecheck
            npm run build
          '''
        }
      }
    }
    // WH-313. Resolves the bump ONCE here so 'Bump version' below is pure arithmetic.
    //
    // Two ways in, deliberately:
    //   - BUMP=auto (the default, and what every webhook build gets) reads the merged
    //     PR title, so a PR-driven deploy versions itself with no human involved.
    //   - An explicit patch/minor/major from the Jenkins dropdown always wins — the
    //     manual path for a direct push that never went through a PR.
    //
    // A title lookup NEVER fails the build: the deploy is downstream and this only
    // decides a name. On any failure it warns and falls back to patch.
    stage('Resolve version bump') {
      steps {
        script {
          if (env.BUMP == 'none') {
            echo "BUMP=none — rebuilding the current version. No bump, no commit, no tag."
            env.RESOLVED_BUMP = 'none'
          } else if (env.BUMP != 'auto') {
            echo "BUMP=${env.BUMP} — selected explicitly in Jenkins, so the PR title is not consulted."
            env.RESOLVED_BUMP = env.BUMP
          } else {
            def subject = sh(returnStdout: true, script: 'git log -1 --pretty=%s').trim()
            def remote  = sh(returnStdout: true, script: 'git config --get remote.origin.url').trim()
            def pr      = prNumberFrom(subject)
            def slug    = slugFrom(remote)
            String title = null

            if (pr && slug) {
              withCredentials([string(credentialsId: 'github-api-token', variable: 'GH_TOKEN')]) {
                // set +x so the token is never echoed. `|| true` + `// empty` so a failed
                // lookup yields "" rather than aborting the shell under set -e.
                title = sh(returnStdout: true, script: '''#!/usr/bin/env bash
                  set -eu
                  set +x
                  curl -sS --max-time 20 \
                       -H "Authorization: Bearer $GH_TOKEN" \
                       -H "Accept: application/vnd.github+json" \
                       "https://api.github.com/repos/''' + slug + '''/pulls/''' + pr + '''" \
                    2>/dev/null | jq -r '.title // empty' || true
                ''').trim()
              }
              if (title) {
                echo "PR #${pr} title: ${title}"
              } else {
                echo "WARNING: could not read the title of PR #${pr} (token scope, or the API is down)."
              }
            } else {
              echo "HEAD is not a PR merge (subject: ${subject}) — no PR title to read."
            }

            // Kept for the Jira stage: the ticket keys live in the PR title, and by
            // the time that stage runs the merge subject alone may not carry them.
            env.PR_TITLE = title ?: ''

            def derived = bumpFromTitle(title)
            if (derived) {
              echo "Bump resolved from the PR title: ${derived}"
              env.RESOLVED_BUMP = derived
            } else {
              // Defaulting to patch, not minor: of the last 30 everhope_nextjs PROD
              // releases the ticket graded 21 patch / 8 minor / 1 major, and a
              // default of minor is what produced the manual "correct this bump to
              // 1.3.1 — the release is a fix, not a feature" commit on 2026-07-30.
              echo "WARNING: no bump signal in the PR title — defaulting to patch."
              echo "         Prefix the PR title with MAJOR:/MINOR:/PATCH:, or use a"
              echo "         conventional-commit prefix (feat: / fix:), to be explicit."
              env.RESOLVED_BUMP = 'patch'
            }
          }
        }
      }
    }

    // WH-313. Runs BEFORE the artifact is named so the version reaches it. Skipped
    // when BUMP=none, so a re-deploy or rollback rebuilds the current version and
    // pushes nothing.
    stage('Bump version') {
      when { allOf { branch 'main'; expression { env.RESOLVED_BUMP != 'none' } } }
      steps {
        script {
          def cur = readFile('VERSION').trim()
          // NO regex Matcher here. Jenkins CPS serializes every local variable at each
          // step boundary, and java.util.regex.Matcher is not serializable — holding one
          // across the `sh` steps below dies with
          //   java.io.NotSerializableException: java.util.regex.Matcher
          // which is exactly how xponentiate-nextjs main #3 failed, AFTER correctly
          // computing 0.1.0 -> 0.2.0. A standalone Groovy test does not catch this
          // because it never runs under CPS.
          //
          // `==~` elsewhere in this file is safe: it returns a boolean. It is `=~`
          // (the find operator, which returns a Matcher) that must be avoided.
          def parts = cur.tokenize('.')
          if (parts.size() != 3 || !parts.every { it ==~ /^[0-9]+$/ }) {
            error("package.json version must be MAJOR.MINOR.PATCH before bumping, got '${cur}'")
          }
          int maj = parts[0].toInteger()
          int min = parts[1].toInteger()
          int pat = parts[2].toInteger()
          def next
          switch (env.RESOLVED_BUMP) {
            case 'major': next = "${maj + 1}.0.0"           ; break
            case 'minor': next = "${maj}.${min + 1}.0"      ; break
            case 'patch': next = "${maj}.${min}.${pat + 1}" ; break
            default: error("unknown bump '${env.RESOLVED_BUMP}'")
          }
          echo "Bumping ${cur} -> ${next} (${env.RESOLVED_BUMP})"
          env.NEW_VERSION = next

          writeFile file: 'VERSION', text: "${next}\n"

          // Push over SSH with the repo's deploy key (read_only=false, verified).
          // "[skip ci]" is what stops the recursion: this push fires the webhook,
          // which would build and bump again — forever. The guard stage below
          // aborts that rebuild.
          sshagent([env.GIT_CRED_ID]) {
            sh '''
              set -eu
              mkdir -p ~/.ssh && ssh-keyscan -H github.com >> ~/.ssh/known_hosts 2>/dev/null || true
              git config user.email "jenkins@xponentiate.com"
              git config user.name  "Jenkins CI"
              git add VERSION
              git commit -m "chore: bump version to ${NEW_VERSION} [skip ci]"
              # WH-313: tag the release so the version is a real git ref, not just a
              # value in a file. The multibranch checkout fetches without tags, so
              # existing tags are not local — fetch them before testing for one.
              git fetch --tags --force origin
              # Ask the REMOTE, not the local ref store. A tag left in this workspace by
              # an earlier run that died between `git tag` and the push is
              # indistinguishable from a published one via rev-parse — which is exactly
              # what blocked `Everhope Data` #15 and `sso data`: both had an UNPUBLISHED
              # v1.1.0 sitting in the workspace from their previous GH013 push failure,
              # so the guard refused a release that had never actually happened.
              # ls-remote is authoritative; the workspace is not.
              if [ -n "$(git ls-remote --tags origin "refs/tags/v${NEW_VERSION}")" ]; then
                echo "tag v${NEW_VERSION} already exists on origin — refusing to move it." >&2
                echo "A tag pointing somewhere new breaks every rollback aimed at it." >&2
                exit 1
              fi
              # -f overwrites a stale LOCAL tag only. The push below is deliberately not
              # --force, so a tag that really is published still cannot be moved: that
              # remains the actual safety net, this check just fails clearly instead.
              git tag -f -a "v${NEW_VERSION}" -m "Release ${NEW_VERSION}"
              # HEAD:<branch> — multibranch checks out a detached HEAD, so a bare
              # `git push origin main` would push nothing.
              # Commit and tag land together or not at all: a bump commit with no tag
              # leaves a version git cannot name; a tag with no commit is orphaned.
              git push --atomic origin "HEAD:main" "refs/tags/v${NEW_VERSION}"
            '''
          }
        }
      }
    }


    // WH-313. Names the release. Runs after 'Bump version' so it sees the new
    // value, and before 'Deploy' which uses it as the S3 artifact key.
    stage('Resolve release tag') {
      steps {
        script {
          env.VERSION = readFile('VERSION').trim()
          if (!(env.VERSION ==~ /^[0-9]+\.[0-9]+\.[0-9]+$/)) {
            error("version must be MAJOR.MINOR.PATCH, got '${env.VERSION}'")
          }
          env.GIT_SHA     = sh(returnStdout: true, script: 'git rev-parse --short=12 HEAD').trim()
          env.RELEASE_TAG = "${env.VERSION}-${env.GIT_SHA}"
          currentBuild.displayName = "#${env.BUILD_NUMBER}  ${env.VERSION} (${env.GIT_SHA})"
          echo "Release ${env.VERSION} -> artifact ${env.RELEASE_TAG}.tgz"
        }
      }
    }

    stage('Deploy (main)') {
      when { branch 'main' }
      steps {
        // withCredentials([[$class: 'AmazonWebServicesCredentialsBinding', credentialsId: 'aws-whealth']]) {
        sh '''
          set -eu
          BUCKET="$(aws ssm get-parameter --region "$AWS_REGION" \
                    --name "/$PROJECT/$APP_ENV/feedback-bucket" --query Parameter.Value --output text)"
          IID="$(aws ssm get-parameter --region "$AWS_REGION" \
                  --name "/$PROJECT/$APP_ENV/instance-id" --query Parameter.Value --output text)"
          # WH-313: artifact key is <version>-<sha>, not a bare 40-char sha, so the S3
          # object names the release. GIT_COMMIT stays in it: the version alone is
          # re-pointable (a rebuild of the same version would overwrite the object).
          KEY="artifacts/signal-agent/${RELEASE_TAG}.tgz"

          # PUSH model: package the reviewed workspace and upload to S3. Build
          # artifacts (.venv/.next/node_modules) and data/ are excluded — the box
          # rebuilds and keeps its own state. The box never talks to GitHub.
          echo "Packaging workspace -> s3://$BUCKET/$KEY"
          tar czf /tmp/sa-app.tgz \
            --exclude=./.git --exclude=./.venv --exclude=./admin/node_modules \
            --exclude=./admin/.next --exclude=./data --exclude=./__pycache__ \
            --exclude='*.pyc' .
          aws s3 cp /tmp/sa-app.tgz "s3://$BUCKET/$KEY" --region "$AWS_REGION"
          aws s3 cp "s3://$BUCKET/$KEY" "s3://$BUCKET/artifacts/signal-agent/latest.tgz" --region "$AWS_REGION"
          rm -f /tmp/sa-app.tgz

          echo "Deploying $KEY to $IID"
          CMD_ID="$(aws ssm send-command \
            --region "$AWS_REGION" --instance-ids "$IID" \
            --document-name AWS-RunShellScript --comment "signal-agent deploy ${RELEASE_TAG}" \
            --timeout-seconds 900 \
            --parameters '{"commands":["/usr/local/bin/sa-fetch.sh '"$KEY"'","bash /opt/signal-agent/repo/deploy/deploy.sh"]}' \
            --query Command.CommandId --output text)"
          echo "SSM command: $CMD_ID"

          # Poll to completion.
          for _ in $(seq 1 120); do
            sleep 8
            ST="$(aws ssm get-command-invocation --region "$AWS_REGION" \
                    --command-id "$CMD_ID" --instance-id "$IID" \
                    --query Status --output text 2>/dev/null || echo Pending)"
            echo "  status: $ST"
            case "$ST" in
              Success) break ;;
              Failed|Cancelled|TimedOut) BAD=1; break ;;
            esac
          done

          echo "----- stdout -----"
          aws ssm get-command-invocation --region "$AWS_REGION" \
            --command-id "$CMD_ID" --instance-id "$IID" \
            --query StandardOutputContent --output text || true
          echo "----- stderr -----"
          aws ssm get-command-invocation --region "$AWS_REGION" \
            --command-id "$CMD_ID" --instance-id "$IID" \
            --query StandardErrorContent --output text || true

          [ "${BAD:-0}" = "1" ] && { echo "DEPLOY FAILED"; exit 1; } || echo "DEPLOY OK"
        '''
        // }
      }
    }

    // WH-313. Last stage: Jira is a bookkeeping side effect, so it must never be
    // able to fail a deploy that already succeeded — hence catchError(SUCCESS).
    stage('Jira Fix Version') {
      when { allOf { branch 'main'; expression { env.RESOLVED_BUMP != 'none' && env.VERSION } } }
      steps {
        catchError(buildResult: 'SUCCESS', stageResult: 'UNSTABLE') {
          script {
            def subject = sh(returnStdout: true, script: 'git log -1 --pretty=%s').trim()
            def keys    = jiraKeysFrom("${env.PR_TITLE ?: ''} ${subject}")
            if (!keys) {
              echo 'Jira: no ticket key in the PR title or commit subject — nothing to tag.'
              return
            }

            def slug = slugFrom(sh(returnStdout: true, script: 'git config --get remote.origin.url').trim())
            def repo = slug ? slug.tokenize('/')[1] : env.JOB_NAME.tokenize('/')[0]
            echo "Jira: ${repo}-${env.VERSION} -> ${keys.join(', ')}"

            withCredentials([usernamePassword(credentialsId: 'jira-api',
                                              usernameVariable: 'JIRA_EMAIL',
                                              passwordVariable: 'JIRA_API_TOKEN')]) {
              withEnv(["JIRA_BASE_URL=https://2070health.atlassian.net",
                       "FIX_VERSION=${repo}-${env.VERSION}",
                       "JIRA_PROJECT=${keys[0].tokenize('-')[0]}",
                       "JIRA_KEYS=${keys.join(' ')}"]) {
                // set +x so the token never lands in the build log. jq builds every
                // payload, so nothing has to be hand-escaped into JSON.
                sh '''#!/usr/bin/env bash
                  set -eu
                  set +x
                  API="$JIRA_BASE_URL/rest/api/3"

                  vid=$(curl -sS --max-time 30 -u "$JIRA_EMAIL:$JIRA_API_TOKEN" \
                          "$API/project/$JIRA_PROJECT/versions" \
                        | jq -r --arg n "$FIX_VERSION" 'map(select(.name==$n))[0].id // empty')

                  if [ -n "$vid" ]; then
                    echo "  version exists (id=$vid)"
                  else
                    # Created UNRELEASED, with today as the planned release date.
                    #
                    # CI knows the code deployed; it does not know the release was
                    # accepted, so marking it released is a human decision. The date
                    # is set because `released:true` with an empty releaseDate is
                    # exactly what the jira-alerts rule flags, and it also makes the
                    # version show as overdue until someone signs it off — which is
                    # the nudge that makes "a human marks it released" actually happen.
                    REL_DATE=$(date -u +%Y-%m-%d)
                    vid=$(curl -sS --max-time 30 -u "$JIRA_EMAIL:$JIRA_API_TOKEN" -X POST \
                            -H 'Content-Type: application/json' \
                            -d "$(jq -nc --arg n "$FIX_VERSION" --arg p "$JIRA_PROJECT" \
                                  --arg d "$REL_DATE" \
                                  '{name:$n,project:$p,released:false,releaseDate:$d}')" \
                            "$API/version" | jq -r '.id // empty')
                    [ -n "$vid" ] && echo "  version created unreleased, dated $REL_DATE (id=$vid) — mark it released in Jira once the release is signed off"
                  fi

                  if [ -z "$vid" ]; then
                    echo "WARN: Jira version '$FIX_VERSION' is missing and could not be created." >&2
                    echo "      The account needs Administer Projects on $JIRA_PROJECT. Skipping." >&2
                    exit 0
                  fi

                  for k in $JIRA_KEYS; do
                    code=$(curl -sS --max-time 30 -o /dev/null -w '%{http_code}' \
                             -u "$JIRA_EMAIL:$JIRA_API_TOKEN" -X PUT \
                             -H 'Content-Type: application/json' \
                             -d "$(jq -nc --arg id "$vid" \
                                   '{update:{fixVersions:[{add:{id:$id}}]}}')" \
                             "$API/issue/$k")
                    echo "  $k -> $FIX_VERSION (HTTP $code)"
                  done
                '''
              }
            }
          }
        }
      }
    }
  }

  post {
    cleanup { cleanWs() }
  }
}
