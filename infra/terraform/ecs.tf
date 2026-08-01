locals {
  deployment_activated = var.deployment_image_tag != null && var.artifact_generation != null
  deployment_resources = local.deployment_activated ? { active = true } : {}
  container_image = local.deployment_activated ? (
    "${data.aws_caller_identity.current.account_id}.dkr.ecr.${var.aws_region}.${data.aws_partition.current.dns_suffix}/${aws_ecr_repository.api.name}:${var.deployment_image_tag}"
  ) : null
  redis_url = "rediss://${aws_elasticache_replication_group.redis.primary_endpoint_address}:${aws_elasticache_replication_group.redis.port}/0"
  task_environment = local.deployment_activated ? concat([
    {
      name  = "SIFT_ARTIFACT_BUCKET"
      value = aws_s3_bucket.artifacts.id
    },
    {
      name  = "SIFT_ARTIFACT_GENERATION"
      value = var.artifact_generation
    },
    {
      name  = "SIFT_REDIS_URL"
      value = local.redis_url
    },
    ], var.openblas_num_threads == null ? [] : [
    {
      name  = "OPENBLAS_NUM_THREADS"
      value = tostring(var.openblas_num_threads)
    },
  ]) : []
}

resource "aws_ecs_cluster" "this" {
  name = local.name_prefix

  setting {
    name  = "containerInsights"
    value = "disabled"
  }
}

resource "aws_ecs_task_definition" "api" {
  for_each = local.deployment_resources

  family                   = "${local.name_prefix}-api"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = tostring(var.fargate_cpu)
  memory                   = tostring(var.fargate_memory)
  execution_role_arn       = aws_iam_role.ecs_execution.arn
  task_role_arn            = aws_iam_role.ecs_task.arn

  runtime_platform {
    cpu_architecture        = var.fargate_cpu_architecture
    operating_system_family = "LINUX"
  }

  container_definitions = jsonencode([
    {
      name      = "api"
      image     = local.container_image
      essential = true
      command = [
        "python",
        "-m",
        "scripts.runtime_probe",
        "--",
        "uvicorn",
        "sift.api.main:app",
        "--host",
        "0.0.0.0",
        "--port",
        "8000",
        "--workers",
        tostring(var.api_worker_count),
        "--http",
        "h11",
        "--timeout-keep-alive",
        "65",
        "--log-config",
        "/app/scripts/uvicorn_log_config.json",
      ]
      environment = local.task_environment
      portMappings = [
        {
          name          = "http"
          containerPort = 8000
          hostPort      = 8000
          protocol      = "tcp"
          appProtocol   = "http"
        },
      ]
      logConfiguration = {
        logDriver = "awslogs"
        options = {
          awslogs-group         = aws_cloudwatch_log_group.tasks["api"].name
          awslogs-region        = var.aws_region
          awslogs-stream-prefix = "api"
        }
      }
      stopTimeout = 60
    },
  ])
}

resource "aws_ecs_task_definition" "materialize" {
  for_each = local.deployment_resources

  family                   = "${local.name_prefix}-materialize"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = tostring(var.fargate_cpu)
  memory                   = tostring(var.fargate_memory)
  execution_role_arn       = aws_iam_role.ecs_execution.arn
  task_role_arn            = aws_iam_role.ecs_task.arn

  runtime_platform {
    cpu_architecture        = var.fargate_cpu_architecture
    operating_system_family = "LINUX"
  }

  container_definitions = jsonencode([
    {
      name      = "materialize"
      image     = local.container_image
      essential = true
      command = [
        "/bin/sh",
        "-c",
        "python -m sift.store.online && python -m sift.store.skew --sample-size 100",
      ]
      environment = local.task_environment
      logConfiguration = {
        logDriver = "awslogs"
        options = {
          awslogs-group         = aws_cloudwatch_log_group.tasks["materialize"].name
          awslogs-region        = var.aws_region
          awslogs-stream-prefix = "materialize"
        }
      }
      stopTimeout = 60
    },
  ])
}

resource "aws_ecs_service" "api" {
  for_each = local.deployment_resources

  name                               = "${local.name_prefix}-api"
  cluster                            = aws_ecs_cluster.this.arn
  task_definition                    = aws_ecs_task_definition.api[each.key].arn
  desired_count                      = var.api_desired_count
  launch_type                        = "FARGATE"
  platform_version                   = "1.4.0"
  health_check_grace_period_seconds  = 180
  deployment_minimum_healthy_percent = 0
  deployment_maximum_percent         = 100
  enable_ecs_managed_tags            = true
  propagate_tags                     = "SERVICE"
  wait_for_steady_state              = true

  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }

  network_configuration {
    assign_public_ip = true
    security_groups  = [aws_security_group.ecs_tasks.id]
    subnets          = values(aws_subnet.public)[*].id
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.api.arn
    container_name   = "api"
    container_port   = 8000
  }

  depends_on = [aws_lb_listener.http]
}
