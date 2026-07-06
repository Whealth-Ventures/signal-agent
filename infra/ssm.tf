# Non-secret deploy coordination values. The Jenkins pipeline reads instance-id
# to target SSM; the box's deploy.sh reads feedback-bucket to build env files.
resource "aws_ssm_parameter" "instance_id" {
  name  = "/${var.project}/${var.env}/instance-id"
  type  = "String"
  value = aws_instance.app.id
}

resource "aws_ssm_parameter" "feedback_bucket" {
  name  = "/${var.project}/${var.env}/feedback-bucket"
  type  = "String"
  value = local.feedback_bucket
}

resource "aws_ssm_parameter" "admin_domain" {
  name  = "/${var.project}/${var.env}/admin-domain"
  type  = "String"
  value = var.admin_domain
}

# orglife-bot co-tenant: publish the shared instance-id + artifact bucket under
# orglife's own namespace so its Jenkins pipeline reads /orglife-bot/prod/*
# (it doesn't need to know the box — or the bucket — is signal-agent's).
resource "aws_ssm_parameter" "orglife_instance_id" {
  count = var.orglife_enabled ? 1 : 0
  name  = "/orglife-bot/${var.env}/instance-id"
  type  = "String"
  value = aws_instance.app.id
}

resource "aws_ssm_parameter" "orglife_artifact_bucket" {
  count = var.orglife_enabled ? 1 : 0
  name  = "/orglife-bot/${var.env}/artifact-bucket"
  type  = "String"
  value = local.feedback_bucket
}
