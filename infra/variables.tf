# All defaults are wired to the discovered Whealth (873448587721 / ap-south-1)
# landing zone so a plain `terraform apply` works. Override in terraform.tfvars
# to move to a different account/VPC/domain.

variable "region" {
  type    = string
  default = "ap-south-1"
}

variable "project" {
  type    = string
  default = "signal-agent"
}

variable "env" {
  type    = string
  default = "prod"
}

# --- Networking (existing xponentiate-vpc) ------------------------------------

variable "vpc_id" {
  type        = string
  description = "Existing VPC to deploy into."
  default     = "vpc-0a5bc1e4b56cb8565" # xponentiate-vpc (10.1.0.0/16)
}

variable "public_subnet_id" {
  type        = string
  description = "Public subnet (routes 0.0.0.0/0 via IGW) for the instance."
  default     = "subnet-04784f83d232823a7" # xponentiate-public-1a
}

# --- Instance -----------------------------------------------------------------

variable "instance_type" {
  type    = string
  default = "t3a.small" # 2 vCPU / 2 GB; bump to t3a.medium if admin builds OOM
}

variable "root_volume_gb" {
  type    = number
  default = 30
}

variable "key_name" {
  type        = string
  description = "EC2 key pair for break-glass SSH. Routine access is via SSM."
  default     = "Whealth"
}

variable "ssh_ingress_cidrs" {
  type        = string
  description = "Comma-separated CIDRs allowed to SSH (22). Empty = SSM-only, no inbound SSH."
  default     = ""
}

# --- Admin web (reuses the existing internet-facing jenkins-alb) --------------

variable "admin_domain" {
  type    = string
  default = "signal-admin.xponentiate.com"
}

variable "admin_port" {
  type    = number
  default = 3000
}

variable "route53_zone_id" {
  type    = string
  default = "Z06861413861N9Y818YYY" # xponentiate.com.
}

variable "alb_listener_arn" {
  type        = string
  description = "ARN of the shared ALB's HTTPS:443 listener to hang the host rule off."
  default     = "arn:aws:elasticloadbalancing:ap-south-1:873448587721:listener/app/jenkins-alb/8ed874926560c820/8cf55e8d89e601ec"
}

variable "alb_security_group_id" {
  type        = string
  description = "Security group of the shared ALB (source of the :admin_port ingress)."
  default     = "sg-0cb39bdae6fc6f9bc"
}

variable "alb_listener_rule_priority" {
  type    = number
  default = 100
}

variable "wildcard_cert_arn" {
  type        = string
  description = "ACM cert to attach to the shared listener for SNI on admin_domain."
  default     = "arn:aws:acm:ap-south-1:873448587721:certificate/183e6509-22a8-4035-8ff4-8bf8ec2b3819" # *.xponentiate.com
}

# --- App source ---------------------------------------------------------------

variable "github_repo_url" {
  type    = string
  default = "https://github.com/Whealth-Ventures/signal-agent.git"
}

variable "github_branch" {
  type    = string
  default = "main"
}

# --- Schedule -----------------------------------------------------------------

# systemd OnCalendar (box is pinned to UTC). 02:20 UTC = 07:50 IST; the pipeline
# then holds until 08:00 IST (DIGEST_POST_AT) before posting.
variable "digest_oncalendar_utc" {
  type    = string
  default = "*-*-* 02:20:00" # India timer: ~07:50 IST, app holds to 08:00 IST
}

# US timer: ~11:50 UTC. main.py resolves 08:00 in America/New_York and holds to
# it, so this only needs to fire shortly before the earliest (EDT) instant.
variable "digest_oncalendar_us_utc" {
  type    = string
  default = "*-*-* 11:50:00"
}

variable "digest_post_at_ist" {
  type    = string
  default = "08:00" # HH:MM resolved per-geo in its own timezone by main.py
}

# --- orglife-bot co-tenant ----------------------------------------------------
# This box also hosts orglife-bot (a Slack Socket Mode helpdesk, separate repo,
# separate Terraform state). It runs as its OWN systemd service under a dedicated
# user, on an isolated Node 24 (the system Node 20 above lacks node:sqlite, which
# orglife-bot's ticket store needs). Deploys mirror signal-agent's: Jenkins ->
# SSM -> the box pulls the repo and runs deploy/deploy.sh. These knobs let the
# box's user_data prepare that runtime; orglife-bot's own infra/ only owns its
# Secrets Manager secret. Set orglife_enabled=false to make the box signal-only.

variable "orglife_enabled" {
  type        = bool
  description = "Prepare this box to also host orglife-bot (user, dirs, Node 24, git askpass, first deploy)."
  default     = true
}

variable "orglife_repo_url" {
  type    = string
  default = "https://github.com/Whealth-Ventures/orglife-bot.git"
}

variable "orglife_branch" {
  type    = string
  default = "main"
}

variable "orglife_secret_name" {
  type        = string
  description = "Secrets Manager secret with orglife-bot's runtime env (owned by orglife-bot/infra). The instance role is granted read on orglife-bot/prod/*."
  default     = "orglife-bot/prod/runtime"
}

# Latest 24.x is resolved at deploy time from nodejs.org; this only pins the line.
variable "orglife_node_major" {
  type    = string
  default = "24"
}
