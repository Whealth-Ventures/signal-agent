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
            --parameters '{"commands":["/usr/local/bin/sa-fetch.sh '"$KEY"'","/opt/signal-agent/repo/deploy/deploy.sh"]}' \
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
