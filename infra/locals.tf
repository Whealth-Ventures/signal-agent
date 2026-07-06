locals {
  name = "${var.project}-${var.env}" # signal-agent-prod

  # Global-unique bucket for feedback events + state backups.
  feedback_bucket = "${local.name}-feedback-${data.aws_caller_identity.current.account_id}"

  ssh_cidrs = var.ssh_ingress_cidrs == "" ? [] : split(",", var.ssh_ingress_cidrs)

  secret_agent_name = "${var.project}/${var.env}/agent-env"
  secret_admin_name = "${var.project}/${var.env}/admin-env"

  app_dir  = "/opt/signal-agent"
  app_user = "signal"

  # orglife-bot co-tenant (see variables.tf "orglife-bot co-tenant").
  orglife_app_dir  = "/opt/orglife-bot"
  orglife_app_user = "orglife"
  # ARN pattern for the instance-role grant (secret is created by orglife-bot/infra).
  orglife_secret_arn_glob = "arn:aws:secretsmanager:${var.region}:${data.aws_caller_identity.current.account_id}:secret:orglife-bot/${var.env}/*"
}
