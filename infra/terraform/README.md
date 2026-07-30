# Sift showcase infrastructure

This directory is the Terraform root for the short-lived Sift AWS showcase.
`.agents/AWS_DEPLOYMENT_PLAN.md` defines the architecture and
`.agents/AWS_DEPLOYMENT_STATUS.md` records implementation status and evidence.

## Safety boundary

- The state backend is intentionally local for this one-day environment.
- State and plan files are gitignored. Keep local state until `terraform destroy`
  succeeds and teardown has been verified.
- `terraform init`, `fmt`, and `validate` do not create AWS resources.
- Inspect a saved plan before any `terraform apply`.
- Never put credentials, Yelp data, derived artifacts, or private endpoints in
  Terraform variables or tracked files.

## Foundation commands

```bash
terraform -chdir=infra/terraform init -backend=false
terraform -chdir=infra/terraform fmt -check -recursive
terraform -chdir=infra/terraform validate
```

Copy `terraform.tfvars.example` only when a non-default value is needed. The
local copy is ignored.

## Planned resource slices

Resources will be added in reviewable slices, not as one uninspectable apply:

1. network (implemented, not applied): VPC, internet gateway, two public and two
   private subnets, route tables, and narrowly scoped security groups;
2. storage and registry: private versioned S3 artifact bucket and private ECR;
3. Redis and observability: single-node ElastiCache and CloudWatch log groups;
4. IAM: separate ECS execution and task roles with resource-scoped policies;
5. ECS and ALB: cluster, task definitions, one-task service, target group,
   listener, and one-off materialization command support;
6. GitHub OIDC: repository-scoped build/push/deploy role.

No NAT Gateway, autoscaling, Multi-AZ Redis, public Redis, EKS, CodePipeline, or
other service outside the deployment plan should appear in these slices.

## Network contract

The VPC defaults to `10.42.0.0/20`, divided into two public and two private
`/24` subnets across the first two available AZs. The private route table has no
internet route. Public subnets do not assign public IPs by default; the later
ECS service must opt its Fargate ENIs into public IP assignment explicitly.

Security-group relationships are deliberately directional:

- the public CIDR input reaches only ALB port 80;
- the ALB reaches only ECS task port 8000;
- ECS tasks reach public HTTPS endpoints and Redis port 6379;
- Redis accepts port 6379 only from the ECS task security group.

The public HTTPS egress is required because this no-NAT design uses public-IP
Fargate tasks to pull from ECR, download artifacts from S3, and publish logs.
