output "instance_id" {
  value       = aws_instance.app.id
  description = "Deploy target for SSM."
}

output "public_ip" {
  value       = aws_instance.app.public_ip
  description = "Auto-assigned public IP (egress via IGW). Not stable across stop/start."
}

output "admin_url" {
  value       = "https://${var.admin_domain}"
  description = "Admin UI (after secrets are filled + first deploy)."
}

output "slack_events_url" {
  value       = "https://${var.admin_domain}/api/slack/events"
  description = "Set this as the Slack app Event Subscriptions Request URL."
}

output "feedback_bucket" {
  value       = aws_s3_bucket.feedback.bucket
  description = "S3 bucket backing feedback events + state backups (replaces Vercel Blob)."
}

output "agent_secret_name" {
  value       = aws_secretsmanager_secret.agent.name
  description = "Fill this JSON with real runtime values."
}

output "admin_secret_name" {
  value       = aws_secretsmanager_secret.admin.name
  description = "Fill this JSON with real admin values."
}

output "jenkins_deploy_policy_arn" {
  value       = aws_iam_policy.jenkins_deploy.arn
  description = "Attach to the Jenkins role/user. Covers BOTH signal-agent and the orglife-bot co-tenant (same box, same SendCommand target)."
}

output "orglife_instance_id_param" {
  value       = var.orglife_enabled ? aws_ssm_parameter.orglife_instance_id[0].name : null
  description = "SSM param orglife-bot's Jenkins reads for the shared instance-id."
}
