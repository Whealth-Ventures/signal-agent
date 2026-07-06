# signal-agent — AWS deployment (Whealth account)

Everything runs on **one EC2 box** in the existing `xponentiate-vpc`
(account `873448587721`, `ap-south-1`). No ECS, no Vercel.

- **Digest agent** — Python batch job, run daily by a **systemd timer** at
  02:20 UTC (07:50 IST); the app itself holds until 08:00 IST before posting.
  State (SQLite + Chroma) lives on the box's EBS volume, backed up nightly to S3.
- **Admin UI** — Next.js, run by **systemd** on port 3000, fronted by the
  existing **jenkins-alb** at `https://signal-admin.xponentiate.com`
  (wildcard `*.xponentiate.com` cert via SNI — no new ALB).
- **Secrets** — Secrets Manager, rendered to env files at deploy time.
- **Deploys** — Jenkins → **SSM Run Command** → the box pulls the reviewed
  commit from the private GitHub repo, builds, and restarts the services.
- **Feedback events** — S3 bucket (replaces Vercel Blob).

```
                         Route53 signal-admin.xponentiate.com
                                        │
                          jenkins-alb :443 (host rule + *.xponentiate.com cert)
                                        │  (only from ALB SG)
   EventBridge? no ── systemd timer     ▼
     02:20 UTC ─────► run-digest.sh   EC2 (t3a.small, public subnet)
                         │              ├─ signal-admin.service  (next start :3000)
                         │              └─ signal-agent.timer → signal-agent.service
                         ▼                     │
                   src/main.py                 ├─ Secrets Manager (agent-env, admin-env)
                   --post-at 08:00             ├─ S3 feedback bucket (events/, state/)
                                               └─ GitHub (private, read PAT) for code+data
```

## Files

| Path | What |
|---|---|
| `infra/*.tf` | Terraform: SG, EC2+EIP, IAM, Secrets Manager, S3, ALB rule, Route53, SSM params |
| `infra/user_data.sh.tftpl` | First-boot: toolchain, app user, git askpass, first clone+deploy |
| `deploy/deploy.sh` | Run by SSM: checkout ref → env files → build venv+admin → restart services |
| `deploy/run-digest.sh` | Run by the timer: refresh inputs/prompts → `main.py` → feedback → backup |
| `deploy/signal-*.service`, `signal-agent.timer` | systemd units |
| `Jenkinsfile` | test agent + admin, deploy on `main` via SSM |

---

## One-time setup

### 0. Prerequisites
- Terraform ≥ 1.11, AWS CLI, `AWS_PROFILE=Whealth`, `AWS_REGION=ap-south-1`.
- Two GitHub fine-grained PATs on `ashwinknan/signal-agent`:
  - **read PAT** (Contents: Read) — for the box to clone + pull.
  - **read/write PAT** (Contents: Read & Write) — for the admin UI to commit
    tuning/prompt edits. (Can be the same token if you prefer.)

### 1. Provision
```bash
cd infra
export AWS_PROFILE=Whealth AWS_REGION=ap-south-1
terraform init
terraform apply
```
This creates the box, IAM, S3, DNS, the ALB host rule, and the **secret
containers with placeholder values** (real values never live in state/git).

### 2. Fill in the secrets
Put the real values into the two secrets (JSON). Example:
```bash
aws secretsmanager put-secret-value --secret-id signal-agent/prod/agent-env \
  --secret-string '{
    "OPENAI_API_KEY":"sk-...",
    "PERPLEXITY_API_KEY":"pplx-...",
    "ANTHROPIC_API_KEY":"sk-ant-...",
    "SLACK_WEBHOOK_URL":"https://hooks.slack.com/services/...",
    "SLACK_BOT_TOKEN":"xoxb-...",
    "SLACK_CHANNEL_ID":"C0123ABC",
    "SLACK_CHANNEL_LABEL":"#signal",
    "GITHUB_TOKEN":"github_pat_READ_ONLY"
  }'

aws secretsmanager put-secret-value --secret-id signal-agent/prod/admin-env \
  --secret-string '{
    "GITHUB_TOKEN":"github_pat_READ_WRITE",
    "GITHUB_OWNER":"ashwinknan","GITHUB_REPO":"signal-agent","GITHUB_BRANCH":"main",
    "GIT_COMMIT_EMAIL":"signal-agent@whealthventures.com",
    "AUTH_SECRET":"'"$(openssl rand -hex 32)"'",
    "ADMIN_USER":"admin","ADMIN_PWD":"a-strong-password",
    "SLACK_SIGNING_SECRET":"..."
  }'
```
> Terraform ignores `secret_string` after creation, so these values survive
> future `apply`s.

### 3. First deploy
The box tries a clone+deploy at boot; if the read PAT wasn't set yet, just
trigger it now (also what Jenkins does):
```bash
IID=$(aws ssm get-parameter --name /signal-agent/prod/instance-id --query Parameter.Value --output text)
aws ssm send-command --instance-ids "$IID" --document-name AWS-RunShellScript \
  --parameters commands='["sudo /usr/local/bin/sa-bootstrap.sh","sudo /opt/signal-agent/repo/deploy/deploy.sh main"]' \
  --query Command.CommandId --output text
```
Watch it: `aws ssm get-command-invocation --command-id <id> --instance-id "$IID"`.

### 4. Jenkins pipeline
- New **Multibranch Pipeline** pointed at `ashwinknan/signal-agent` (Jenkinsfile
  at repo root). It validates every branch/PR and deploys on `main`.
- Give Jenkins AWS access: attach the Terraform output
  `jenkins_deploy_policy_arn` to the Jenkins EC2 instance role, **or** bind an
  AWS credentials pair with id `aws-whealth` and uncomment the `withCredentials`
  wrapper in the `Deploy` stage.
- Node 20, python3.11, aws CLI, jq must be on the Jenkins agent.

### 5. Point Slack at the new admin URL
In api.slack.com → your app → **Event Subscriptions**, set the Request URL to:
```
https://signal-admin.xponentiate.com/api/slack/events
```
(`terraform output slack_events_url`). Keep bot events `reaction_added` /
`reaction_removed`. The receiver now writes to S3, and the daily run's
`feedback_puller` reads from S3 — no Vercel.

### 6. Decommission Vercel
Once the admin URL is green and Slack events are landing in S3
(`aws s3 ls s3://$(terraform output -raw feedback_bucket)/events/`), delete the
Vercel project and its Blob store. The `@vercel/blob` dependency and the
`vercel.com/api/blob` reference are already removed from the codebase.

---

## Operations

**Shell on the box** (no SSH key needed):
```bash
aws ssm start-session --target "$IID"
```

**Run the digest now** (e.g. a test post):
```bash
aws ssm send-command --instance-ids "$IID" --document-name AWS-RunShellScript \
  --parameters commands='["sudo -u signal /opt/signal-agent/repo/.venv/bin/python /opt/signal-agent/repo/src/main.py --test"]'
```
On the box directly: `sudo systemctl start signal-agent.service` (a normal run),
or `journalctl -u signal-agent.service -f` for logs. Per-module logs are in
`/opt/signal-agent/repo/data/logs/`.

**Admin logs / restart:** `journalctl -u signal-admin.service -f` /
`sudo systemctl restart signal-admin.service`.

**Rollback:** deploy an older commit — `deploy/deploy.sh <old-sha>` via SSM (same
command as step 3 with the SHA). State in `data/` is untouched by deploys.

**Schedule change:** edit `digest_oncalendar_utc` in `infra/variables.tf`
(and `digest_post_at_ist`), `terraform apply`, then redeploy (deploy.sh stamps
the timer from `/etc/signal-agent.env`).

---

## Cost (ap-south-1, approx/mo)
- EC2 t3a.small (on-demand, 24×7): **~$14**  ·  30 GB gp3: **~$2.5**  ·  public IPv4: **~$3.6**
- S3 + Secrets Manager (2 secrets) + logs: **< $2**
- ALB: **$0** (reuses jenkins-alb)

**~$18/mo.** Bump `instance_type` to `t3a.medium` only if the admin `next build`
OOMs (the box has a 2 GB swapfile as headroom).

## Notes / decisions
- **Public subnet + auto-assigned public IP, no NAT** — avoids NAT-gateway cost;
  egress via IGW. No EIP: inbound is via the ALB (targets by instance-id) and
  nothing depends on a stable IP.
- **No inbound SSH by default** — access is SSM only. Set `ssh_ingress_cidrs`
  for break-glass.
- **Code vs data split** — code ships only via Jenkins/SSM (reviewed commits);
  `inputs/` + `prompts/` are pulled fresh from `origin/main` at the start of each
  daily run, so admin tuning edits take effect next morning without a redeploy.
- **GitHub token is never persisted** — a git askpass helper fetches it from
  Secrets Manager per git operation.
