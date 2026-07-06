provider "aws" {
  region = var.region

  default_tags {
    tags = {
      Project   = var.project
      Env       = var.env
      ManagedBy = "terraform"
      Repo      = "Whealth-Ventures/signal-agent"
      Owner     = "himanshu.khutiyare"
    }
  }
}
