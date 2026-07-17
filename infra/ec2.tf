resource "aws_instance" "app" {
  ami                    = data.aws_ssm_parameter.al2023.value
  instance_type          = var.instance_type
  subnet_id              = var.public_subnet_id
  vpc_security_group_ids = [aws_security_group.instance.id]
  iam_instance_profile   = aws_iam_instance_profile.instance.name
  key_name               = var.key_name

  # Public subnet's default route is the IGW (no NAT), and the IGW only NATs
  # instances that HAVE a public IP — so this auto-assigned public IP is what
  # gives the box outbound internet. No EIP needed: inbound is via the ALB
  # (targets by instance-id) and nothing depends on a stable IP.
  associate_public_ip_address = true

  root_block_device {
    volume_size           = var.root_volume_gb
    volume_type           = "gp3"
    encrypted             = true
    delete_on_termination = true
  }

  metadata_options {
    http_tokens   = "required" # IMDSv2 only
    http_endpoint = "enabled"
  }

  user_data = templatefile("${path.module}/user_data.sh.tftpl", {
    region          = var.region
    app_dir         = local.app_dir
    app_user        = local.app_user
    repo_url        = var.github_repo_url
    branch          = var.github_branch
    agent_secret    = local.secret_agent_name
    admin_secret    = local.secret_admin_name
    feedback_bucket = local.feedback_bucket
    admin_port      = var.admin_port
    digest_post_at     = var.digest_post_at_ist
    digest_calendar    = var.digest_oncalendar_utc
    digest_calendar_us = var.digest_oncalendar_us_utc
    admin_domain       = var.admin_domain

    # orglife-bot co-tenant (prepared only when orglife_enabled).
    orglife_enabled    = var.orglife_enabled
    orglife_app_dir    = local.orglife_app_dir
    orglife_app_user   = local.orglife_app_user
    orglife_repo_url   = var.orglife_repo_url
    orglife_branch     = var.orglife_branch
    orglife_secret     = var.orglife_secret_name
    orglife_node_major = var.orglife_node_major
  })
  # Changing user_data alone won't re-run it on an existing box; deploys go
  # through SSM (deploy.sh). Ignore so tweaks don't force a replace.
  lifecycle {
    ignore_changes = [user_data, ami]
  }

  tags = { Name = local.name }
}
