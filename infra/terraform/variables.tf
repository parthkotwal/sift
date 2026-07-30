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
  default     = "cache.t4g.micro"

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
