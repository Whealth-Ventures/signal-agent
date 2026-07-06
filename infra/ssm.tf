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
