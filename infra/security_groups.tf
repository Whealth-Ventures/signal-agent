resource "aws_security_group" "instance" {
  name        = "${local.name}-ec2"
  description = "signal-agent EC2: admin port from ALB only, egress all, SSM for access."
  vpc_id      = var.vpc_id

  tags = { Name = "${local.name}-ec2" }
}

# Admin UI traffic arrives only from the shared ALB's security group.
resource "aws_security_group_rule" "admin_from_alb" {
  type                     = "ingress"
  from_port                = var.admin_port
  to_port                  = var.admin_port
  protocol                 = "tcp"
  security_group_id        = aws_security_group.instance.id
  source_security_group_id = var.alb_security_group_id
  description              = "Next.js admin from jenkins-alb"
}

# Optional break-glass SSH — empty by default (use SSM Session Manager instead).
resource "aws_security_group_rule" "ssh" {
  count             = length(local.ssh_cidrs) > 0 ? 1 : 0
  type              = "ingress"
  from_port         = 22
  to_port           = 22
  protocol          = "tcp"
  security_group_id = aws_security_group.instance.id
  cidr_blocks       = local.ssh_cidrs
  description       = "break-glass SSH"
}

resource "aws_security_group_rule" "egress_all" {
  type              = "egress"
  from_port         = 0
  to_port           = 0
  protocol          = "-1"
  security_group_id = aws_security_group.instance.id
  cidr_blocks       = ["0.0.0.0/0"]
  description       = "outbound to LLM/Slack/GitHub/AWS APIs"
}
