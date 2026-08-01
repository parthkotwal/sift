# Short-lived diagnostic for the restricted public HTTP path. This records only
# rejected flows and does not alter any route, security group, or network ACL.
resource "aws_cloudwatch_log_group" "vpc_rejects" {
  name                        = "/vpc/${local.name_prefix}/rejects"
  retention_in_days           = 1
  log_group_class             = "STANDARD"
  deletion_protection_enabled = false
  skip_destroy                = false
}

resource "aws_iam_role" "vpc_flow_logs" {
  name = "${local.name_prefix}-vpc-flow-logs"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Principal = {
          Service = "vpc-flow-logs.amazonaws.com"
        }
        Action = "sts:AssumeRole"
        Condition = {
          StringEquals = {
            "aws:SourceAccount" = data.aws_caller_identity.current.account_id
          }
          ArnLike = {
            "aws:SourceArn" = "arn:${data.aws_partition.current.partition}:ec2:${var.aws_region}:${data.aws_caller_identity.current.account_id}:vpc-flow-log/*"
          }
        }
      },
    ]
  })
}

resource "aws_iam_role_policy" "vpc_flow_logs" {
  name = "${local.name_prefix}-publish-vpc-flow-logs"
  role = aws_iam_role.vpc_flow_logs.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "PublishToDiagnosticLogGroup"
        Effect = "Allow"
        Action = [
          "logs:CreateLogStream",
          "logs:PutLogEvents",
        ]
        Resource = "${aws_cloudwatch_log_group.vpc_rejects.arn}:*"
      },
      {
        Sid    = "DescribeLogDelivery"
        Effect = "Allow"
        Action = [
          "logs:DescribeLogGroups",
          "logs:DescribeLogStreams",
        ]
        Resource = "*"
      },
    ]
  })
}

resource "aws_flow_log" "vpc_rejects" {
  iam_role_arn             = aws_iam_role.vpc_flow_logs.arn
  log_destination          = aws_cloudwatch_log_group.vpc_rejects.arn
  log_destination_type     = "cloud-watch-logs"
  log_format               = "$${version} $${account-id} $${interface-id} $${srcaddr} $${dstaddr} $${srcport} $${dstport} $${protocol} $${packets} $${bytes} $${start} $${end} $${action} $${log-status} $${flow-direction} $${traffic-path}"
  max_aggregation_interval = 60
  traffic_type             = "REJECT"
  vpc_id                   = aws_vpc.this.id

  depends_on = [aws_iam_role_policy.vpc_flow_logs]

  tags = {
    Name    = "${local.name_prefix}-vpc-rejects"
    Purpose = "short-lived-public-path-diagnostic"
  }
}
