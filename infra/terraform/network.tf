data "aws_availability_zones" "available" {
  state = "available"
}

check "two_availability_zones" {
  assert {
    condition     = length(data.aws_availability_zones.available.names) >= local.network_az_count
    error_message = "The selected AWS region must expose at least two available Availability Zones."
  }
}

resource "aws_vpc" "this" {
  cidr_block           = var.vpc_cidr
  enable_dns_hostnames = true
  enable_dns_support   = true

  tags = {
    Name = local.name_prefix
  }
}

resource "aws_internet_gateway" "this" {
  vpc_id = aws_vpc.this.id

  tags = {
    Name = "${local.name_prefix}-igw"
  }
}

resource "aws_subnet" "public" {
  for_each = local.public_subnets

  availability_zone       = each.value.availability_zone
  cidr_block              = each.value.cidr_block
  map_public_ip_on_launch = false
  vpc_id                  = aws_vpc.this.id

  tags = {
    Name = "${local.name_prefix}-public-${each.value.availability_zone}"
    Tier = "public"
  }
}

resource "aws_subnet" "private" {
  for_each = local.private_subnets

  availability_zone       = each.value.availability_zone
  cidr_block              = each.value.cidr_block
  map_public_ip_on_launch = false
  vpc_id                  = aws_vpc.this.id

  tags = {
    Name = "${local.name_prefix}-private-${each.value.availability_zone}"
    Tier = "private"
  }
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.this.id

  tags = {
    Name = "${local.name_prefix}-public"
  }
}

resource "aws_route" "public_internet" {
  destination_cidr_block = "0.0.0.0/0"
  gateway_id             = aws_internet_gateway.this.id
  route_table_id         = aws_route_table.public.id
}

resource "aws_route_table_association" "public" {
  for_each = aws_subnet.public

  route_table_id = aws_route_table.public.id
  subnet_id      = each.value.id
}

resource "aws_route_table" "private" {
  vpc_id = aws_vpc.this.id

  tags = {
    Name = "${local.name_prefix}-private"
  }
}

resource "aws_route_table_association" "private" {
  for_each = aws_subnet.private

  route_table_id = aws_route_table.private.id
  subnet_id      = each.value.id
}

resource "aws_security_group" "alb" {
  name_prefix            = "${local.name_prefix}-alb-"
  description            = "Public HTTP entrypoint for the Sift demo"
  revoke_rules_on_delete = true
  vpc_id                 = aws_vpc.this.id

  tags = {
    Name = "${local.name_prefix}-alb"
  }
}

resource "aws_vpc_security_group_ingress_rule" "alb_http" {
  for_each = var.alb_ingress_cidrs

  security_group_id = aws_security_group.alb.id
  cidr_ipv4         = each.value
  description       = "Public demo HTTP"
  from_port         = 80
  ip_protocol       = "tcp"
  to_port           = 80
}

resource "aws_security_group" "ecs_tasks" {
  name_prefix            = "${local.name_prefix}-ecs-"
  description            = "Fargate API and materialization task networking"
  revoke_rules_on_delete = true
  vpc_id                 = aws_vpc.this.id

  tags = {
    Name = "${local.name_prefix}-ecs"
  }
}

resource "aws_vpc_security_group_ingress_rule" "ecs_from_alb" {
  security_group_id            = aws_security_group.ecs_tasks.id
  referenced_security_group_id = aws_security_group.alb.id
  description                  = "ALB to the Sift API"
  from_port                    = 8000
  ip_protocol                  = "tcp"
  to_port                      = 8000
}

resource "aws_vpc_security_group_egress_rule" "alb_to_ecs" {
  security_group_id            = aws_security_group.alb.id
  referenced_security_group_id = aws_security_group.ecs_tasks.id
  description                  = "ALB health checks and requests to Sift"
  from_port                    = 8000
  ip_protocol                  = "tcp"
  to_port                      = 8000
}

resource "aws_vpc_security_group_egress_rule" "ecs_https" {
  security_group_id = aws_security_group.ecs_tasks.id
  cidr_ipv4         = "0.0.0.0/0"
  description       = "ECR, S3, CloudWatch, and AWS control-plane HTTPS"
  from_port         = 443
  ip_protocol       = "tcp"
  to_port           = 443
}

resource "aws_security_group" "redis" {
  name_prefix            = "${local.name_prefix}-redis-"
  description            = "Private rebuildable Redis serving state"
  revoke_rules_on_delete = true
  vpc_id                 = aws_vpc.this.id

  tags = {
    Name = "${local.name_prefix}-redis"
  }
}

resource "aws_vpc_security_group_egress_rule" "ecs_to_redis" {
  security_group_id            = aws_security_group.ecs_tasks.id
  referenced_security_group_id = aws_security_group.redis.id
  description                  = "Sift tasks to Redis"
  from_port                    = 6379
  ip_protocol                  = "tcp"
  to_port                      = 6379
}

resource "aws_vpc_security_group_ingress_rule" "redis_from_ecs" {
  security_group_id            = aws_security_group.redis.id
  referenced_security_group_id = aws_security_group.ecs_tasks.id
  description                  = "Sift tasks to Redis"
  from_port                    = 6379
  ip_protocol                  = "tcp"
  to_port                      = 6379
}
