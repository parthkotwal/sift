locals {
  name_prefix      = "${var.project_name}-${var.environment}"
  network_az_count = 2

  public_subnets = {
    for index in range(local.network_az_count) : tostring(index) => {
      availability_zone = data.aws_availability_zones.available.names[index]
      cidr_block        = cidrsubnet(var.vpc_cidr, 4, index)
    }
  }

  private_subnets = {
    for index in range(local.network_az_count) : tostring(index) => {
      availability_zone = data.aws_availability_zones.available.names[index]
      cidr_block        = cidrsubnet(var.vpc_cidr, 4, index + local.network_az_count)
    }
  }

  common_tags = merge(
    var.additional_tags,
    {
      Environment = var.environment
      Ephemeral   = "true"
      ManagedBy   = "Terraform"
      Project     = var.project_name
      Repository  = "sift"
    },
  )
}
