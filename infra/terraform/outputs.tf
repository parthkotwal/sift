output "aws_region" {
  description = "AWS region selected for the showcase."
  value       = var.aws_region
}

output "deployment_name" {
  description = "Shared prefix for resources created by later slices."
  value       = local.name_prefix
}

output "common_tags" {
  description = "Tags the AWS provider applies to supported resources."
  value       = local.common_tags
}
