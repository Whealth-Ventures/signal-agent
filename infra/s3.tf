# Feedback + backups bucket. Replaces Vercel Blob:
#   events/   — one JSON object per Slack reaction event (written by the admin
#               app's /api/slack/events receiver, read by the admin Suggestions
#               page and by src/feedback_puller.py).
#   state/    — nightly tar backups of data/ (SQLite + Chroma) for DR.
resource "aws_s3_bucket" "feedback" {
  bucket = local.feedback_bucket
}

resource "aws_s3_bucket_public_access_block" "feedback" {
  bucket                  = aws_s3_bucket.feedback.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "feedback" {
  bucket = aws_s3_bucket.feedback.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_versioning" "feedback" {
  bucket = aws_s3_bucket.feedback.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "feedback" {
  bucket = aws_s3_bucket.feedback.id

  rule {
    id     = "expire-old-events"
    status = "Enabled"
    filter { prefix = "events/" }
    expiration { days = 180 }
    noncurrent_version_expiration { noncurrent_days = 30 }
  }

  rule {
    id     = "retain-few-state-backups"
    status = "Enabled"
    filter { prefix = "state/" }
    noncurrent_version_expiration { noncurrent_days = 14 }
  }

  # Deploy artifacts (PUSH model). Keep ~30 days of per-commit tarballs for
  # rollback; expire superseded latest.tgz versions quickly.
  rule {
    id     = "expire-old-artifacts"
    status = "Enabled"
    filter { prefix = "artifacts/" }
    expiration { days = 30 }
    noncurrent_version_expiration { noncurrent_days = 7 }
  }
}
