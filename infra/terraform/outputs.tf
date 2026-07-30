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

output "vpc_id" {
  description = "VPC containing the showcase deployment."
  value       = aws_vpc.this.id
}

output "availability_zones" {
  description = "Availability zones selected for the two-subnet tiers."
  value       = [for subnet in values(local.public_subnets) : subnet.availability_zone]
}

output "public_subnet_ids" {
  description = "Public subnet IDs for the ALB and public-IP Fargate tasks."
  value       = [for subnet in values(aws_subnet.public) : subnet.id]
}

output "private_subnet_ids" {
  description = "Private subnet IDs for the ElastiCache subnet group."
  value       = [for subnet in values(aws_subnet.private) : subnet.id]
}

output "security_group_ids" {
  description = "Security groups consumed by later ALB, ECS, and Redis resources."
  value = {
    alb       = aws_security_group.alb.id
    ecs_tasks = aws_security_group.ecs_tasks.id
    redis     = aws_security_group.redis.id
  }
}

output "artifact_bucket" {
  description = "Private S3 bucket holding immutable serving generations."
  value = {
    arn    = aws_s3_bucket.artifacts.arn
    name   = aws_s3_bucket.artifacts.id
    prefix = local.artifact_prefix
  }
}

output "ecr_repository" {
  description = "Private ECR repository consumed by CI and ECS."
  value = {
    arn  = aws_ecr_repository.api.arn
    name = aws_ecr_repository.api.name
    url  = aws_ecr_repository.api.repository_url
  }
}

output "redis_connection" {
  description = "Private TLS connection information for ECS task configuration."
  sensitive   = true
  value = {
    endpoint = aws_elasticache_replication_group.redis.primary_endpoint_address
    port     = aws_elasticache_replication_group.redis.port
    url      = "rediss://${aws_elasticache_replication_group.redis.primary_endpoint_address}:${aws_elasticache_replication_group.redis.port}/0"
  }
}

output "task_log_groups" {
  description = "CloudWatch log groups consumed by the later ECS task definitions."
  value       = { for purpose, log_group in aws_cloudwatch_log_group.tasks : purpose => log_group.name }
}
