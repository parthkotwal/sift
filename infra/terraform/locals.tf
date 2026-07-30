locals {
  name_prefix = "${var.project_name}-${var.environment}"

  common_tags = merge(
    var.additional_tags,
    {
      Environment = var.environment
      Ephemeral   = "true"
      ManagedBy   = "Terraform"
      Project     = var.project_name
      Repository  = "sift"
    },
  )
}
