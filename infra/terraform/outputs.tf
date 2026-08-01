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
    alb              = aws_security_group.alb.id
    benchmark_client = aws_security_group.benchmark.id
    ecs_tasks        = aws_security_group.ecs_tasks.id
    redis            = aws_security_group.redis.id
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

output "ecs_role_arns" {
  description = "Least-privilege IAM roles consumed by the later ECS task definitions."
  value = {
    execution = aws_iam_role.ecs_execution.arn
    task      = aws_iam_role.ecs_task.arn
  }
}

output "alb" {
  description = "Public ALB endpoint and resources consumed by deployment and validation."
  value = {
    arn               = aws_lb.api.arn
    dns_name          = aws_lb.api.dns_name
    hosted_zone_id    = aws_lb.api.zone_id
    http_listener_arn = aws_lb_listener.http.arn
    target_group_arn  = aws_lb_target_group.api.arn
  }
}

output "https_endpoint" {
  description = "Restricted CloudFront HTTPS endpoint for the showcase API."
  value       = "https://${aws_cloudfront_distribution.api.domain_name}"
}

output "ecs_cluster" {
  description = "ECS cluster used by the API service and one-off materialization tasks."
  value = {
    arn  = aws_ecs_cluster.this.arn
    name = aws_ecs_cluster.this.name
  }
}

output "ecs_deployment" {
  description = "ECS identifiers; task definitions and service are null until immutable inputs activate them."
  value = {
    active                          = local.deployment_activated
    api_task_definition_arn         = try(aws_ecs_task_definition.api["active"].arn, null)
    materialize_task_definition_arn = try(aws_ecs_task_definition.materialize["active"].arn, null)
    service_name                    = try(aws_ecs_service.api["active"].name, null)
  }
}

output "ecs_run_task_network" {
  description = "Network inputs for an ECS RunTask materialization invocation."
  value = {
    assign_public_ip = true
    security_group_ids = [
      aws_security_group.ecs_tasks.id,
    ]
    subnet_ids = values(aws_subnet.public)[*].id
  }
}

output "benchmark_run_task_network" {
  description = "Network inputs for short-lived ECS benchmark clients that can reach only the ALB and AWS HTTPS endpoints."
  value = {
    assign_public_ip = true
    security_group_ids = [
      aws_security_group.benchmark.id,
    ]
    subnet_ids = values(aws_subnet.public)[*].id
  }
}
