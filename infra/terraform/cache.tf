locals {
  task_log_groups = {
    api         = "/ecs/${local.name_prefix}/api"
    materialize = "/ecs/${local.name_prefix}/materialize"
  }
}

resource "aws_elasticache_subnet_group" "redis" {
  name        = "${local.name_prefix}-redis"
  description = "Private subnets for Sift's rebuildable serving cache"
  subnet_ids  = values(aws_subnet.private)[*].id
}

resource "aws_elasticache_replication_group" "redis" {
  replication_group_id = "${local.name_prefix}-redis"
  description          = "Rebuildable Sift serving state"

  engine         = "valkey"
  engine_version = var.elasticache_engine_version
  node_type      = var.elasticache_node_type
  port           = 6379

  num_cache_clusters         = 1
  automatic_failover_enabled = false
  multi_az_enabled           = false

  subnet_group_name  = aws_elasticache_subnet_group.redis.name
  security_group_ids = [aws_security_group.redis.id]

  at_rest_encryption_enabled = true
  transit_encryption_enabled = true
  transit_encryption_mode    = "required"

  snapshot_retention_limit   = 0
  auto_minor_version_upgrade = true
  apply_immediately          = true
}

resource "aws_cloudwatch_log_group" "tasks" {
  for_each = local.task_log_groups

  name                        = each.value
  retention_in_days           = var.cloudwatch_log_retention_days
  log_group_class             = "STANDARD"
  deletion_protection_enabled = false
  skip_destroy                = false
}
