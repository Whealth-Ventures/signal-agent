# Terraform + provider version pins and remote state backend.
#
# State lives in the account's existing shared state bucket. S3 native locking
# (use_lockfile) is used instead of a DynamoDB lock table — supported by the
# S3 backend since Terraform 1.11 (this account runs 1.15.x).
terraform {
  required_version = ">= 1.11"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.60"
    }
  }

  backend "s3" {
    bucket       = "whealth-tf-state-s3"
    key          = "signal-agent/prod/terraform.tfstate"
    region       = "ap-south-1"
    encrypt      = true
    use_lockfile = true
  }
}
