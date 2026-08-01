terraform {
  required_version = "~> 1.15.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.57.0"
    }

    random = {
      source  = "hashicorp/random"
      version = "~> 3.8.0"
    }
  }
}
