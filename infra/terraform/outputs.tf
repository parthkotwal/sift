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
