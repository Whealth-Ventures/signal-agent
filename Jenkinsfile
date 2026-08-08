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

  environment {
    AWS_REGION = 'ap-south-1'
    PROJECT    = 'signal-agent'
    APP_ENV    = 'prod'
  }

  stages {
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

          # inputs/ is NOT in git — SharePoint owns it. Pull the same
          # SHAREPOINT_* credentials the box uses so CI tests against the real,
          # current inputs. Best-effort: if the Jenkins role can't read the
          # secret, the sync no-ops and the input-dependent tests skip
          # themselves (the skip reason says so) rather than failing the build.
          SECRET_JSON="$(aws secretsmanager get-secret-value --region "$AWS_REGION" \
              --secret-id "$PROJECT/$APP_ENV/agent-env" \
              --query SecretString --output text 2>/dev/null || echo '{}')"
          for K in SHAREPOINT_TENANT_ID SHAREPOINT_CLIENT_ID SHAREPOINT_CLIENT_SECRET \
                   SHAREPOINT_SITE SHAREPOINT_INPUTS_PATH; do
            V="$(printf '%s' "$SECRET_JSON" | jq -r --arg k "$K" '.[$k] // ""')"
            # `|| true` matters: under `set -e` a false test would abort the build.
            [ -n "$V" ] && export "$K=$V" || true
          done
          unset SECRET_JSON
          python src/sharepoint_sync.py

          # config.py parses inputs/tuning.xlsx at import time, so *something*
          # must be there or every test dies on import. Bootstrap code defaults
          # only if the sync didn't provide one.
          test -f inputs/tuning.xlsx || python scripts/build_default_tuning_xlsx.py

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
          KEY="artifacts/signal-agent/${GIT_COMMIT}.tgz"

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
            --document-name AWS-RunShellScript --comment "signal-agent deploy ${GIT_COMMIT}" \
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
