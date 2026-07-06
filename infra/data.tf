data "aws_caller_identity" "current" {}

data "aws_region" "current" {}

# Latest Amazon Linux 2023 AMI (x86_64) — resolved at plan time from SSM so the
# box always launches on a current, patched image.
data "aws_ssm_parameter" "al2023" {
  name = "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64"
}

data "aws_vpc" "this" {
  id = var.vpc_id
}

data "aws_subnet" "public" {
  id = var.public_subnet_id
}
