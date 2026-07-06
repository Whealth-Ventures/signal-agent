# Secret containers only. Terraform creates them with a placeholder; real values
# are filled in out-of-band (console / CLI) so secrets never live in state or git.
# lifecycle.ignore_changes keeps `terraform apply` from clobbering the real value
# once it's set. See infra/README.md "Fill in the secrets".

resource "aws_secretsmanager_secret" "agent" {
  name        = local.secret_agent_name
  description = "signal-agent daily digest runtime env (LLM keys, Slack, GitHub read PAT)."
}

resource "aws_secretsmanager_secret_version" "agent_placeholder" {
  secret_id = aws_secretsmanager_secret.agent.id
  secret_string = jsonencode({
    OPENAI_API_KEY      = "REPLACE_ME"
    PERPLEXITY_API_KEY  = "REPLACE_ME"
    ANTHROPIC_API_KEY   = "REPLACE_ME"
    SLACK_WEBHOOK_URL   = "REPLACE_ME"
    SLACK_BOT_TOKEN     = ""
    SLACK_CHANNEL_ID    = ""
    SLACK_CHANNEL_LABEL = "#signal"
    # Read-only fine-grained PAT (Contents: Read on ashwinknan/signal-agent).
    # Used to clone the private repo and to pull inputs/ + prompts/ each run.
    GITHUB_TOKEN = "REPLACE_ME"
  })

  lifecycle {
    ignore_changes = [secret_string]
  }
}

resource "aws_secretsmanager_secret" "admin" {
  name        = local.secret_admin_name
  description = "signal-agent admin UI runtime env (GitHub PAT, auth, Slack signing secret)."
}

resource "aws_secretsmanager_secret_version" "admin_placeholder" {
  secret_id = aws_secretsmanager_secret.admin.id
  secret_string = jsonencode({
    GITHUB_TOKEN         = "REPLACE_ME" # Contents: Read & Write (admin commits tuning/prompts)
    GITHUB_OWNER         = "ashwinknan"
    GITHUB_REPO          = "signal-agent"
    GITHUB_BRANCH        = "main"
    GIT_COMMIT_EMAIL     = "signal-agent@whealthventures.com"
    AUTH_SECRET          = "REPLACE_ME" # openssl rand -hex 32
    ADMIN_USER           = "admin"
    ADMIN_PWD            = "REPLACE_ME"
    SLACK_SIGNING_SECRET = "REPLACE_ME"
  })

  lifecycle {
    ignore_changes = [secret_string]
  }
}
