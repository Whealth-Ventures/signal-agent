# Reuse the existing internet-facing jenkins-alb (same VPC) instead of paying for
# a dedicated ALB. We only ADD child resources to its HTTPS:443 listener:
#   - a target group pointing at the admin port on this instance
#   - the *.xponentiate.com cert (SNI) so admin_domain serves a valid cert
#   - a host-header rule routing admin_domain -> the target group
# The listener's default action (jenkins) is untouched.

resource "aws_lb_target_group" "admin" {
  name        = "${local.name}-admin"
  port        = var.admin_port
  protocol    = "HTTP"
  vpc_id      = var.vpc_id
  target_type = "instance"

  health_check {
    path                = "/login" # public route (middleware allowlist) -> 200
    matcher             = "200"
    interval            = 30
    healthy_threshold   = 2
    unhealthy_threshold = 3
    timeout             = 5
  }
}

resource "aws_lb_target_group_attachment" "admin" {
  target_group_arn = aws_lb_target_group.admin.arn
  target_id        = aws_instance.app.id
  port             = var.admin_port
}

resource "aws_lb_listener_certificate" "admin" {
  listener_arn    = var.alb_listener_arn
  certificate_arn = var.wildcard_cert_arn
}

resource "aws_lb_listener_rule" "admin" {
  listener_arn = var.alb_listener_arn
  priority     = var.alb_listener_rule_priority

  action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.admin.arn
  }

  condition {
    host_header {
      values = [var.admin_domain]
    }
  }
}
