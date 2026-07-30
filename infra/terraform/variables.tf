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
