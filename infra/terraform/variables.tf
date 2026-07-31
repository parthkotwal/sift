variable "aws_region" {
  description = "AWS region for every showcase resource."
  type        = string
  default     = "us-west-2"

  validation {
    condition     = can(regex("^[a-z]{2}(-[a-z]+)+-[0-9]+$", var.aws_region))
    error_message = "aws_region must be a valid AWS region identifier."
  }
}

variable "project_name" {
  description = "Stable project component used in resource names and tags."
  type        = string
  default     = "sift"

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{0,18}[a-z0-9]$", var.project_name))
    error_message = "project_name must be 2-20 lowercase letters, digits, or internal hyphens, beginning with a letter."
  }
}

variable "environment" {
  description = "Short-lived environment component used in names and tags."
  type        = string
  default     = "showcase"

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{0,18}[a-z0-9]$", var.environment))
    error_message = "environment must be 2-20 lowercase letters, digits, or internal hyphens, beginning with a letter."
  }
}

variable "additional_tags" {
  description = "Optional non-sensitive tags; required ownership tags take precedence."
  type        = map(string)
  default     = {}

  validation {
    condition     = alltrue([for key, value in var.additional_tags : trimspace(key) != "" && trimspace(value) != ""])
    error_message = "additional_tags cannot contain blank keys or values."
  }
}

variable "vpc_cidr" {
  description = "Private IPv4 CIDR for the short-lived showcase VPC."
  type        = string
  default     = "10.42.0.0/20"

  validation {
    condition     = can(cidrnetmask(var.vpc_cidr)) && can(cidrsubnet(var.vpc_cidr, 4, 3))
    error_message = "vpc_cidr must be an IPv4 CIDR with room for four equal subnets."
  }
}

variable "alb_ingress_cidrs" {
  description = "IPv4 CIDRs allowed to call the public HTTP demo listener."
  type        = set(string)
  default     = ["0.0.0.0/0"]

  validation {
    condition = (
      length(var.alb_ingress_cidrs) > 0
      && alltrue([for cidr in var.alb_ingress_cidrs : can(cidrnetmask(cidr))])
    )
    error_message = "alb_ingress_cidrs must contain at least one valid IPv4 CIDR."
  }
}

variable "allow_asset_deletion" {
  description = "Allow Terraform to delete a non-empty artifact bucket and ECR repository during teardown."
  type        = bool
  default     = false
}

variable "elasticache_engine_version" {
  description = "Valkey major/minor version for the rebuildable serving cache."
  type        = string
  default     = "8.0"

  validation {
    condition     = can(regex("^8\\.[0-9]+$", var.elasticache_engine_version))
    error_message = "elasticache_engine_version must select a Valkey 8.x version."
  }
}

variable "elasticache_node_type" {
  description = "Single ElastiCache node type; resize when deployment measurements require it."
  type        = string
  default     = "cache.t4g.medium"

  validation {
    condition     = can(regex("^cache\\.[a-z0-9]+\\.[a-z0-9]+$", var.elasticache_node_type))
    error_message = "elasticache_node_type must be a cache.* ElastiCache node type."
  }
}

variable "cloudwatch_log_retention_days" {
  description = "Short retention for API and materialization task logs."
  type        = number
  default     = 3

  validation {
    condition     = contains([1, 3, 5, 7, 14, 30], var.cloudwatch_log_retention_days)
    error_message = "cloudwatch_log_retention_days must be one of 1, 3, 5, 7, 14, or 30."
  }
}

variable "deployment_image_tag" {
  description = "Immutable Git-SHA ECR tag; null leaves the ECS deployment inactive."
  type        = string
  default     = null
  nullable    = true

  validation {
    condition = (
      var.deployment_image_tag == null
      || can(regex("^[0-9a-f]{7,40}$", var.deployment_image_tag))
    )
    error_message = "deployment_image_tag must be null or a 7-40 character lowercase hexadecimal Git SHA, never latest."
  }
}

variable "artifact_generation" {
  description = "Immutable S3 artifact generation ID; null leaves the ECS deployment inactive."
  type        = string
  default     = null
  nullable    = true

  validation {
    condition = (
      var.artifact_generation == null
      || can(regex("^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$", var.artifact_generation))
    )
    error_message = "artifact_generation must be null or a safe 1-128 character immutable generation ID."
  }

  validation {
    condition = (
      (var.artifact_generation == null && var.deployment_image_tag == null)
      || (var.artifact_generation != null && var.deployment_image_tag != null)
    )
    error_message = "deployment_image_tag and artifact_generation must be set together or both left null."
  }
}

variable "fargate_cpu" {
  description = "Task-level Fargate CPU units for both API and materialization tasks."
  type        = number
  default     = 1024

  validation {
    condition     = contains([256, 512, 1024, 2048, 4096, 8192, 16384], var.fargate_cpu)
    error_message = "fargate_cpu must be a supported Fargate CPU value."
  }
}

variable "fargate_memory" {
  description = "Task-level Fargate memory in MiB for both API and materialization tasks."
  type        = number
  default     = 2048

  validation {
    condition = (
      (var.fargate_cpu == 256 && contains([512, 1024, 2048], var.fargate_memory))
      || (var.fargate_cpu == 512 && contains([1024, 2048, 3072, 4096], var.fargate_memory))
      || (var.fargate_cpu == 1024 && var.fargate_memory >= 2048 && var.fargate_memory <= 8192 && var.fargate_memory % 1024 == 0)
      || (var.fargate_cpu == 2048 && var.fargate_memory >= 4096 && var.fargate_memory <= 16384 && var.fargate_memory % 1024 == 0)
      || (var.fargate_cpu == 4096 && var.fargate_memory >= 8192 && var.fargate_memory <= 30720 && var.fargate_memory % 1024 == 0)
      || (var.fargate_cpu == 8192 && var.fargate_memory >= 16384 && var.fargate_memory <= 61440 && var.fargate_memory % 4096 == 0)
      || (var.fargate_cpu == 16384 && var.fargate_memory >= 32768 && var.fargate_memory <= 122880 && var.fargate_memory % 8192 == 0)
    )
    error_message = "fargate_memory must form a valid AWS Fargate CPU/memory combination."
  }
}

variable "fargate_cpu_architecture" {
  description = "Linux image CPU architecture; this must match the image pushed by CI."
  type        = string
  default     = "X86_64"

  validation {
    condition     = contains(["ARM64", "X86_64"], var.fargate_cpu_architecture)
    error_message = "fargate_cpu_architecture must be ARM64 or X86_64."
  }
}

variable "api_desired_count" {
  description = "API task count after activation; keep zero until materialization and skew verification pass."
  type        = number
  default     = 0

  validation {
    condition     = contains([0, 1], var.api_desired_count)
    error_message = "api_desired_count must be zero or one; this showcase has no autoscaling."
  }

  validation {
    condition = (
      var.api_desired_count == 0
      || (var.deployment_image_tag != null && var.artifact_generation != null)
    )
    error_message = "api_desired_count must remain zero while immutable deployment inputs are unset."
  }
}
