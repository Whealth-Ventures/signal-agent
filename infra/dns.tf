# Alias admin_domain -> the shared ALB. Look the ALB up by name so we get its
# DNS name + hosted zone for the A-alias record.
data "aws_lb" "shared" {
  name = "jenkins-alb"
}

resource "aws_route53_record" "admin" {
  zone_id = var.route53_zone_id
  name    = var.admin_domain
  type    = "A"

  alias {
    name                   = data.aws_lb.shared.dns_name
    zone_id                = data.aws_lb.shared.zone_id
    evaluate_target_health = false
  }
}
