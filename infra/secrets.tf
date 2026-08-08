# Secret containers only. Terraform creates them with a placeholder; real values
# are filled in out-of-band (console / CLI) so secrets never live in state or git.
# lifecycle.ignore_changes keeps `terraform apply` from clobbering the real value
# once it's set. See infra/README.md "Fill in the secrets".

resource "aws_secretsmanager_secret" "agent" {
  name        = local.secret_agent_name
  description = "signal-agent daily digest runtime env (LLM keys + Slack)."
}

resource "aws_secretsmanager_secret_version" "agent_placeholder" {
  secret_id = aws_secretsmanager_secret.agent.id
  secret_string = jsonencode({
    OPENAI_API_KEY      = "REPLACE_ME"
    PERPLEXITY_API_KEY  = "REPLACE_ME"
    ANTHROPIC_API_KEY   = "REPLACE_ME"
    SLACK_WEBHOOK_URL   = "REPLACE_ME"
    SLACK_BOT_TOKEN     = ""
    SLACK_CHANNEL_ID    = "" # legacy single channel (used by --geo both)
    SLACK_CHANNEL_LABEL = "#signal"
    # Two-channel split (same bot). Both fall back to SLACK_CHANNEL_ID if unset.
    # The bot must be invited to each channel.
    SLACK_CHANNEL_ID_INDIA = ""
    SLACK_CHANNEL_ID_US    = ""
    # PUSH deploy: the box gets its code as an S3 artifact from Jenkins, so no
    # GitHub credential is needed here.
    #
    # SharePoint is the source of truth for inputs/ — src/sharepoint_sync.py
    # mirrors the folder down at the top of every run. Azure AD app registration
    # with app-only Graph Sites.Selected on the one site. Leave blank to disable
    # the sync (the box then runs the inputs/ baked into the artifact).
    SHAREPOINT_TENANT_ID     = ""
    SHAREPOINT_CLIENT_ID     = ""
    SHAREPOINT_CLIENT_SECRET = ""
    SHAREPOINT_SITE          = "" # e.g. contoso.sharepoint.com:/sites/SignalAgent
    SHAREPOINT_INPUTS_PATH   = "" # drive-relative folder, e.g. Signal Agent/inputs
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
    GITHUB_OWNER         = "Whealth-Ventures"
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
