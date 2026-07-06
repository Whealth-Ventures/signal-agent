# --- EC2 instance role --------------------------------------------------------

data "aws_iam_policy_document" "ec2_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "instance" {
  name               = "${local.name}-ec2"
  assume_role_policy = data.aws_iam_policy_document.ec2_assume.json
}

# SSM Session Manager + Run Command (deploys, break-glass shell) — no SSH needed.
resource "aws_iam_role_policy_attachment" "ssm_core" {
  role       = aws_iam_role.instance.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

data "aws_iam_policy_document" "instance" {
  statement {
    sid       = "ReadRuntimeSecrets"
    actions   = ["secretsmanager:GetSecretValue"]
    resources = [aws_secretsmanager_secret.agent.arn, aws_secretsmanager_secret.admin.arn]
  }

  statement {
    sid       = "ReadDeployParams"
    actions   = ["ssm:GetParameter", "ssm:GetParameters"]
    resources = ["arn:aws:ssm:${var.region}:${data.aws_caller_identity.current.account_id}:parameter/${var.project}/${var.env}/*"]
  }

  statement {
    sid       = "FeedbackBucketList"
    actions   = ["s3:ListBucket"]
    resources = [aws_s3_bucket.feedback.arn]
  }

  statement {
    sid       = "FeedbackBucketObjects"
    actions   = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"]
    resources = ["${aws_s3_bucket.feedback.arn}/*"]
  }
}

resource "aws_iam_role_policy" "instance" {
  name   = "${local.name}-instance"
  role   = aws_iam_role.instance.id
  policy = data.aws_iam_policy_document.instance.json
}

resource "aws_iam_instance_profile" "instance" {
  name = "${local.name}-ec2"
  role = aws_iam_role.instance.name
}

# --- Deploy policy for the Jenkins principal ----------------------------------
# Attach this to whatever role/user your Jenkins runs as so the pipeline can
# push a deploy via SSM. Output: signal_agent_jenkins_deploy_policy_arn.

data "aws_iam_policy_document" "jenkins_deploy" {
  statement {
    sid     = "SendDeployCommand"
    actions = ["ssm:SendCommand"]
    resources = [
      aws_instance.app.arn,
      "arn:aws:ssm:${var.region}::document/AWS-RunShellScript",
    ]
  }
  statement {
    sid       = "ReadCommandResult"
    actions   = ["ssm:GetCommandInvocation", "ssm:ListCommandInvocations", "ssm:ListCommands"]
    resources = ["*"]
  }
  statement {
    sid       = "ResolveInstanceId"
    actions   = ["ssm:GetParameter"]
    resources = ["arn:aws:ssm:${var.region}:${data.aws_caller_identity.current.account_id}:parameter/${var.project}/${var.env}/*"]
  }
}

resource "aws_iam_policy" "jenkins_deploy" {
  name        = "${local.name}-jenkins-deploy"
  description = "Lets Jenkins deploy signal-agent via SSM Run Command."
  policy      = data.aws_iam_policy_document.jenkins_deploy.json
}
