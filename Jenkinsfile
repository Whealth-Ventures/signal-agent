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
      choices: ['none', 'patch', 'minor', 'major'],
      description: 'Version bump for this release. none = rebuild the current version (use for re-deploys/rollbacks). patch = fixes. minor = new features. major = breaking changes. On a bump, Jenkins rewrites VERSION, commits "chore: bump version to X.Y.Z [skip ci]" and pushes to main BEFORE building.'
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
    BUMP        = "${params.BUMP ?: 'none'}"
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
    // WH-313. Runs BEFORE the artifact is named so the version reaches it. Skipped
    // when BUMP=none, so a re-deploy or rollback rebuilds the current version and
    // pushes nothing.
    stage('Bump version') {
      when { expression { env.BUMP != 'none' } }
      steps {
        script {
          def cur = readFile('VERSION').trim()
          def m = cur =~ /^([0-9]+)\.([0-9]+)\.([0-9]+)$/
          if (!m.matches()) {
            error("version must be MAJOR.MINOR.PATCH before bumping, got '${cur}'")
          }
          def (maj, min, pat) = [m[0][1] as int, m[0][2] as int, m[0][3] as int]
          def next
          switch (env.BUMP) {
            case 'major': next = "${maj + 1}.0.0"           ; break
            case 'minor': next = "${maj}.${min + 1}.0"      ; break
            case 'patch': next = "${maj}.${min}.${pat + 1}" ; break
            default: error("unknown BUMP '${env.BUMP}'")
          }
          echo "Bumping ${cur} -> ${next} (${env.BUMP})"
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
              # HEAD:<branch> — multibranch checks out a detached HEAD, so a bare
              # `git push origin main` would push nothing.
              git push origin "HEAD:main"
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
  }

  post {
    cleanup { cleanWs() }
  }
}
